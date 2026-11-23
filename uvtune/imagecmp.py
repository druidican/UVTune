"""Comparison of test results against the reference.

Results are either PNG images (ComfyUI load) or .npy float arrays (built-in PyTorch load).
"""
import hashlib
import io
import math

import numpy as np
from PIL import Image

NPY_MAGIC = b"\x93NUMPY"


def is_raw(data):
    return data[:6] == NPY_MAGIC


def ext(data):
    return ".npy" if is_raw(data) else ".png"


def decode(data):
    if is_raw(data):
        return np.load(io.BytesIO(data), allow_pickle=False)
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.uint8)


def pixel_hash(arr):
    return hashlib.sha256(arr.tobytes()).hexdigest()


def psnr(a, b):
    """Peak signal-to-noise ratio in dB of a against reference b.
    inf = identical, < 30 = clear difference. Works for images and float arrays."""
    if a.shape != b.shape:
        return 0.0
    a64, b64 = a.astype(np.float64), b.astype(np.float64)
    if not (np.isfinite(a64).all() and np.isfinite(b64).all()):
        return 0.0
    mse = np.mean((a64 - b64) ** 2)
    if mse == 0:
        return math.inf
    peak = 255.0 if a.dtype == np.uint8 else (float(np.max(np.abs(b64))) or 1.0)
    return 10 * math.log10(peak ** 2 / mse)


def fmt_psnr(v):
    if v is None:
        return "–"
    return "identical" if v == math.inf else f"{v:.1f} dB"
