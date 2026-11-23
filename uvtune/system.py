"""Systemting: boot-id, kernel-log, LACT, watchdog, genstart."""
import os
import re
import subprocess
import threading
import time

GPU_ERR = re.compile(
    r"amdgpu.*(ring \S+ timeout|gpu reset|gpu recover|guilty|page fault|reset failed|"
    r"\bhang\b|mes failed|ring test failed|failed to .*idle|protection fault|device lost)",
    re.I)


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _journal(args):
    try:
        r = subprocess.run(["journalctl", "-k", "-q", "--no-pager", "-o", "short-iso"] + args,
                           capture_output=True, text=True, timeout=30)
        return r.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []


class RealSystem:
    fake = False

    def gpu_errors_since(self, epoch):
        lines = _journal(["--since", f"@{int(epoch)}"])
        return [ln for ln in lines if GPU_ERR.search(ln)]

    def previous_boot_log(self):
        lines = _journal(["-b", "-1"])
        hits = [ln for ln in lines if GPU_ERR.search(ln)]
        tail = lines[-60:]
        return hits, tail

    def run(self, *cmd):
        try:
            return subprocess.run(list(cmd), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def lactd_active(self):
        r = self.run("systemctl", "is-active", "lactd")
        return bool(r and r.stdout.strip() == "active")

    def stop_lactd(self):
        self.run("systemctl", "stop", "lactd")

    def start_lactd(self):
        self.run("systemctl", "start", "lactd")

    def system_stopping(self):
        r = self.run("systemctl", "is-system-running")
        return bool(r and r.stdout.strip() == "stopping")

    def reboot(self):
        os.sync()
        self.run("systemctl", "reboot")

    def poweroff(self):
        os.sync()
        self.run("systemctl", "poweroff")

    # --- auto-genstart ved frys ---
    def read_sysctl(self, name):
        try:
            with open(f"/proc/sys/{name.replace('.', '/')}") as f:
                return f.read().strip()
        except OSError:
            return None

    def write_sysctl(self, name, value):
        try:
            with open(f"/proc/sys/{name.replace('.', '/')}", "w") as f:
                f.write(str(value))
            return True
        except OSError:
            return False


class Watchdog:
    """Holder hardware-watchdoggen i live. Fryser kernen helt, genstarter bundkortet maskinen."""

    def __init__(self, log):
        self.log = log
        self.fd = None
        self._stop = threading.Event()

    def start(self):
        subprocess.run(["modprobe", "sp5100_tco"], capture_output=True)
        try:
            self.fd = os.open("/dev/watchdog", os.O_WRONLY)
        except OSError as e:
            self.log("warn", f"Hardware-watchdog ikke tilgængelig ({e}). Ved total frys skal du selv trykke reset.")
            return False
        threading.Thread(target=self._pet, daemon=True).start()
        self.log("info", "Hardware-watchdog aktiv: maskinen genstarter selv, hvis den fryser helt.")
        return True

    def _pet(self):
        while not self._stop.is_set():
            try:
                os.write(self.fd, b"\0")
            except OSError:
                pass
            time.sleep(5)

    def stop(self):
        self._stop.set()
        if self.fd is not None:
            try:
                os.write(self.fd, b"V")  # magic close: slå watchdog fra
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class FakeSystem:
    fake = True

    def __init__(self):
        self.kernel = []

    def gpu_errors_since(self, epoch):
        return [ln for t, ln in self.kernel if t >= epoch]

    def previous_boot_log(self):
        return [], []

    def lactd_active(self):
        return False

    def stop_lactd(self):
        pass

    def start_lactd(self):
        pass

    def reboot(self):
        pass

    def poweroff(self):
        print("[fake] poweroff", flush=True)

    def system_stopping(self):
        return False

    def read_sysctl(self, name):
        return None

    def write_sysctl(self, name, value):
        return True


def open_system():
    return FakeSystem() if os.environ.get("UVTUNE_FAKE") else RealSystem()
