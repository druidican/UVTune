"""UV Tune built-in PyTorch load.

Runs inside the user's own PyTorch (ROCm) environment as a separate process, so UV Tune
itself does not need PyTorch. It imports nothing from UV Tune.

The work imitates image-generation models: a stack of transformer blocks (attention + MLP,
like a diffusion transformer) and a stack of 3x3 convolutions (like a VAE), in bf16, with
fixed weights and a seeded input. The same seed and step count always give the same result
on a healthy GPU, so any difference means the GPU calculated something wrong.

Protocol: one JSON object per line on stdin and stdout.
  first line out:  {"ready": true, "device": ..., "torch": ..., "hip": ...}
  in:  {"cmd": "calibrate", "target_s": 30}   out: {"ok": true, "steps": N, "sec_per_step": x}
  in:  {"cmd": "run", "seed": 1001, "steps": N} out: {"ok": true, "secs": x, "data": <base64 .npy>}
  in:  {"cmd": "quit"}
Errors: {"ok": false, "error": "..."}

Environment variables (for testing only):
  UVTUNE_TORCH_DEVICE=cpu   run on the CPU
  UVTUNE_TORCH_SMALL=1      tiny model sizes
"""
import base64
import io
import json
import os
import sys
import time

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


try:
    import numpy as np
    import torch
    import torch.nn.functional as F
except Exception as e:  # noqa: BLE001
    out({"ready": False, "error": f"Could not import PyTorch/numpy: {e}"})
    sys.exit(2)

DEV = os.environ.get("UVTUNE_TORCH_DEVICE") or ("cuda" if torch.cuda.is_available() else None)
if DEV is None:
    out({"ready": False, "error": "PyTorch sees no GPU in this environment"})
    sys.exit(2)

SMALL = bool(os.environ.get("UVTUNE_TORCH_SMALL"))
DTYPE = torch.float32 if DEV == "cpu" else torch.bfloat16
B, T, D, H, L = (1, 128, 256, 4, 2) if SMALL else (2, 2048, 2048, 16, 6)   # transformer
C, R, NCONV = (16, 32, 2) if SMALL else (128, 384, 4)                     # conv stack

torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True
try:
    torch.use_deterministic_algorithms(True, warn_only=True)
except Exception:  # noqa: BLE001
    pass

# Fixed weights, made on the CPU so they are identical on every machine and every start.
_g = torch.Generator(device="cpu").manual_seed(0)


def _w(*shape, scale):
    return (torch.randn(*shape, generator=_g) * scale).to(DEV, DTYPE)


LAYERS = [dict(qkv=_w(3 * D, D, scale=D ** -0.5), o=_w(D, D, scale=D ** -0.5),
               up=_w(4 * D, D, scale=D ** -0.5), down=_w(D, 4 * D, scale=(4 * D) ** -0.5))
          for _ in range(L)]
CONVS = [_w(C, C, 3, 3, scale=(C * 9) ** -0.5) for _ in range(NCONV)]


def _block(x, p):
    h = F.layer_norm(x, (D,))
    q, k, v = F.linear(h, p["qkv"]).view(B, T, 3, H, D // H).permute(2, 0, 3, 1, 4)
    a = F.scaled_dot_product_attention(q, k, v)
    x = x + F.linear(a.transpose(1, 2).reshape(B, T, D), p["o"])
    h = F.layer_norm(x, (D,))
    return x + F.linear(F.gelu(F.linear(h, p["up"])), p["down"])


def _sync():
    if DEV != "cpu":
        torch.cuda.synchronize()


@torch.no_grad()
def run(seed, steps):
    gen = torch.Generator(device="cpu").manual_seed(int(seed))
    x = torch.randn(B, T, D, generator=gen).to(DEV, DTYPE)
    img = torch.randn(1, C, R, R, generator=gen).to(DEV, DTYPE)
    for s in range(int(steps)):
        for p in LAYERS:
            x = _block(x, p)
        x = F.layer_norm(x, (D,))
        img = F.group_norm(F.silu(F.conv2d(img, CONVS[s % NCONV], padding=1)), 8)
    xf, imf = x.float(), img.float()
    # Fingerprint: row sums cover every value; strided samples catch local errors.
    fp = torch.cat([xf.sum(-1).flatten(), xf.flatten()[::997], imf.sum((2, 3)).flatten(),
                    imf.flatten()[::991]])
    _sync()
    return fp.cpu().numpy().astype(np.float32)


def _encode(arr):
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    dev_name = torch.cuda.get_device_name(0) if DEV != "cpu" else "cpu"
    out({"ready": True, "device": dev_name, "torch": torch.__version__,
         "hip": getattr(torch.version, "hip", None)})
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        cmd = req.get("cmd")
        try:
            if cmd == "quit":
                break
            if cmd == "calibrate":
                run(12345, 2)  # warm-up: kernels, memory pools
                t0 = time.perf_counter()
                run(12345, 4)
                sps = (time.perf_counter() - t0) / 4
                steps = int(max(4, min(5000, round(float(req.get("target_s", 30)) / max(sps, 1e-4)))))
                out({"ok": True, "steps": steps, "sec_per_step": sps})
            elif cmd == "run":
                t0 = time.perf_counter()
                arr = run(req["seed"], req["steps"])
                secs = time.perf_counter() - t0
                out({"ok": True, "secs": secs, "data": _encode(arr),
                     "finite": bool(np.isfinite(arr).all())})
            else:
                out({"ok": False, "error": f"unknown command {cmd}"})
        except Exception as e:  # noqa: BLE001
            out({"ok": False, "error": f"{type(e).__name__}: {e}"[:1000]})
            if DEV != "cpu" and ("HIP" in str(e) or "CUDA" in str(e)):
                sys.exit(3)  # the GPU context is probably broken


if __name__ == "__main__":
    main()
