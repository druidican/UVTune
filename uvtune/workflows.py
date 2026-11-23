"""Test workflows for the ComfyUI load: a built-in one that uses only standard ComfyUI nodes,
presets shipped with UV Tune, or the user's own exported workflow file."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# key -> (label, file in uvtune/workflows/)
PRESETS = {
    "anima_int8": ("Preset: Anima int8 + Qwen (needs those models and custom nodes)", "anima_int8.json"),
}

SOURCES = {"builtin": "Built-in (standard ComfyUI nodes, choose a checkpoint)",
           **{f"preset:{k}": v[0] for k, v in PRESETS.items()},
           "file": "My own workflow file (API format)"}

MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".sft")


def list_checkpoints(comfy_dir):
    """Checkpoint names as ComfyUI lists them (paths relative to models/checkpoints)."""
    root = os.path.join(comfy_dir or "", "models", "checkpoints")
    out = []
    for dirpath, _, files in os.walk(root, followlinks=True):
        for f in files:
            if f.lower().endswith(MODEL_EXT):
                out.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return sorted(out, key=str.lower)


def builtin(ckpt, res=1024, steps=30, hires_steps=20):
    """Two-pass text-to-image workflow with locked seeds, using only core ComfyUI nodes:
    base pass at res x res, then a 1.5x hires pass (where the GPU works hardest)."""
    res = int(res)
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt},
              "_meta": {"title": "Checkpoint"}},
        "2": {"class_type": "CLIPTextEncode", "_meta": {"title": "Positive prompt"},
              "inputs": {"clip": ["1", 1], "text": (
                  "masterpiece, best quality, oil painting, classical landscape, rolling hills, "
                  "small village with stone cottages, church steeple, winding path, cypress trees, "
                  "misty mountains, dramatic clouds, warm golden hour light, detailed brushstrokes")}},
        "3": {"class_type": "CLIPTextEncode", "_meta": {"title": "Negative prompt"},
              "inputs": {"clip": ["1", 1],
                         "text": "worst quality, low quality, blurry, jpeg artifacts, watermark"}},
        "4": {"class_type": "EmptyLatentImage", "_meta": {"title": "Latent"},
              "inputs": {"width": res, "height": res, "batch_size": 1}},
        "5": {"class_type": "KSampler", "_meta": {"title": "Base pass (locked seed)"},
              "inputs": {"seed": 424242, "steps": int(steps), "cfg": 6.0, "sampler_name": "dpmpp_2m",
                         "scheduler": "karras", "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "LatentUpscaleBy", "_meta": {"title": "Upscale latent 1.5x"},
              "inputs": {"upscale_method": "nearest-exact", "scale_by": 1.5, "samples": ["5", 0]}},
        "7": {"class_type": "KSampler", "_meta": {"title": "Hires pass (locked seed)"},
              "inputs": {"seed": 777777, "steps": int(hires_steps), "cfg": 6.0, "sampler_name": "euler",
                         "scheduler": "normal", "denoise": 0.5, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "_meta": {"title": "VAE decode"},
              "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage", "_meta": {"title": "Save image"},
              "inputs": {"filename_prefix": "uvtune/test", "images": ["8", 0]}},
    }


def load_preset(key):
    label, fname = PRESETS[key]
    with open(os.path.join(HERE, "workflows", fname)) as f:
        return json.load(f)


def resolve(cfg):
    """Returns (workflow, error) for the ComfyUI load according to the settings."""
    src = cfg.get("workflow_source", "builtin")
    if src == "builtin":
        ckpt = cfg.get("builtin_ckpt")
        if not ckpt:
            return None, ("Choose a checkpoint for the built-in workflow "
                          "(models/checkpoints in your ComfyUI folder).")
        return builtin(ckpt, cfg.get("builtin_res", 1024), cfg.get("builtin_steps", 30),
                       cfg.get("builtin_hires_steps", 20)), None
    if src.startswith("preset:"):
        key = src.split(":", 1)[1]
        if key not in PRESETS:
            return None, f"Unknown preset: {key}"
        return load_preset(key), None
    wp = cfg.get("workflow_path", "")
    if not wp or not os.path.isfile(wp):
        return None, "Choose or upload your workflow (API format)."
    try:
        with open(wp) as f:
            return json.load(f), None
    except ValueError:
        return None, "The workflow file is not valid JSON."
