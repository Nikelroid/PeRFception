from torch.utils import data
import config
import os

from utils.select_option import select_model, select_dataloader

import torch
import pytorch_lightning as pl
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning import loggers as pl_loggers
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
from pytorch_lightning.plugins import DDPPlugin
import utils.metrics as metrics
import utils.store_image as store_image

class ValEveryNSteps(pl.Callback):
    def __init__(self, every_n_step):
        self.every_n_step = every_n_step

    def on_batch_end(self, trainer, pl_module):
        if trainer.global_step % self.every_n_step == 0 and trainer.global_step != 0:
            trainer.run_evaluation(test_mode=False)

if __name__ == "__main__":

    args = config.config_parser()
    basedir = args.basedir
    expname = args.expname
    logdir = os.path.join(basedir, expname)

    n_gpus = torch.cuda.device_count()

    os.makedirs(logdir, exist_ok=True)
    
    f = os.path.join(logdir, "args.txt")
    with open(f, "w") as file:
        for arg in sorted(vars(args)):
            attr = getattr(args, arg)
            file.write("{} = {}\n".format(arg, attr))

    f = os.path.join(logdir, "config.txt")
    with open(f, "w") as file:
        file.write(open(args.config, "r").read())

    wandb_logger = pl_loggers.WandbLogger(
        name=expname, entity="postech_cvlab", project=args.model
    )

    seed_everything(args.seed, workers=True)
    dataloader = select_dataloader(args)
    info = dataloader.get_info()
    model_fn = select_model(args)
    model = model_fn(args, info)

    lr_monitor = LearningRateMonitor(logging_interval="step")
    model_checkpoint = ModelCheckpoint(
        dirpath=logdir, filename="model", save_last=True
    )
    each_n_step = ValEveryNSteps(args.i_validation)

    trainer = Trainer(
        logger=wandb_logger if args.train and not args.debug else None, 
        log_every_n_steps=args.i_print,
        devices=n_gpus,
        max_steps=args.max_steps,
        accelerator="gpu",
        strategy=DDPPlugin(find_unused_parameters=False),
        deterministic=True,
        precision=16, 
        num_sanity_val_steps=-1 if args.debug else 0,
        callbacks=[lr_monitor, model_checkpoint, each_n_step], 
    )

    if args.train:
        trainer.fit(
            model, dataloader.train_dataloader(), dataloader.val_dataloader()
        )
    if args.eval:
        ckpt_path = os.path.join(logdir,"model.ckpt")
        model = model_fn.load_from_checkpoint(
            checkpoint_path=ckpt_path, args=args, info=info
        )
        trainer.test(model, dataloader.test_dataloader())
        ret = model.test_result
        if "rgbs" in ret: 
            rgbs, depths = ret["rgbs"], ret["depths"]
            imgdir = os.path.join(logdir, "render_model")
            os.makedirs(imgdir, exist_ok=True)
            store_image.store_image(imgdir, rgbs, depths)
        
        metrics.write_stats(
            os.path.join(logdir, "results.txt"), ret["psnr"], ret["ssim"], ret["lpips"]
        )
    if args.bake:
        pass