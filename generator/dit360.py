"""DiT360 loading and generation, following the DiT360 repo's own inference.py.

The pipeline class lives in the DiT360 repo (src/pipeline.py), not in diffusers, so the repo must be
cloned and passed in: git clone https://github.com/Insta360-Research-Team/DiT360.git
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_MODEL = "black-forest-labs/FLUX.1-dev"
LORA = "Insta360-Research/DiT360-Panorama-Image-Generation"
PROMPT_PREFIX = "This is a panorama. "
WIDTH, HEIGHT = 2048, 1024  # the size the model was trained at; other sizes may give abnormal results
STEPS, GUIDANCE = 28, 2.8  # values from the repo's inference.py


def load(repo_dir: str | Path, low_ram: bool = True):
    """Load FLUX.1-dev with the DiT360 LoRA on the GPU (about 37 GB of GPU memory in fp16).

    low_ram=True places each component directly on the GPU as it loads (diffusers' device_map="cuda"),
    so the 33 GB of weights never have to fit in system RAM at once. The repo's own inference.py
    loads everything to the CPU first and then moves it, which hung a 32 GiB machine twice.
    low_ram=False is that original path.
    """
    import torch

    sys.path.insert(0, str(repo_dir))
    from src.pipeline import DiT360Pipeline

    pipe = None
    if low_ram:
        try:
            pipe = DiT360Pipeline.from_pretrained(BASE_MODEL, torch_dtype=torch.float16, device_map="cuda")
        except Exception as err:  # noqa: BLE001 - this path is new and untested on a GPU, so fall back, don't fail
            print(f"low-RAM load failed ({err!r}); falling back to the original loader", flush=True)
            import gc

            gc.collect()
            torch.cuda.empty_cache()
    if pipe is None:
        device = torch.device("cuda:0")
        pipe = DiT360Pipeline.from_pretrained(BASE_MODEL, torch_dtype=torch.float16).to(device)
    pipe.load_lora_weights(LORA)
    return pipe


def generate(pipe, prompt: str, seed: int, steps: int = STEPS):
    """Return one 2:1 equirectangular PIL image."""
    import torch

    generator = torch.Generator(device="cuda:0").manual_seed(seed)
    return pipe(
        PROMPT_PREFIX + prompt,
        width=WIDTH,
        height=HEIGHT,
        num_inference_steps=steps,
        guidance_scale=GUIDANCE,
        generator=generator,
    ).images[0]
