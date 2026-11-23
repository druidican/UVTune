"""Adgang til amdgpu via sysfs: sensorer, voltage offset og power limit.

Skrivning kræver root og at overdrive er slået til
(amdgpu.ppfeaturemask med bit 0x4000, fx via LACT's "Enable overclocking").
"""
import glob
import math
import os
import random
import re
import time

OD_FILE = "pp_od_clk_voltage"


class GPUError(Exception):
    pass


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _int(path):
    v = _read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_od(text):
    info = {"od_available": text is not None, "vo_supported": False,
            "vo_current": None, "vo_min": None, "vo_max": None}
    if not text:
        return info
    m = re.search(r"OD_VDDGFX_OFFSET:\s*\n\s*(-?\d+)\s*mV", text, re.I)
    if m:
        info["vo_supported"] = True
        info["vo_current"] = int(m.group(1))
    r = re.search(r"^\s*VDDGFX_OFFSET:\s*(-?\d+)\s*mV\s+(-?\d+)\s*mV", text, re.I | re.M)
    if r:
        info["vo_min"], info["vo_max"] = int(r.group(1)), int(r.group(2))
    return info


class AmdGpu:
    def __init__(self, sysfs="/sys/class/drm"):
        self.dev = self._find(sysfs)
        hw = sorted(glob.glob(os.path.join(self.dev, "hwmon", "hwmon*")))
        if not hw:
            raise GPUError("Fandt ikke hwmon-sensorer for grafikkortet")
        self.hwmon = hw[0]
        self.name = _read(os.path.join(self.dev, "product_name")) or \
            f"AMD GPU {_read(os.path.join(self.dev, 'device'), '?')}"
        self._sensors = self._scan_sensors()

    @staticmethod
    def _find(sysfs):
        best, best_vram = None, -1
        for card in sorted(glob.glob(os.path.join(sysfs, "card*"))):
            if not re.fullmatch(r"card\d+", os.path.basename(card)):
                continue
            dev = os.path.realpath(os.path.join(card, "device"))
            if _read(os.path.join(dev, "vendor")) != "0x1002":
                continue
            vram = _int(os.path.join(dev, "mem_info_vram_total")) or 0
            if vram > best_vram:
                best, best_vram = dev, vram
        if not best:
            raise GPUError("Fandt intet AMD-grafikkort")
        return best

    def _scan_sensors(self):
        s = []
        hw = self.hwmon
        pw = os.path.join(hw, "power1_average")
        if not os.path.exists(pw):
            pw = os.path.join(hw, "power1_input")
        if os.path.exists(pw):
            s.append(("power_w", pw, 1e-6))
        for f in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            label = _read(f.replace("_input", "_label")) or os.path.basename(f)
            s.append((f"temp_{label}", f, 1e-3))
        for f in sorted(glob.glob(os.path.join(hw, "freq*_input"))):
            label = _read(f.replace("_input", "_label")) or os.path.basename(f)
            s.append((f"{label}_mhz", f, 1e-6))
        for f in sorted(glob.glob(os.path.join(hw, "in*_input"))):
            label = _read(f.replace("_input", "_label")) or os.path.basename(f)
            s.append((f"{label}_mv", f, 1.0))
        fan = os.path.join(hw, "fan1_input")
        if os.path.exists(fan):
            s.append(("fan_rpm", fan, 1.0))
        busy = os.path.join(self.dev, "gpu_busy_percent")
        if os.path.exists(busy):
            s.append(("busy_pct", busy, 1.0))
        vram = os.path.join(self.dev, "mem_info_vram_used")
        if os.path.exists(vram):
            s.append(("vram_mb", vram, 1 / 1048576))
        return s

    def sample(self):
        out = {"t": time.time()}
        for key, path, scale in self._sensors:
            v = _int(path)
            out[key] = round(v * scale, 2) if v is not None else None
        return out

    # --- overdrive / spænding ---
    @property
    def od_path(self):
        return os.path.join(self.dev, OD_FILE)

    def od_info(self):
        info = parse_od(_read(self.od_path))
        info["writable"] = os.access(self.od_path, os.W_OK)
        return info

    def _od_write(self, cmd):
        with open(self.od_path, "w") as f:
            f.write(cmd + "\n")

    def set_voltage_offset(self, mv):
        self._od_write(f"vo {int(mv)}")
        self._od_write("c")
        cur = self.od_info().get("vo_current")
        if cur is not None and cur != int(mv):
            raise GPUError(f"Kortet accepterede ikke {mv} mV (viser {cur} mV)")

    def reset_od(self):
        self._od_write("r")
        self._od_write("c")

    # --- power limit ---
    def power_info(self):
        hw = self.hwmon

        def w(name):
            v = _int(os.path.join(hw, name))
            return round(v / 1e6, 1) if v is not None else None
        return {"cap_w": w("power1_cap"), "min_w": w("power1_cap_min"),
                "max_w": w("power1_cap_max"), "default_w": w("power1_cap_default"),
                "writable": os.access(os.path.join(hw, "power1_cap"), os.W_OK)}

    def set_power_cap(self, watts):
        with open(os.path.join(self.hwmon, "power1_cap"), "w") as f:
            f.write(str(int(round(watts * 1e6))))

    def reset_power_cap(self):
        d = self.power_info()["default_w"]
        if d:
            self.set_power_cap(d)


class FakeGpu:
    """Simuleret kort til demo og test (UVTUNE_FAKE=1)."""
    fake = True

    def __init__(self):
        self.name = "Simuleret RX 9070 XT"
        self.offset = 0
        self.cap = 304.0
        self.dev = "/fake"

    def sample(self):
        load = 1.0
        pw = min(self.cap, 304) * (1 - 0.0016 * -self.offset) * load
        clk = 2970 * (min(self.cap, 304) / 304) ** 0.35 * (1 + 0.0006 * -self.offset)
        return {"t": time.time(), "power_w": round(pw + random.uniform(-4, 4), 1),
                "temp_edge": round(55 + pw / 12, 1), "temp_junction": round(62 + pw / 8, 1),
                "temp_mem": 70.0, "sclk_mhz": round(clk + random.uniform(-20, 20)),
                "mclk_mhz": 1258, "vddgfx_mv": round(1050 + self.offset + 20 * math.sin(time.time())),
                "fan_rpm": round(800 + pw * 3.2), "busy_pct": 99, "vram_mb": 11000}

    def od_info(self):
        return {"od_available": True, "vo_supported": True, "vo_current": self.offset,
                "vo_min": -200, "vo_max": 0, "writable": True}

    def set_voltage_offset(self, mv):
        self.offset = int(mv)

    def reset_od(self):
        self.offset = 0

    def power_info(self):
        return {"cap_w": self.cap, "min_w": 210.0, "max_w": 334.0, "default_w": 304.0,
                "writable": True}

    def set_power_cap(self, watts):
        self.cap = float(watts)

    def reset_power_cap(self):
        self.cap = 304.0

    # simuleret ustabilitet: lavere power limit tåler lidt mere undervolt
    def failure_mode(self):
        slack = (304 - min(self.cap, 304)) * 0.15
        if self.offset < -115 - slack:
            return "gpu_reset"
        if self.offset < -95 - slack:
            return "mismatch"
        return None


def open_gpu():
    if os.environ.get("UVTUNE_FAKE"):
        return FakeGpu()
    return AmdGpu()
