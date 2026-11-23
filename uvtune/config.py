"""Standardindstillinger. GUI'en gemmer brugerens ændringer i config.json."""
import os

_HOME = os.path.expanduser("~")


def _docs():
    for name in ("Dokumenter", "Documents"):
        if os.path.isdir(os.path.join(_HOME, name)):
            return os.path.join(_HOME, name, "UV Tune")
    return os.path.join(_HOME, "UV Tune rapporter")

DEFAULTS = {
    # ComfyUI (test-instansen kører på sin egen port, så den ikke rammer din normale)
    "workflow_path": "",
    "comfy_cwd": os.path.join(_HOME, "AI/ComfyUI"),
    "comfy_command": ".rocm/bin/python main.py --use-ck-attention --bf16-unet --bf16-vae "
                     "--fast --listen 127.0.0.1 --port 8189",
    "comfy_port": 8189,
    "comfy_startup_timeout_s": 300,

    # Power limits der testes, i % af kortets standard. Målet er lavere forbrug,
    # så der testes fra 100 % og nedad.
    "power_caps_pct": "100, 90, 85, 80",

    # Søgning efter laveste stabile spænding
    "coarse_step_mv": 25,
    "fine_step_mv": 5,
    "min_offset_mv": -200,
    "safety_margin_mv": 10,
    "warmup_minutes": 5,     # reference/opvarmning, også efter hver genstart
    "screen_minutes": 3,     # korte screening-tests
    "confirm_minutes": 15,   # bekræftelse fra det mest moddede og opad
    "verify_minutes": 30,    # slut-test med sikkerhedsmargin

    # Hvor meget langsommere end standard en indstilling må være og stadig tælle som
    # "uændret ydelse" i anbefalingen
    "perf_tolerance_pct": 2.0,

    # Rapport og nedlukning
    "report_dir": _docs(),
    "auto_report": True,
    "shutdown_when_finished": False,
    "shutdown_delay_s": 60,

    # Sikkerhed og crash-håndtering
    "reboot_on_gpu_reset": True,
    "stop_lactd": True,
    "auto_reboot_on_freeze": False,   # hardware-watchdog + kernel.panic
    "boot_grace_s": 90,
    "max_consecutive_crashes": 3,   # crashes i træk på SAMME indstilling (crash-løkke)
    "max_total_crashes": 40,        # samlet loft for hele testen
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
