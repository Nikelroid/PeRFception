import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import model.jaxnerf_torch.utils as utils
import utils.embedder as embedder
import logging
import pytorch_lightning as pl
import utils.metrics as metrics

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class NeRF(nn.Module):
    def __init__(
        self,
        D=8,
        W=256,
        input_ch=3,
        input_ch_views=3,
        output_ch=4,
        skips=[4],
        use_viewdirs=False,
    ):
        super(NeRF, self).__init__()
        self.D = D
        self.W = W
        self.input_ch = input_ch
        self.input_ch_views = input_ch_views
        self.skips = skips
        self.use_viewdirs = use_viewdirs

        self.pts_linears = nn.ModuleList(
            [nn.Linear(input_ch, W)]
            + [
                nn.Linear(W, W) if i not in self.skips else nn.Linear(W + input_ch, W)
                for i in range(D - 1)
            ]
        )

        ### Implementation according to the official code release 
        # (https://github.com/bmild/nerf/blob/master/run_nerf_helpers.py#L104-L105)
        self.views_linears = nn.ModuleList([nn.Linear(input_ch_views + W, W // 2)])

        ### Implementation according to the paper

        if use_viewdirs:
            self.feature_linear = nn.Linear(W, W)
            self.alpha_linear = nn.Linear(W, 1)
            self.rgb_linear = nn.Linear(W // 2, 3)
        else:
            self.output_linear = nn.Linear(W, output_ch)

    def forward(self, x):
        input_pts, input_views = torch.split(
            x, [self.input_ch, self.input_ch_views], dim=-1
        )
        h = input_pts
        for i, l in enumerate(self.pts_linears):
            h = self.pts_linears[i](h)
            h = F.relu(h)
            if i in self.skips:
                h = torch.cat([input_pts, h], -1)

        if self.use_viewdirs:
            alpha = self.alpha_linear(h)
            feature = self.feature_linear(h)
            h = torch.cat([feature, input_views], -1)

            for i, l in enumerate(self.views_linears):
                h = self.views_linears[i](h)
                h = F.relu(h)

            rgb = self.rgb_linear(h)
            outputs = torch.cat([rgb, alpha], -1)
        else:
            outputs = self.output_linear(h)

        return outputs


class LitJaxNeRF(pl.LightningModule):

    def __init__(self, args, info):
        super(LitJaxNeRF, self).__init__()
        self.args = args
        (
            self.h, self.w, self.intrinsics, self.i_train, 
            self.i_val, self.i_test, self.val_dummy, self.test_dummy, 
            self.near, self.far
        ) = info["h"], info["w"], info["intrinsics"], info["i_train"], \
            info["i_val"], info["i_test"], info["val_dummy"], info["test_dummy"], \
            info["near"], info["far"]
        
        self.img_size = self.h * self.w
        self.create_model()

    def on_train_start(self):
        self.logger.log_hyperparams(self.args)

    def create_model(self):
        args = self.args
        embed_fn, input_ch = embedder.get_embedder(args.multires, args.i_embed)

        input_ch_views = 0
        embeddirs_fn = None
        if args.use_viewdirs:
            embeddirs_fn, input_ch_views = embedder.get_embedder(
                args.multires_views, args.i_embed
            )
        output_ch = 5 if args.num_fine_samples > 0 else 4
        skips = [4]
        self.model = NeRF(
            D=args.netdepth, W=args.netwidth, input_ch=input_ch,
            output_ch=output_ch, skips=skips, input_ch_views=input_ch_views,
            use_viewdirs=args.use_viewdirs,
        )

        self.model_fine = None
        if args.num_fine_samples > 0:
            self.model_fine = NeRF(
                D=args.netdepth_fine, W=args.netwidth_fine, input_ch=input_ch,
                output_ch=output_ch, skips=skips, input_ch_views=input_ch_views,
                use_viewdirs=args.use_viewdirs,
            )

        network_query_fn = lambda inputs, viewdirs, network_fn: utils.run_network(
            inputs,
            viewdirs,
            network_fn,
            embed_fn=embed_fn,
            embeddirs_fn=embeddirs_fn,
            netchunk=args.netchunk,
        )

        render_kwargs_train = {
            "network_query_fn": network_query_fn,
            "perturb": args.perturb,
            "num_fine_samples": args.num_fine_samples,
            "network_fine": self.model_fine,
            "num_coarse_samples": args.num_coarse_samples,
            "network_fn": self.model,
            "use_viewdirs": args.use_viewdirs,
            "white_bkgd": args.white_bkgd,
            "raw_noise_std": args.raw_noise_std,
            "near": self.near, 
            "far": self.far
        }

        # NDC only good for LLFF-style forward facing data
        if args.dataset_type != "llff" or args.no_ndc:
            render_kwargs_train["ndc"] = False
            render_kwargs_train["lindisp"] = args.lindisp

        render_kwargs_test = {
            k : render_kwargs_train[k] for k in render_kwargs_train
        }
        render_kwargs_test['perturb'] = False
        render_kwargs_test['raw_noise_std'] = 0.

        self.start = 0
        self.render_kwargs_train = render_kwargs_train
        self.render_kwargs_test = render_kwargs_test

    def configure_optimizers(self):
        return torch.optim.Adam(
            params=self.parameters(), lr=self.args.lr_init, betas=(0.9, 0.999)
        )

    def forward(self, batch_rays):
        kwargs = self.render_kwargs_train if self.mode == "train" \
            else self.render_kwargs_test
        return utils.render(
            self.h, self.w, self.intrinsics, chunk=self.args.chunk,
            rays=batch_rays, use_pixel_centers=self.args.use_pixel_centers, 
            **kwargs
        )

    def on_train_start(self):
        self.mode = "train"

    def on_validation_start(self):
        self.mode = "val"

    def on_test_start(self):
        self.mode = "test"

    def training_step(self, batch, batch_idx):
        batch_rays = batch["ray"]
        target = batch["target"]
        rgb, disp, acc, depth, extras = self(batch_rays)
        img_loss = utils.img2mse(rgb, target)
        loss1 = img_loss
        loss = loss1
        psnr = utils.mse2psnr(img_loss)

        if "rgb0" in extras:
            loss0 = utils.img2mse(extras["rgb0"], target)
            psnr0 = utils.mse2psnr(loss0)
            loss = loss + loss0

        self.log("train/psnr1", psnr, on_step=True, prog_bar=True, logger=True)
        self.log("train/psnr0", psnr0, on_step=True)
        self.log("train/loss", loss, on_step=True)
        return loss

    def optimizer_step(
        self, epoch, batch_idx, optimizer, optimizer_idx, 
        optimizer_closure, on_tpu, using_native_amp, using_lbfgs
    ):
        args = self.args
        step = self.trainer.global_step
        if args.lr_delay_steps > 0:
            delay_rate = args.lr_delay_mult + (1 - args.lr_delay_mult) * np.sin(
                0.5 * np.pi * np.clip(step / args.lr_delay_steps, 0, 1)
            )
        else:
            delay_rate = 1.

        t = np.clip(step / args.max_steps, 0, 1)
        log_lerp = np.exp(np.log(args.lr_init) * (1 - t) + np.log(args.lr_final) * t)
        new_lrate = delay_rate * log_lerp

        for pg in optimizer.param_groups:
            pg["lr"] = new_lrate
        
        optimizer.step(closure=optimizer_closure)

    def render_rays(self, batch, batch_idx):
        batch_rays = batch["ray"]   
        target = batch["target"]
        rgb, disp, acc, depth, extras = self(batch_rays)
        return {
            "rgb": rgb, 
            "depth": depth,
            "target": target
        }

    def validation_step(self, batch, batch_idx):
        return self.render_rays(batch, batch_idx)

    def test_step(self, batch, batch_idx):
        return self.render_rays(batch, batch_idx)
        
    def gather_results(self, outputs, dummy_num):
        outputs_gather = self.all_gather(outputs)
        rgbs = torch.cat([torch.cat([*out["rgb"]]) for out in outputs_gather])
        target = torch.cat([torch.cat([*out["target"]]) for out in outputs_gather])
        depths = torch.cat([torch.cat([*out["depth"]]) for out in outputs_gather])
        if dummy_num != 0:
            rgbs, target, depths = rgbs[:-dummy_num], target[:-dummy_num], depths[:-dummy_num]
        return rgbs, target, depths

    def test_epoch_end(self, outputs):
        rgbs, target, depths = self.gather_results(outputs, self.test_dummy)
        rgbs = rgbs.reshape(-1, self.h, self.w, 3).detach().cpu().numpy()
        target = target.reshape(-1, self.h, self.w, 3).detach().cpu().numpy()
        depths = depths.reshape(-1, self.h, self.w).detach().cpu().numpy()
        psnr = metrics.psnr(rgbs, target, self.i_train, self.i_val, self.i_test)
        ssim = metrics.ssim(rgbs, target, self.i_train, self.i_val, self.i_test)
        lpips = metrics.lpips_v(rgbs, target, self.i_train, self.i_val, self.i_test)
        self.test_result = {
            "rgbs": rgbs, "depths": depths, 
            "psnr": psnr, "ssim": ssim, "lpips": lpips
        } 

    def validation_epoch_end(self, outputs):
        rgbs, target, _ = self.gather_results(outputs, self.val_dummy)
        rgbs, target = rgbs.reshape(-1, self.img_size * 3), target.reshape(-1, self.img_size * 3)
        mse = torch.mean((target - rgbs) ** 2, dim=1)
        psnr = -10.0 * torch.log(mse) / np.log(10)
        psnr_mean = psnr.mean()
        self.log("validation/psnr", psnr_mean, on_epoch=True, sync_dist=True)