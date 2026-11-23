"""Default settings. The GUI saves the user's changes in config.json."""
import os

_HOME = os.path.expanduser("~")


def _docs():
    for name in ("Dokumenter", "Documents"):
        if os.path.isdir(os.path.join(_HOME, name)):
            return os.path.join(_HOME, name, "UV Tune")
    return os.path.join(_HOME, "UV Tune reports")

DEFAULTS = {
    # ComfyUI (the test instance runs on its own port, so it does not clash with your normal one)
    # Test load: "comfyui" (a ComfyUI workflow) or "torch" (built-in PyTorch test, no ComfyUI)
    "load_type": "comfyui",
    # ComfyUI workflow source: "builtin", "preset:<name>" or "file"
    "workflow_source": "builtin",
    "builtin_ckpt": "",
    "builtin_res": 1024,
    "builtin_steps": 30,
    "builtin_hires_steps": 20,
    "workflow_path": "",
    # Built-in PyTorch test: the Python that has PyTorch with ROCm (found by 'Scan system')
    "torch_python": "",
    "torch_env": "",          # extra environment variables, e.g. HSA_OVERRIDE_GFX_VERSION=11.0.0
    "torch_target_s": 30,     # seconds per result; the load calibrates its size once
    "comfy_cwd": os.path.join(_HOME, "ComfyUI"),
    "comfy_command": "venv/bin/python main.py --listen 127.0.0.1 --port 8189",
    "comfy_port": 8189,
    "comfy_startup_timeout_s": 300,

    # Power limits to test, in % of the card's default. The goal is lower power draw,
    # so testing goes from 100 % downwards.
    "power_caps_pct": "100, 90, 85, 80",

    # "auto" = automatic search, "manual" = test the settings in manual_list
    "test_mode": "auto",
    "manual_list": "",        # one setting per line: watts and mV, e.g. "254 -80"
    "manual_minutes": 60,     # test time per setting in manual mode

    # Search for the lowest stable voltage
    "coarse_step_mv": 25,
    "fine_step_mv": 5,
    "min_offset_mv": -200,
    "safety_margin_mv": 10,
    "warmup_minutes": 5,     # reference/warm-up, also after every reboot
    "screen_minutes": 3,     # short screening tests
    "confirm_minutes": 15,   # confirmation from the most aggressive setting upwards
    "verify_minutes": 30,    # final test with safety margin

    # How much slower than stock a setting may be and still count as
    # "unchanged performance" in the recommendation
    "perf_tolerance_pct": 2.0,

    # Report and shutdown
    "report_dir": _docs(),
    "auto_report": True,
    "shutdown_when_finished": False,
    "shutdown_delay_s": 60,

    # Safety and crash handling
    "reboot_on_gpu_reset": True,
    "stop_lactd": True,
    "auto_reboot_on_freeze": False,   # hardware-watchdog + kernel.panic
    "boot_grace_s": 90,
    "max_consecutive_crashes": 3,   # crashes in a row on the SAME setting (crash loop)
    "max_total_crashes": 40,        # overall limit for the whole run
    "image_timeout_factor": 4.0,
}


def merged(cfg):
    out = dict(DEFAULTS)
    if cfg:
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


def parse_caps(text):
    vals = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip().rstrip("%")
        if part:
            vals.append(float(part))
    return vals
