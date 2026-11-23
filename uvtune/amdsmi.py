"""Reads amd-smi (ships with ROCm). Output is shown generically, so it tolerates format changes
between ROCm versions."""
import json
import os
import random
import shutil
import subprocess

CANDIDATES = ["/opt/rocm/bin/amd-smi", "/usr/bin/amd-smi"]


def binary():
    b = shutil.which("amd-smi")
    if b:
        return b
    for c in CANDIDATES:
        if os.access(c, os.X_OK):
            return c
    import glob
    for c in sorted(glob.glob("/opt/rocm-*/bin/amd-smi"), reverse=True):
        if os.access(c, os.X_OK):
            return c
    return None


def run(*args, timeout=20):
    """Returns (data, error_text). data is JSON if possible, otherwise raw text."""
    if os.environ.get("UVTUNE_FAKE"):
        return _fake(args[0] if args else ""), None
    b = binary()
    if not b:
        from .scan import install_hint
        return None, ("amd-smi was not found. It ships with ROCm; install it with "
                      f"'{install_hint('amd-smi')}'.")
    try:
        r = subprocess.run([b, *args, "--json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"amd-smi failed: {e}"
    txt = r.stdout.strip()
    try:
        return json.loads(txt), None
    except ValueError:
        # some versions print warnings before the JSON
        start = min([i for i in (txt.find("["), txt.find("{")) if i >= 0], default=-1)
        if start >= 0:
            try:
                return json.loads(txt[start:]), None
            except ValueError:
                pass
        return None, (r.stderr.strip() or txt or f"amd-smi returned code {r.returncode}")[:2000]


def _fmt(v):
    if isinstance(v, dict) and "value" in v:
        unit = v.get("unit") or ""
        return f"{v['value']} {unit}".strip()
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def sections(data, gpu_index=0):
    """Turn amd-smi JSON into {section: [(key, value), ...]} for one GPU."""
    if isinstance(data, dict) and "gpu_data" in data:
        data = data["gpu_data"]
    if isinstance(data, list):
        if not data:
            return {}
        data = data[min(gpu_index, len(data) - 1)]
    if not isinstance(data, dict):
        return {"amd-smi": [("output", _fmt(data))]}
    out = {}
    for sec, val in data.items():
        if sec == "gpu":
            continue
        rows = []
        _flatten(val, "", rows)
        if rows:
            out[sec.replace("_", " ").title()] = rows
    return out


def _flatten(v, prefix, rows):
    if isinstance(v, dict) and not ("value" in v and len(v) <= 2):
        for k, x in v.items():
            _flatten(x, f"{prefix}{k}.", rows)
        return
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            rows.append((prefix.rstrip("."), ", ".join(_fmt(x) for x in v) if v else "–"))
        else:
            for i, x in enumerate(v):
                _flatten(x, f"{prefix}{i}.", rows)
        return
    if v in (None, "N/A", ""):
        return
    rows.append((prefix.rstrip(".").replace("_", " "), _fmt(v)))


def _fake(kind):
    if kind == "static":
        return [{"gpu": 0, "asic": {"market_name": "AMD Radeon RX 9070 XT (simulated)",
                                    "target_graphics_version": "gfx1201", "num_compute_units": 64},
                 "vbios": {"name": "SIMULATED", "version": "113-0000"},
                 "driver": {"name": "amdgpu", "version": "7.3.0"},
                 "limit": {"max_power": {"value": 304, "unit": "W"},
                           "slowdown_edge_temperature": {"value": 100, "unit": "C"}},
                 "vram": {"type": "GDDR6", "size": {"value": 16304, "unit": "MB"}}}]
    return [{"gpu": 0,
             "usage": {"gfx_activity": {"value": random.randint(95, 100), "unit": "%"},
                       "umc_activity": {"value": random.randint(30, 60), "unit": "%"}},
             "power": {"socket_power": {"value": random.randint(250, 300), "unit": "W"}},
             "clock": {"gfx_0": {"clk": {"value": random.randint(2900, 3100), "unit": "MHz"}},
                       "mem_0": {"clk": {"value": 1258, "unit": "MHz"}}},
             "temperature": {"edge": {"value": 78, "unit": "C"},
                             "hotspot": {"value": random.randint(88, 95), "unit": "C"},
                             "mem": {"value": 80, "unit": "C"}},
             "fan": {"speed": 120, "max": 255, "rpm": random.randint(1500, 1700), "usage": {"value": 47, "unit": "%"}},
             "throttle": {"power_throttle": "ACTIVE", "thermal_throttle": "NOT ACTIVE"},
             "pcie": {"width": 16, "speed": {"value": 16, "unit": "GT/s"}}}]
