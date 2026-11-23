"""Built-in PyTorch test load: runs torchload.py in the user's own PyTorch environment.

Same interface as comfy.ComfyServer: start(), stop(), alive(), run_once(). A "workload" is
{"seed": int, "steps": int}; the result is a .npy fingerprint instead of a PNG.
"""
import base64
import io
import json
import os
import select
import shlex
import signal
import subprocess
import time

import numpy as np

from .comfy import ComfyError, ComfyTimeout, FakeComfy, WaitInterrupted

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "torchload.py")


class TorchBackend:
    def __init__(self, cfg, user, log_path, log):
        self.python = cfg.get("torch_python", "")
        self.env = (cfg.get("torch_env") or "").strip()
        self.user = user
        self.log_path = log_path
        self.log = log
        self.proc = None
        self.buf = b""
        self.info = {}

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Starts the load process if needed. Returns True if a new process was started."""
        if self.alive():
            return False
        if not self.python or not os.path.isfile(self.python):
            raise ComfyError(f"PyTorch Python not found: {self.python or '(not set)'}")
        inner = (f"{self.env} exec {shlex.quote(self.python)} -I {shlex.quote(SCRIPT)}").strip()
        if os.geteuid() == 0 and self.user:
            cmd = ["runuser", "-l", self.user, "-c", inner]
        else:
            cmd = ["bash", "-c", inner]
        self.log("info", "Starting the built-in PyTorch load")
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=logf, start_new_session=True)
        self.buf = b""
        ready = self._readline(300, None)
        if not ready.get("ready"):
            err = ready.get("error", "unknown error")
            self.stop()
            raise ComfyError(f"The PyTorch load could not start: {err}")
        self.info = ready
        self.log("info", f"PyTorch load ready on {ready.get('device')} "
                         f"(torch {ready.get('torch')}, HIP {ready.get('hip')})")
        return True

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.write(b'{"cmd": "quit"}\n')
            self.proc.stdin.flush()
            self.proc.wait(5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc = None

    def _readline(self, timeout, poll_cb):
        t0 = time.time()
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            if poll_cb:
                r = poll_cb()
                if r:
                    raise WaitInterrupted(*r)
            if time.time() - t0 > timeout:
                raise ComfyTimeout(f"The PyTorch load did not answer within {int(timeout)} s")
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    raise ComfyError("The PyTorch load process stopped unexpectedly "
                                     f"(see {self.log_path})")
                self.buf += chunk
            elif not self.alive():
                raise ComfyError("The PyTorch load process died while running")
        line, _, self.buf = self.buf.partition(b"\n")
        try:
            return json.loads(line)
        except ValueError:
            return {"ok": False, "error": line[:300].decode(errors="replace")}

    def request(self, obj, timeout, poll_cb=None):
        if not self.alive():
            raise ComfyError("The PyTorch load is not running")
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise ComfyError(f"Could not talk to the PyTorch load: {e}")
        resp = self._readline(timeout, poll_cb)
        if not resp.get("ok"):
            raise ComfyError(f"PyTorch load error: {resp.get('error')}")
        return resp

    def calibrate(self, target_s):
        return int(self.request({"cmd": "calibrate", "target_s": target_s}, 600)["steps"])

    def run_once(self, workload, timeout, poll_cb, must_run=None):
        try:
            resp = self.request({"cmd": "run", "seed": workload["seed"], "steps": workload["steps"]},
                                timeout, poll_cb)
        except (WaitInterrupted, ComfyTimeout):
            # the process may still be calculating; restart it so a late answer is never
            # mistaken for the next one
            self.stop()
            raise
        return base64.b64decode(resp["data"]), float(resp["secs"])


class FakeTorch(FakeComfy):
    """Simulated PyTorch load for UVTUNE_FAKE=1: returns .npy fingerprints."""

    def calibrate(self, target_s):
        return 10

    def run_once(self, workload, timeout, poll_cb, must_run=None):
        data, secs = super().run_once(workload, timeout, poll_cb, must_run)
        arr = np.asarray(__import__("PIL.Image", fromlist=["Image"]).open(io.BytesIO(data)),
                         dtype=np.float32) / 255.0
        buf = io.BytesIO()
        np.save(buf, arr.reshape(-1)[:4096], allow_pickle=False)
        return buf.getvalue(), secs
