from random import random
import math
from pathlib import Path
from functools import partial
from multiprocessing import cpu_count

import torch
import torch.nn.functional as F
from torch.optim import Adam
from torch.utils.data import Dataset, DataLoader
from torchvision import utils, transforms as T

from einops import rearrange, reduce

from tqdm import tqdm

from denoising_diffusion_pytorch import GaussianDiffusion
from denoising_diffusion_pytorch.fid_evaluation import FIDEvaluation

from deep_image_prior.utils.denoising_utils import *


def exists(x):
    return x is not None

def default(val, d):
    if exists(val):
        return val
    return d() if callable(d) else d

def divisible_by(numer, denom):
    return (numer % denom) == 0

def cycle(dl):
    while True:
        for data in dl:
            yield data

def has_int_squareroot(num):
    return (math.sqrt(num) ** 2) == num

def num_to_groups(num, divisor):
    groups = num // divisor
    remainder = num % divisor
    arr = [divisor] * groups
    if remainder > 0:
        arr.append(remainder)
    return arr

def convert_image_to_fn(img_type, image):
    if image.mode != img_type:
        return image.convert(img_type)
    return image

def extract(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))

def normalize_norm_dist(x):
    return (x - torch.mean(x)) / torch.std(x)

def collate_fn(batch):
    data, noise = zip(*batch)
    return torch.stack(data), torch.stack(noise)

def generate_noise(dip_model,
                   dip_input_depth,
                   datasets_folder,
                   noises_folder,
                   train_num_steps = 100,
                   exts = ['jpg', 'jpeg', 'png', 'tiff']
):
    for ext in exts:
        for img_path in tqdm(Path(f'{datasets_folder}').glob(f'**/*.{ext}'), desc = 'generating noise'):
            noise_path = Path(noises_folder) / f'{img_path.stem}.pth'
            if noise_path.exists():
                continue
            dip_trainer = DIPTrainer(
                model = dip_model,
                dip_input_depth = dip_input_depth,
                train_img = str(img_path),
                results_folder = noises_folder
            )
            dip_trainer.train(train_num_steps=train_num_steps)
            dip_trainer.save_noise(f'{img_path.stem}.pth')



class GaussianDiffusionWithDeepImagePrior(GaussianDiffusion):
    def __init__(
        self,
        base_model,
        dip_model,
        dip_input_depth,
        *,
        image_size,
        timesteps = 1000,
        sampling_timesteps = None,
        objective = 'pred_v',
        beta_schedule = 'sigmoid',
        schedule_fn_kwargs = dict(),
        ddim_sampling_eta = 0.,
        auto_normalize = True,
        offset_noise_strength = 0.,  # https://www.crosslabs.org/blog/diffusion-with-offset-noise
        min_snr_loss_weight = False, # https://arxiv.org/abs/2303.09556
        min_snr_gamma = 5
    ):
        super().__init__(
            model = base_model,
            image_size = image_size,
            timesteps = timesteps,
            sampling_timesteps = sampling_timesteps,
            objective = objective,
            beta_schedule = beta_schedule,
            schedule_fn_kwargs = schedule_fn_kwargs,
            ddim_sampling_eta = ddim_sampling_eta,
            auto_normalize = auto_normalize,
            offset_noise_strength = offset_noise_strength,
            min_snr_loss_weight = min_snr_loss_weight,
            min_snr_gamma = min_snr_gamma
        )
        
        self.dip_model = dip_model.type(torch.FloatTensor)
        self.dip_input_depth = dip_input_depth
        
    @torch.inference_mode()
    def p_sample_loop(self, shape, return_all_timesteps = False):
        batch, device = shape[0], self.device

        dip_input = get_noise(self.dip_input_depth, 'noise', shape[-2:]).to(device)
        dip_input = dip_input.expand(batch, -1, -1, -1)
        img = self.dip_model(dip_input)
        img = normalize_norm_dist(img)
        imgs = [img]

        x_start = None

        for t in tqdm(reversed(range(0, self.num_timesteps)), desc = 'sampling loop time step', total = self.num_timesteps, leave = False):
            self_cond = x_start if self.self_condition else None
            img, x_start = self.p_sample(img, t, self_cond)
            imgs.append(img)

        ret = img if not return_all_timesteps else torch.stack(imgs, dim = 1)

        ret = self.unnormalize(ret)
        return ret

    @torch.inference_mode()
    def ddim_sample(self, shape, return_all_timesteps = False):
        batch, device, total_timesteps, sampling_timesteps, eta, objective = shape[0], self.device, self.num_timesteps, self.sampling_timesteps, self.ddim_sampling_eta, self.objective

        times = torch.linspace(-1, total_timesteps - 1, steps = sampling_timesteps + 1)   # [-1, 0, 1, 2, ..., T-1] when sampling_timesteps == total_timesteps
        times = list(reversed(times.int().tolist()))
        time_pairs = list(zip(times[:-1], times[1:])) # [(T-1, T-2), (T-2, T-3), ..., (1, 0), (0, -1)]

        dip_input = get_noise(self.dip_input_depth, 'noise', shape[-2:]).to(device)
        dip_input = dip_input.expand(batch, -1, -1, -1)
        img = self.dip_model(dip_input)
        img = normalize_norm_dist(img)
        imgs = [img]

        x_start = None

        for time, time_next in tqdm(time_pairs, desc = 'sampling loop time step', leave = False):
            time_cond = torch.full((batch,), time, device = device, dtype = torch.long)
            self_cond = x_start if self.self_condition else None
            pred_noise, x_start, *_ = self.model_predictions(img, time_cond, self_cond, clip_x_start = True, rederive_pred_noise = True)

            if time_next < 0:
                img = x_start
                imgs.append(img)
                continue

            alpha = self.alphas_cumprod[time]
            alpha_next = self.alphas_cumprod[time_next]

            sigma = eta * ((1 - alpha / alpha_next) * (1 - alpha_next) / (1 - alpha)).sqrt()
            c = (1 - alpha_next - sigma ** 2).sqrt()

            noise = torch.randn_like(img)

            img = x_start * alpha_next.sqrt() + \
                  c * pred_noise + \
                  sigma * noise

            imgs.append(img)

        ret = img if not return_all_timesteps else torch.stack(imgs, dim = 1)

        ret = self.unnormalize(ret)
        return ret

    @torch.inference_mode()
    def sample(self, batch_size = 16, return_all_timesteps = False):
        (h, w), channels = self.image_size, self.channels
        sample_fn = self.p_sample_loop if not self.is_ddim_sampling else self.ddim_sample
        return sample_fn((batch_size, channels, h, w), return_all_timesteps = return_all_timesteps)

    def p_losses(self, x_start, t, noise = None, offset_noise_strength = None):
        b, c, h, w = x_start.shape
        
        dip_input = get_noise(self.dip_input_depth, 'noise', (h, w)).to(self.device)
        dip_input = dip_input.expand(b, -1, -1, -1)
        
        dip_out = self.dip_model(dip_input)
        
        noise = default(noise, lambda: normalize_norm_dist(dip_out - x_start))

        # offset noise - https://www.crosslabs.org/blog/diffusion-with-offset-noise

        offset_noise_strength = default(offset_noise_strength, self.offset_noise_strength)

        if offset_noise_strength > 0.:
            offset_noise = torch.randn(x_start.shape[:2], device = self.device)
            noise += offset_noise_strength * rearrange(offset_noise, 'b c -> b c 1 1')

        # noise sample

        x = self.q_sample(x_start = x_start, t = t, noise = noise)

        # if doing self-conditioning, 50% of the time, predict x_start from current set of times
        # and condition with unet with that
        # this technique will slow down training by 25%, but seems to lower FID significantly

        x_self_cond = None
        if self.self_condition and random() < 0.5:
            with torch.no_grad():
                x_self_cond = self.model_predictions(x, t).pred_x_start
                x_self_cond.detach_()

        # predict and take gradient step

        model_out = self.model(x, t, x_self_cond)

        if self.objective == 'pred_noise':
            target = noise
        elif self.objective == 'pred_x0':
            target = x_start
        elif self.objective == 'pred_v':
            v = self.predict_v(x_start, t, noise)
            target = v
        else:
            raise ValueError(f'unknown objective {self.objective}')

        loss = F.mse_loss(model_out, target, reduction = 'none')
        loss = reduce(loss, 'b ... -> b', 'mean')

        loss = loss * extract(self.loss_weight, t, loss.shape)
        
        dip_loss = F.mse_loss(dip_out, x_start)
        dip_loss = dip_loss.mean() * self.loss_weight[-1]
        
        return loss.mean() + dip_loss

    def forward(self, img, *args, **kwargs):
        b, c, h, w, device, img_size, = *img.shape, img.device, self.image_size
        assert h == img_size[0] and w == img_size[1], f'height and width of image must be {img_size}'
        t = torch.randint(0, self.num_timesteps, (b,), device=device).long()

        img = self.normalize(img)
        return self.p_losses(img, t, *args, **kwargs)



class Dataset(Dataset):
    def __init__(
        self,
        folder,
        noise_folder,
        image_size,
        exts = ['jpg', 'jpeg', 'png', 'tiff'],
        augment_horizontal_flip = False,
        convert_image_to = None
    ):
        super().__init__()
        self.folder = folder
        self.noise_folder = noise_folder
        self.image_size = image_size
        self.paths = [p for ext in exts for p in Path(f'{folder}').glob(f'**/*.{ext}')]

        maybe_convert_fn = partial(convert_image_to_fn, convert_image_to) if exists(convert_image_to) else nn.Identity()

        self.transform = T.Compose([
            T.Lambda(maybe_convert_fn),
            T.Resize(image_size),
            T.RandomHorizontalFlip() if augment_horizontal_flip else nn.Identity(),
            T.CenterCrop(image_size),
            T.ToTensor()
        ])

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, index):
        path = self.paths[index]
        img = Image.open(path)
        noise_path = os.path.join(self.noise_folder, f'{path.stem}.pth')
        noise = torch.load(noise_path)
        return self.transform(img), noise



class Trainer:
    def __init__(
        self,
        diffusion_model,
        folder,
        noise_folder,
        *,
        train_batch_size = 16,
        gradient_accumulate_every = 1,
        augment_horizontal_flip = True,
        train_lr = 1e-4,
        train_num_steps = 100000,
        ema_update_every = 10,
        ema_decay = 0.995,
        adam_betas = (0.9, 0.99),
        save_and_sample_every = 1000,
        num_samples = 25,
        results_folder = './results',
        convert_image_to = None,
        calculate_fid = True,
        inception_block_idx = 2048,
        max_grad_norm = 1.,
        num_fid_samples = 50000,
        save_best_and_latest_only = False
    ):

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        # model

        self.model = diffusion_model.to(self.device)
        self.channels = diffusion_model.channels
        is_ddim_sampling = diffusion_model.is_ddim_sampling

        # default convert_image_to depending on channels

        if not exists(convert_image_to):
            convert_image_to = {1: 'L', 3: 'RGB', 4: 'RGBA'}.get(self.channels)

        # sampling and training hyperparameters

        assert has_int_squareroot(num_samples), 'number of samples must have an integer square root'
        self.num_samples = num_samples
        self.save_and_sample_every = save_and_sample_every

        self.batch_size = train_batch_size
        self.gradient_accumulate_every = gradient_accumulate_every
        assert (train_batch_size * gradient_accumulate_every) >= 16, f'your effective batch size (train_batch_size x gradient_accumulate_every) should be at least 16 or above'

        self.train_num_steps = train_num_steps
        self.image_size = diffusion_model.image_size

        self.max_grad_norm = max_grad_norm

        # dataset and dataloader

        self.ds = Dataset(folder, noise_folder, self.image_size, augment_horizontal_flip = augment_horizontal_flip, convert_image_to = convert_image_to)

        assert len(self.ds) >= 100, 'you should have at least 100 images in your folder. at least 10k images recommended'

        dl = DataLoader(self.ds, batch_size = train_batch_size, shuffle = True, pin_memory = True, num_workers = cpu_count(), collate_fn = collate_fn)
        
        self.dl = cycle(dl)

        # optimizer

        self.opt = Adam(diffusion_model.parameters(), lr = train_lr, betas = adam_betas)

        # for logging results in a folder periodically

        self.results_folder = Path(results_folder)
        self.results_folder.mkdir(exist_ok = True)

        # step counter state

        self.step = 0

        # FID-score computation

        self.calculate_fid = calculate_fid

        if self.calculate_fid:
            if not is_ddim_sampling:
                print(
                    "WARNING: Robust FID computation requires a lot of generated samples and can therefore be very time consuming."\
                    "Consider using DDIM sampling to save time."
                )
            self.fid_scorer = FIDEvaluation(
                batch_size=self.batch_size,
                dl=self.dl,
                sampler=self.model,
                channels=self.channels,
                stats_dir=results_folder,
                device=self.device,
                num_fid_samples=num_fid_samples,
                inception_block_idx=inception_block_idx
            )

        if save_best_and_latest_only:
            assert calculate_fid, "`calculate_fid` must be True to provide a means for model evaluation for `save_best_and_latest_only`."
            self.best_fid = 1e10 # infinite

        self.save_best_and_latest_only = save_best_and_latest_only


    def save(self, milestone):
        data = {
            'step': self.step,
            'model': self.model.state_dict(),
            'opt': self.opt.state_dict(),
        }

        torch.save(data, str(self.results_folder / f'model-{milestone}.pt'))

    def load(self, milestone):
        data = torch.load(str(self.results_folder / f'model-{milestone}.pt'), map_location=self.device)

        self.model.load_state_dict(data['model'])

        self.step = data['step']
        self.opt.load_state_dict(data['opt'])

    def train(self):
        device = self.device

        with tqdm(initial = self.step, total = self.train_num_steps) as pbar:

            while self.step < self.train_num_steps:

                total_loss = 0.

                for _ in range(self.gradient_accumulate_every):
                    data, noise = next(self.dl)
                    data = data.to(device)
                    noise = noise.to(device)

                    loss = self.model(data, noise = noise)
                    loss = loss / self.gradient_accumulate_every
                    total_loss += loss.item()

                    loss.backward()

                pbar.set_description(f'loss: {total_loss:.4f}')

                self.opt.step()
                self.opt.zero_grad()

                self.step += 1

                if self.step != 0 and divisible_by(self.step, self.save_and_sample_every):
                    self.model.eval()

                    with torch.inference_mode():
                        milestone = self.step // self.save_and_sample_every
                        batches = num_to_groups(self.num_samples, self.batch_size)
                        all_images_list = list(map(lambda n: self.ema.ema_model.sample(batch_size=n), batches))

                    all_images = torch.cat(all_images_list, dim = 0)

                    utils.save_image(all_images, str(self.results_folder / f'sample-{milestone}.png'), nrow = int(math.sqrt(self.num_samples)))

                    # whether to calculate fid

                    if self.calculate_fid:
                        fid_score = self.fid_scorer.fid_score()
                        print(f'fid_score: {fid_score}')
                    if self.save_best_and_latest_only:
                        if self.best_fid > fid_score:
                            self.best_fid = fid_score
                            self.save("best")
                        self.save("latest")
                    else:
                        self.save(milestone)

                pbar.update(1)

        print('training complete')



class DIPTrainer:
    def __init__(
        self,
        model,
        dip_input_depth,
        train_img,
        *,
        learning_rate = 1e-4,
        adam_betas = (0.9, 0.99),
        results_folder = './results',
        device = None
    ):
        self.device = default(device, torch.device('cuda' if torch.cuda.is_available() else 'cpu'))
        self.dtype = torch.FloatTensor
        
        self.model = model.type(self.dtype).to(self.device)
        self.dip_input_depth = dip_input_depth
        self.train_img = self._load_image(train_img).type(self.dtype).to(self.device)
        self.result_folder = results_folder
        self.image_size = self.train_img.shape[-2:]

        # optimizer

        self.opt = Adam(self.model.parameters(), lr = learning_rate, betas = adam_betas)
        
        self.model_input = get_noise(self.dip_input_depth, 'noise', self.image_size).to(self.device)

        self.step = 0
        os.makedirs(self.result_folder, exist_ok=True)
    
    def _load_image(self, train_img):
        if isinstance(train_img, str):
            train_img = get_image(train_img)[0]
        if isinstance(train_img, Image.Image):
            train_img = crop_image(train_img, d=self.dip_input_depth)
            train_img = pil_to_np(train_img)
        if isinstance(train_img, np.ndarray):
            train_img = np_to_torch(train_img)
        if not isinstance(train_img, torch.Tensor):
            raise ValueError('train_img must be a string, image, numpy array or torch tensor')
        return train_img
    
    def save(self, milestone):
        data = {
            'step': self.step,
            'model': self.model.state_dict(),
            'model_input': self.model_input,
        }
        torch.save(data, os.path.join(self.result_folder, f'dip_model_{milestone}.pth'))
    
    def load(self, milestone):
        data = torch.load(os.path.join(self.result_folder, f'dip_model_{milestone}.pth'))
        self.step = data['step']
        self.model.load_state_dict(data['model'])
        self.model_input = data['model_input']
        
    def predict(self):
        self.model.eval()
        out = self.model(self.model_input)
        return out

    def show_image(self):
        out = self.predict()
        out_np = torch_to_np(out)
        img_np = torch_to_np(self.train_img)
        plot_image_grid([img_np, out_np], factor = 13)
    
    def save_image(self):
        out = self.predict()
        out_np = torch_to_np(out)
        out_img = np_to_pil(out_np)
        out_img.save(os.path.join(self.result_folder, f'dip_{self.step}.png'))
    
    def generate_noise(self):
        out = self.predict()
        noise = normalize_norm_dist(out - self.train_img)
        return noise

    def save_noise(self, filename):
        noise = self.generate_noise().detach().cpu()[0]
        torch.save(noise, os.path.join(self.result_folder, filename))
    
    def show_noise(self):
        noise = self.generate_noise()
        noise = (noise + 1) / 2
        noise_np = torch_to_np(noise)
        noise_np = np.clip(noise_np, 0, 1)
        plot_image_grid([noise_np], factor = 13)
    
    def train(self, train_num_steps = 10000, predict_every = 10000, save_every = 10000):
        
        with tqdm(initial = self.step, total = train_num_steps, leave = False) as pbar:
            while self.step < train_num_steps:
                self.model.train()
                self.opt.zero_grad()
                output = self.model(self.model_input)
                loss = F.mse_loss(output, self.train_img)
                loss.backward()
                self.opt.step()
                
                self.step += 1
                pbar.update(1)
                
                if self.step % predict_every == 0:
                    self.save_image()
            
                if self.step % save_every == 0:
                    milestone = self.step // save_every
                    self.save(milestone)