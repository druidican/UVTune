"""Controls a ComfyUI test instance and runs the workflow through its API."""
import copy
import io
import json
import os
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import numpy as np
from PIL import Image


class ComfyError(Exception):
    pass


class ComfyTimeout(ComfyError):
    pass


class WaitInterrupted(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def validate_workflow(wf):
    """Returns an error text or None."""
    if not isinstance(wf, dict):
        return "The workflow file is not a JSON object."
    if "nodes" in wf and "links" in wf:
        return ("The file is in the normal UI format. Use 'Export (API)' in ComfyUI "
                "and choose that file instead.")
    if not wf or not all(isinstance(v, dict) and "class_type" in v for v in wf.values()):
        return "The file does not look like a ComfyUI API workflow."
    classes = {v["class_type"] for v in wf.values()}
    if not any("Save" in c or "Preview" in c for c in classes):
        return "The workflow has no Save Image or Preview Image node."
    return None


SEED_KEYS = ("seed", "noise_seed")


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)


def seed_variants(wf):
    """Make two versions of the workflow, each with its own seed (A and B).

    ComfyUI only keeps the latest result per node, so when the test alternates A, B, A, B,
    the sampler has to calculate every image from scratch while the models stay in memory.
    Returns (wf_a, wf_b, sampler_node, description) or (None, None, None, reason).
    """
    consumers = {}
    for nid, node in wf.items():
        for k, v in node.get("inputs", {}).items():
            if _is_link(v):
                consumers.setdefault(v[0], []).append((nid, k))

    ancestors_cache = {}

    def ancestors(nid, seen=None):
        if nid in ancestors_cache:
            return ancestors_cache[nid]
        seen = seen or set()
        out = set()
        for v in wf.get(nid, {}).get("inputs", {}).values():
            if _is_link(v) and v[0] not in seen:
                seen.add(v[0])
                out |= {v[0]} | ancestors(v[0], seen)
        ancestors_cache[nid] = out
        return out

    samplers = [nid for nid, n in wf.items()
                if "sampler" in n.get("class_type", "").lower()
                and any(k in n.get("inputs", {}) for k in SEED_KEYS)]
    samplers.sort(key=lambda nid: len(ancestors(nid)))
    for sid in samplers:
        inputs = wf[sid]["inputs"]
        for k in SEED_KEYS:
            if k not in inputs:
                continue
            v = inputs[k]
            target = None
            if isinstance(v, int) and not isinstance(v, bool):
                target = (sid, k, v)
            elif _is_link(v):
                src = v[0]
                if all(ck in SEED_KEYS for _, ck in consumers.get(src, [])):
                    lits = [(kk, vv) for kk, vv in wf.get(src, {}).get("inputs", {}).items()
                            if isinstance(vv, int) and not isinstance(vv, bool)]
                    if len(lits) == 1:
                        target = (src, lits[0][0], lits[0][1])
            if target:
                nid, key, val = target
                b = copy.deepcopy(wf)
                b[nid]["inputs"][key] = val + 1 if val < 2 ** 63 - 2 else val - 1
                desc = (f"The test alternates between seed {val} and {b[nid]['inputs'][key]} "
                        f"(node {nid}, {wf[nid].get('class_type')}), so the models stay in memory.")
                return wf, b, sid, desc
    return None, None, None, ("Found no fixed seed that can be alternated. Add --cache-none to "
                              "the start command (slower: all models are reloaded for every image).")


def _pick_image(entry, workflow):
    """Always pick the same image: preferably from a Save node, otherwise the lowest node id."""
    outs = [(nid, o) for nid, o in entry.get("outputs", {}).items() if o.get("images")]

    def rank(item):
        nid, _ = item
        cls = workflow.get(nid, {}).get("class_type", "")
        num = int(nid) if str(nid).isdigit() else 10 ** 9
        return (0 if "Save" in cls else 1, num, str(nid))
    outs.sort(key=rank)
    return outs[0][1]["images"][0] if outs else None


class ComfyServer:
    def __init__(self, cfg, user, log_path, log):
        self.cfg = cfg
        self.user = user
        self.log_path = log_path
        self.log = log
        self.proc = None
        self.base = f"http://127.0.0.1:{int(cfg['comfy_port'])}"

    # --- HTTP ---
    def _req(self, path, data=None, timeout=30):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _json(self, path, data=None, timeout=30):
        return json.loads(self._req(path, data, timeout))

    def api_ok(self):
        try:
            self._json("/system_stats", timeout=5)
            return True
        except Exception:
            return False

    # --- proces ---
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Starts ComfyUI if needed. Returns True if a new process was started."""
        if self.alive() and self.api_ok():
            return False
        self.stop()
        inner = f"cd {shlex.quote(self.cfg['comfy_cwd'])} && exec {self.cfg['comfy_command']}"
        if os.geteuid() == 0 and self.user:
            cmd = ["runuser", "-l", self.user, "-c", inner]
        else:
            cmd = ["bash", "-c", inner]
        self.log("info", "Starting ComfyUI test instance")
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + float(self.cfg["comfy_startup_timeout_s"])
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ComfyError(f"ComfyUI stopped during startup (code {self.proc.returncode}). "
                                 f"See {self.log_path}")
            if self.api_ok():
                self.log("info", "ComfyUI is ready")
                return True
            time.sleep(2)
        raise ComfyError("ComfyUI did not become ready in time")

    def stop(self):
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(20)
        except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc = None

    # --- run one image ---
    def run_once(self, workflow, timeout, poll_cb, must_run=None):
        cid = uuid.uuid4().hex
        try:
            resp = self._json("/prompt", {"prompt": workflow, "client_id": cid})
        except urllib.error.HTTPError as e:
            raise ComfyError(f"ComfyUI rejected the workflow: {e.read()[:800].decode(errors='replace')}")
        except Exception as e:
            raise ComfyError(f"Could not send workflow: {e}")
        pid = resp.get("prompt_id")
        if resp.get("node_errors"):
            raise ComfyError(f"Node errors: {json.dumps(resp['node_errors'])[:800]}")
        t0 = time.time()
        while True:
            time.sleep(1)
            if not self.alive():
                raise ComfyError("The ComfyUI process died while running")
            r = poll_cb()
            if r:
                raise WaitInterrupted(*r)
            if time.time() - t0 > timeout:
                raise ComfyTimeout(f"The image did not finish within {int(timeout)} s")
            try:
                hist = self._json(f"/history/{pid}", timeout=10)
            except Exception:
                continue
            entry = hist.get(pid)
            if not entry:
                continue
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                detail = msgs[0][1].get("exception_message", "") if msgs else ""
                raise ComfyError(f"Error during generation: {detail}".strip())
            if status.get("completed") or entry.get("outputs"):
                break
        elapsed = time.time() - t0
        cached = set()
        for m in entry.get("status", {}).get("messages", []):
            if m and m[0] == "execution_cached":
                cached.update(str(n) for n in m[1].get("nodes", []))
        if (must_run and must_run in cached) or (cached and set(workflow.keys()) <= cached):
            raise ComfyError("ComfyUI reused a cached result instead of calculating the image. "
                             "Remove --cache-lru/--cache-ram from the start command.")
        im = _pick_image(entry, workflow)
        if not im:
            raise ComfyError("The workflow produced no image")
        q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                    "type": im.get("type", "output")})
        return self._req(f"/view?{q}", timeout=60), elapsed


class FakeComfy:
    """Simulated ComfyUI: about 2 s per image, affected by the simulated GPU."""

    def __init__(self, gpu, system):
        self.gpu = gpu
        self.system = system
        self._alive = False
        self._imgs = {}

    def alive(self):
        return self._alive

    def start(self):
        fresh = not self._alive
        self._alive = True
        return fresh

    def stop(self):
        self._alive = False

    def _img(self, workflow):
        key = json.dumps(workflow, sort_keys=True)
        if key not in self._imgs:
            rng = np.random.default_rng(abs(hash(key)) % 2 ** 32)
            self._imgs[key] = (rng.random((128, 128, 3)) * 255).astype(np.uint8)
        return self._imgs[key]

    def run_once(self, workflow, timeout, poll_cb, must_run=None):
        t0 = time.time()
        clk = self.gpu.sample()["sclk_mhz"]
        dur = 2.0 * 2970 / clk
        while time.time() - t0 < dur:
            time.sleep(0.2)
            r = poll_cb()
            if r:
                raise WaitInterrupted(*r)
        mode = self.gpu.failure_mode()
        if mode == "gpu_reset" and np.random.random() < 0.5:
            self.system.kernel.append((time.time() - 1,
                                       "kernel: amdgpu 0000:03:00.0: [drm] *ERROR* ring gfx_0.0.0 timeout"))
            self._alive = False
            raise ComfyError("HIP error: the operation cannot be performed in the present state")
        if mode == "mismatch" and np.random.random() < 0.3:
            bad = self._img(workflow).copy()
            bad[10:20, 10:20] = 0
            return _to_png(bad), time.time() - t0
        return _to_png(self._img(workflow)), time.time() - t0


def _to_png(arr):
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "PNG")
    return b.getvalue()
