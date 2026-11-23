"""System scan: finds ROCm, ComfyUI installations and Python environments with PyTorch.

Runs as the normal user (from the GUI). Every Python candidate is probed in its own process,
so a broken environment can never affect UV Tune itself.
"""
import glob
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

PROBE = r"""
import json, sys
r = {"python": sys.version.split()[0]}
try:
    import torch
    r["torch"] = torch.__version__
    r["hip"] = getattr(torch.version, "hip", None)
    r["cuda_build"] = getattr(torch.version, "cuda", None)
    r["gpu"] = bool(torch.cuda.is_available())
    if r["gpu"]:
        r["device"] = torch.cuda.get_device_name(0)
except Exception as e:  # noqa: BLE001
    r["error"] = (type(e).__name__ + ": " + str(e))[:300]
print(json.dumps(r))
"""

VENV_NAMES = ("venv", ".venv", ".rocm", "env", ".env", "venv-rocm", "rocm-venv")


def os_info():
    info = {"id": "", "like": "", "name": "Linux"}
    try:
        for line in open("/etc/os-release"):
            k, _, v = line.strip().partition("=")
            v = v.strip('"')
            if k == "ID":
                info["id"] = v
            elif k == "ID_LIKE":
                info["like"] = v
            elif k == "PRETTY_NAME":
                info["name"] = v
    except OSError:
        pass
    ids = f"{info['id']} {info['like']}"
    info["family"] = ("debian" if any(x in ids for x in ("debian", "ubuntu")) else
                      "fedora" if any(x in ids for x in ("fedora", "rhel", "centos")) else
                      "arch" if "arch" in ids else "other")
    return info


def install_hint(package):
    """Package install command for this distribution, for messages shown to the user."""
    fam = os_info()["family"]
    names = {"amd-smi": {"debian": "amd-smi-lib", "fedora": "amdsmi", "arch": "amdsmi"}}
    name = names.get(package, {}).get(fam, package)
    cmd = {"debian": "sudo apt install", "fedora": "sudo dnf install",
           "arch": "sudo pacman -S"}.get(fam, "install the package")
    return f"{cmd} {name}"


def rocm_info():
    versions = []
    for f in sorted(glob.glob("/opt/rocm*/.info/version")):
        try:
            v = open(f).read().strip()
            if v and v not in versions:
                versions.append(v)
        except OSError:
            pass
    tools = {t: shutil.which(t) or next((p for p in (f"/opt/rocm/bin/{t}",) if os.access(p, os.X_OK)), None)
             for t in ("rocminfo", "amd-smi")}
    return {"versions": versions, "tools": tools}


def _is_comfy(d):
    return os.path.isfile(os.path.join(d, "main.py")) and os.path.isdir(os.path.join(d, "comfy"))


def _venv_pythons(d):
    out = []
    for name in VENV_NAMES:
        p = os.path.join(d, name, "bin", "python")
        if os.access(p, os.X_OK):
            out.append(p)
    for cfg in glob.glob(os.path.join(d, "*", "pyvenv.cfg")):
        p = os.path.join(os.path.dirname(cfg), "bin", "python")
        if os.access(p, os.X_OK) and p not in out:
            out.append(p)
    return out


def find_comfyui(home, extra=()):
    cands = [os.path.join(home, "ComfyUI"), "/opt/ComfyUI", *extra]
    cands += glob.glob(os.path.join(home, "*", "ComfyUI"))
    cands += glob.glob(os.path.join(home, "*", "*", "ComfyUI"))
    cands += glob.glob(os.path.join(home, "*", "comfyui"))
    found, seen = [], set()
    for d in cands:
        d = os.path.realpath(d)
        if d in seen or not _is_comfy(d):
            continue
        seen.add(d)
        pys = _venv_pythons(d)
        rel = os.path.relpath(pys[0], d) if pys else "python3"
        found.append({"dir": d, "pythons": pys,
                      "command": f"{rel} main.py --listen 127.0.0.1 --port 8189",
                      "has_checkpoints": os.path.isdir(os.path.join(d, "models", "checkpoints"))})
    return found


def find_pythons(home, comfy_dirs=(), limit=14):
    cands = []
    for c in comfy_dirs:
        cands += c["pythons"]
    for pattern in ("*/bin/python", "*/*/bin/python", "*/*/*/bin/python"):
        for p in glob.glob(os.path.join(home, pattern)):
            if os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(p)), "pyvenv.cfg")):
                cands.append(p)
    for base in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
        cands += glob.glob(os.path.join(home, base, "envs", "*", "bin", "python"))
        cands += glob.glob(os.path.join(home, base, "bin", "python"))
    for p in ("/usr/bin/python3",):
        if os.access(p, os.X_OK):
            cands.append(p)
    out = []
    for p in cands:
        if os.access(p, os.X_OK) and p not in out and "/.cache/" not in p:
            out.append(p)
    return out[:limit]


def probe(python, timeout=120):
    try:
        r = subprocess.run([python, "-I", "-c", PROBE], capture_output=True, text=True, timeout=timeout)
        line = (r.stdout.strip().splitlines() or [""])[-1]
        info = json.loads(line) if line.startswith("{") else {"error": (r.stderr or r.stdout).strip()[-300:]}
    except subprocess.TimeoutExpired:
        info = {"error": "timed out while importing PyTorch"}
    except (OSError, ValueError) as e:
        info = {"error": str(e)}
    info["path"] = python
    info["usable"] = bool(info.get("torch") and info.get("hip") and info.get("gpu"))
    return info


def scan(home=None, extra_comfy=()):
    home = home or os.path.expanduser("~")
    comfy = find_comfyui(home, extra_comfy)
    pythons = find_pythons(home, comfy)
    with ThreadPoolExecutor(max_workers=4) as ex:
        probes = list(ex.map(probe, pythons))
    torch_envs = [p for p in probes if p.get("torch")]
    torch_envs.sort(key=lambda p: (not p["usable"], p["path"]))
    return {"os": os_info(), "rocm": rocm_info(), "comfyui": comfy, "torch": torch_envs,
            "checked": len(pythons)}
