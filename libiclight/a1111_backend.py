""" A1111 IC Light extension backend."""

import os
from typing import Callable
import torch
import numpy as np
import safetensors.torch

from modules import devices
from modules.paths import models_path
from modules.processing import StableDiffusionProcessing

try:
    from lib_modelpatcher.model_patcher import ModulePatch
except ImportError as e:
    print("Please install sd-webui-model-patcher")
    raise e

from .args import ICLightArgs
from .utils import numpy2pytorch


def vae_encode(sd_model, img: torch.Tensor) -> torch.Tensor:
    """
    img: [B, C, H, W] format tensor. Value from -1.0 to 1.0.
    Return tensor in [B, C, H, W] format.

    Note: Input img format differs from forge/comfy's vae input format.
    """
    return sd_model.get_first_stage_encoding(sd_model.encode_first_stage(img))


def apply_ic_light(
    p: StableDiffusionProcessing,
    args: ICLightArgs,
):
    device = devices.get_device_for("ic_light")
    dtype = devices.dtype_unet

    # Load model
    unet_path = os.path.join(models_path, "unet", args.model_type.model_name)
    ic_model_state_dict = safetensors.torch.load_file(unet_path)

    # Get input
    input_rgb: np.ndarray = args.get_input_rgb(device=device)

    # [B, 4, H, W]
    concat_conds = vae_encode(
        p.sd_model,
        numpy2pytorch(args.get_concat_cond(input_rgb, p)).to(
            dtype=devices.dtype_vae, device=device
        ),
    ).to(dtype=devices.dtype_unet)
    # [1, 4 * B, H, W]
    concat_conds = torch.cat([c[None, ...] for c in concat_conds], dim=1)

    def apply_c_concat(unet, old_forward: Callable) -> Callable:
        def new_forward(x, timesteps=None, context=None, **kwargs):
            # Expand according to batch number.
            c_concat = torch.cat(
                ([concat_conds.to(x.device)] * (x.shape[0] // concat_conds.shape[0])),
                dim=0,
            )
            new_x = torch.cat([x, c_concat], dim=1)
            return old_forward(new_x, timesteps, context, **kwargs)

        return new_forward

    # Patch unet forward.
    p.model_patcher.add_module_patch(
        "diffusion_model", ModulePatch(create_new_forward_func=apply_c_concat)
    )
    # Patch weights.
    p.model_patcher.add_patches(
        patches={
            "diffusion_model." + key: (value.to(dtype=dtype, device=device),)
            for key, value in ic_model_state_dict.items()
        }
    )