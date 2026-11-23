"""Sammenligning af testbilleder mod referencebilledet."""
import hashlib
import io
import math

import numpy as np
from PIL import Image


def decode(data):
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.uint8)


def pixel_hash(arr):
    return hashlib.sha256(arr.tobytes()).hexdigest()


def psnr(a, b):
    """Peak signal-to-noise ratio i dB. inf = identiske, < 30 = tydelig forskel."""
    if a.shape != b.shape:
        return 0.0
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    if mse == 0:
        return math.inf
    return 10 * math.log10(255.0 ** 2 / mse)


def fmt_psnr(v):
    if v is None:
        return "–"
    return "identisk" if v == math.inf else f"{v:.1f} dB"
