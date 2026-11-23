"""UV Tune worker. Kører som root via systemd og overlever genstarter.

Før hvert trin gemmes "nu tester jeg trin X" på disken. Hvis maskinen fryser eller
genstarter, ser workeren ved næste boot, at trin X ikke blev færdigt, og noterer det
som ustabilt. Overdrive-indstillinger nulstilles altid af kernen ved genstart.
"""
import math
import os
import signal
import statistics
import sys
import threading
import time
import traceback

from . import storage as S
from .comfy import (ComfyError, ComfyServer, ComfyTimeout, FakeComfy, WaitInterrupted,
                    seed_variants, validate_workflow)
from .config import merged, parse_caps
from .gpu import open_gpu
from .imagecmp import decode, fmt_psnr, pixel_hash, psnr
from . import report
from .reco import recommend
from .search import Search
from .system import Watchdog, boot_id, open_system

FAKE = bool(os.environ.get("UVTUNE_FAKE"))


PHASE_MINUTES = {"baseline": "warmup_minutes", "warmup": "warmup_minutes",
                 "coarse": "screen_minutes", "up": "screen_minutes", "fine": "screen_minutes",
                 "confirm": "confirm_minutes", "verify": "verify_minutes"}
PHASE_NAMES = {"baseline": "Reference + opvarmning", "warmup": "Opvarmning",
               "coarse": "Screening (grov)", "up": "Screening (op)", "fine": "Screening (fin)",
               "confirm": "Bekræftelse", "verify": "Verifikation"}


class Abort(Exception):
    pass


class Terminate(BaseException):
    """Tjenesten stoppes (systemctl stop, afinstallation eller nedlukning)."""


def _on_sigterm(signum, frame):
    raise Terminate()


def call_timeout(fn, secs):
    """Kør fn i en tråd. Returnerer None ved succes, fejltekst, eller 'timeout'."""
    res = {}

    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            res["err"] = f"{type(e).__name__}: {e}"
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        return "timeout"
    return res.get("err")


class Sampler(threading.Thread):
    def __init__(self, gpu, path):
        super().__init__(daemon=True)
        self.gpu, self.path = gpu, path
        self.samples = []
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                s = self.gpu.sample()
                self.samples.append(s)
                S.append_jsonl(self.path, s)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()
        self.join(5)


def step_stats(samples, img_times):
    def vals(k, pred=None):
        return [s[k] for s in samples if s.get(k) is not None and (pred is None or pred(s))]

    def avg(v):
        return round(statistics.fmean(v), 1) if v else None
    busy = lambda s: (s.get("busy_pct") or 0) >= 50  # noqa: E731
    power = avg(vals("power_w"))
    times = img_times[1:] if len(img_times) > 2 else img_times
    spi = statistics.median(times) if times else None
    return {
        "sec_per_img": round(spi, 2) if spi else None,
        "img_per_hour": round(3600 / spi, 1) if spi else None,
        "avg_power_w": power,
        "wh_per_img": round(power * spi / 3600, 3) if power and spi else None,
        "avg_sclk_mhz": avg(vals("sclk_mhz", busy)) or avg(vals("sclk_mhz")),
        "avg_vddgfx_mv": avg(vals("vddgfx_mv", busy)) or avg(vals("vddgfx_mv")),
        "max_junction_c": max(vals("temp_junction"), default=None),
        "avg_edge_c": avg(vals("temp_edge")),
        "avg_fan_rpm": avg(vals("fan_rpm")),
        "max_fan_rpm": max(vals("fan_rpm"), default=None),
    }


class Worker:
    def __init__(self):
        S.ensure_dir(S.DATA)
        S.ensure_dir(S.RUNS)
        self.gpu = open_gpu()
        self.sys = open_system()
        self.comfy = None
        self.workflow = None
        self.variants = []
        self.must_run = None
        self.vi = 0
        self.watchdog = None
        self.prepared = False
        self._last_kcheck = 0
        self._errors = 0
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # ---------- hjælpere ----------
    def _heartbeat(self):
        while True:
            try:
                S.write_json(S.HEARTBEAT, {"t": time.time(), "pid": os.getpid(), "fake": FAKE,
                                           "gpu": self.gpu.name, "root": os.geteuid() == 0})
            except OSError:
                pass
            time.sleep(3)

    def event(self, st, level, msg, **extra):
        rec = {"t": time.time(), "level": level, "msg": msg, **extra}
        path = (S.Path(st["run_dir"]) / "events.jsonl") if st and st.get("run_dir") else \
            S.DATA / "events.jsonl"
        S.append_jsonl(path, rec, sync=True)
        print(f"[{level}] {msg}", flush=True)

    def save(self, st):
        S.write_json(S.STATE, st)

    def _read_command(self):
        cmd = S.read_json(S.COMMAND)
        if cmd is not None:
            try:
                os.remove(S.COMMAND)
            except OSError:
                pass
        return cmd

    def check_abort(self, st):
        cmd = self._read_command()
        if not cmd:
            return
        if cmd.get("action") == "abort":
            raise Abort()
        self.event(st, "warn", "En test kører allerede. Startkommandoen blev ignoreret.")

    # ---------- hovedløkke ----------
    def loop(self):
        self.event(None, "info", f"Worker startet ({'SIMULERING' if FAKE else self.gpu.name})")
        try:
            self._loop()
        except Terminate:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            self.terminate()

    def _loop(self):
        while True:
            st = S.read_json(S.STATE) or {}
            try:
                cmd = self._read_command()
                if cmd:
                    st = self.handle_command(cmd, st)
                if st.get("active"):
                    try:
                        self.continue_run(st)
                    except Abort:
                        self.finish(st, "aborted", "Afbrudt af brugeren")
                    self._errors = 0
                else:
                    time.sleep(1)
            except Exception as e:  # noqa: BLE001
                self._errors += 1
                self.event(st, "error", f"Intern fejl: {e}", trace=traceback.format_exc())
                if st.get("active") and self._errors >= 3:
                    self.finish(st, "error", f"Stoppet efter gentagne interne fejl: {e}")
                time.sleep(5)

    def handle_command(self, cmd, st):
        action = cmd.get("action")
        if action == "start":
            if st.get("active"):
                self.event(st, "warn", "En test kører allerede.")
                return st
            return self.new_run(cmd)
        if action == "abort" and st.get("active"):
            self.finish(st, "aborted", "Afbrudt af brugeren")
            return S.read_json(S.STATE) or {}
        return st

    def new_run(self, cmd):
        cfg = merged(cmd.get("config"))
        wf = cmd.get("workflow")
        run_id = time.strftime("%Y%m%d-%H%M%S")
        run_dir = S.ensure_dir(S.RUNS / run_id)
        st = {"active": False, "run_id": run_id, "run_dir": str(run_dir), "cfg": cfg,
              "status": "error", "created": time.time()}

        def fail(msg):
            st["message"] = msg
            self.event(st, "error", msg)
            self.save(st)
            return st
        err = validate_workflow(wf)
        if err:
            return fail(err)
        if os.geteuid() != 0 and not FAKE:
            return fail("Workeren kører ikke som root og kan ikke ændre kortets indstillinger.")
        od = self.gpu.od_info()
        if not od["vo_supported"]:
            return fail("Kortet viser ingen OD_VDDGFX_OFFSET. Slå overclocking til i LACT "
                        "(eller amdgpu.ppfeaturemask=0xffffffff) og genstart.")
        pi = self.gpu.power_info()
        default = pi["default_w"] or pi["cap_w"]
        caps = []
        try:
            pcts = parse_caps(cfg["power_caps_pct"])
        except ValueError:
            return fail("Power limits skal skrives som procenttal adskilt af komma, fx 100, 90, 80")
        for p in pcts:
            w = default * p / 100
            if pi["min_w"]:
                w = max(w, pi["min_w"])
            if pi["max_w"]:
                w = min(w, pi["max_w"])
            w = round(w)
            if w not in caps:
                caps.append(w)
        if not caps:
            return fail("Ingen power limits at teste")
        floor = int(cfg["min_offset_mv"])
        if od.get("vo_min") is not None:
            floor = max(floor, od["vo_min"])
        S.write_json(run_dir / "workflow.json", wf)
        st.update({
            "active": True, "status": "between", "message": "",
            "default_power_w": default, "power_info": pi, "od_info": od, "gpu_name": self.gpu.name,
            "search": Search.new(caps, cfg, floor).to_dict(), "step_n": 0,
            "consecutive_crashes": 0, "ref": None, "current": None,
        })
        self.prepared = False
        self.save(st)
        self.event(st, "info", f"Ny test startet. Power limits: {', '.join(f'{c} W' for c in caps)}. "
                               f"Laveste offset: {floor} mV.")
        return st

    # ---------- forberedelse pr. boot ----------
    def prepare(self, st):
        cfg = st["cfg"]
        if cfg["stop_lactd"] and self.sys.lactd_active():
            self.sys.stop_lactd()
            st["lactd_was_active"] = True
            self.event(st, "info", "LACT-tjenesten er sat på pause under testen.")
        if cfg["auto_reboot_on_freeze"] and not FAKE:
            if "sysctl_saved" not in st:
                st["sysctl_saved"] = {k: self.sys.read_sysctl(k)
                                      for k in ("kernel.panic", "kernel.softlockup_panic")}
            self.sys.write_sysctl("kernel.panic", 10)
            self.sys.write_sysctl("kernel.softlockup_panic", 1)
            self.watchdog = Watchdog(lambda lvl, m: self.event(st, lvl, m))
            self.watchdog.start()
        self.workflow = S.read_json(S.Path(st["run_dir"]) / "workflow.json")
        a, b, sampler, desc = seed_variants(self.workflow)
        if a is not None:
            self.variants, self.must_run = [a, b], sampler
            if not st.get("variant_note"):
                st["variant_note"] = desc
                self.event(st, "info", desc)
            cmd = cfg["comfy_command"]
            if "--cache-none" in cmd.split():
                cfg = dict(cfg, comfy_command=" ".join(x for x in cmd.split() if x != "--cache-none"))
                self.event(st, "info", "Kører ComfyUI uden --cache-none, så modellerne bliver i hukommelsen.")
        else:
            self.variants, self.must_run = [self.workflow], None
            self.event(st, "warn", desc)
        if FAKE:
            self.comfy = FakeComfy(self.gpu, self.sys)
        else:
            self.comfy = ComfyServer(cfg, S.OWNER, S.Path(st["run_dir"]) / "comfyui.log",
                                     lambda lvl, m: self.event(st, lvl, m))
        self.save(st)
        self.prepared = True

    def continue_run(self, st):
        cfg = st["cfg"]
        if not self.prepared:
            bid = boot_id()
            if st.get("status") in ("running", "rebooting") and st.get("boot_id") != bid:
                if st["status"] == "running":
                    self.record_hard_crash(st)
                else:
                    self.event(st, "info", "Maskinen er genstartet efter en GPU-fejl. Fortsætter.")
                st["status"] = "between"
                st["needs_warmup"] = True
                self.save(st)
                self.grace(st)
            elif st.get("status") == "running":
                self.event(st, "warn", "Workeren blev genstartet midt i et trin. Trinnet køres forfra.")
                st["status"] = "between"
                st["step_n"] = max(0, st.get("step_n", 1) - 1)
            self.prepare(st)
        if st.get("consecutive_crashes", 0) >= int(cfg["max_consecutive_crashes"]):
            self.finish(st, "error", "Den samme indstilling crashede igen og igen. "
                                     "Testen er stoppet for en sikkerheds skyld.")
            return
        if st.get("total_crashes", 0) >= int(cfg.get("max_total_crashes", 40)):
            self.finish(st, "error", "Testen har ramt det samlede loft for antal crashes og er stoppet.")
            return
        search = Search(st["search"])
        if search.done:
            if search.d.get("error"):
                self.finish(st, "error", search.d["error"])
            else:
                self.finish(st, "finished", "Testen er færdig.")
            return
        step = search.current()
        if st.get("needs_warmup") and (st.get("ref") or {}).get("mode"):
            # kortet er koldt efter genstart: varm op på standard spænding først
            self.run_step(st, {"cap_w": step["cap_w"], "offset_mv": 0, "phase": "warmup"})
            return
        self.run_step(st, step)

    def grace(self, st):
        secs = int(st["cfg"]["boot_grace_s"])
        st["grace_until"] = time.time() + secs
        self.save(st)
        self.event(st, "info", f"Venter {secs} s, før testen fortsætter (du kan afbryde i appen).")
        end = time.time() + secs
        while time.time() < end:
            self.check_abort(st)
            time.sleep(1)
        st.pop("grace_until", None)

    # ---------- et trin ----------
    def _poll(self, st, t0):
        self.check_abort(st)
        now = time.time()
        if now - self._last_kcheck >= 5:
            self._last_kcheck = now
            hits = self.sys.gpu_errors_since(t0)
            if hits:
                return ("gpu_reset", "\n".join(hits[:15]))
        return None

    def _apply(self, cap, off):
        def go():
            self.gpu.reset_od()
            self.gpu.set_power_cap(cap)
            if off:
                self.gpu.set_voltage_offset(off)
        return call_timeout(go, 30)

    def run_step(self, st, step):
        cfg = st["cfg"]
        run_dir = S.Path(st["run_dir"])
        n = st["step_n"] + 1
        st["step_n"] = n
        phase, cap, off = step["phase"], step["cap_w"], step["offset_mv"]
        minutes = float(cfg[PHASE_MINUTES[phase]])
        dur_s = minutes * 60
        names = PHASE_NAMES
        self.event(st, "info", f"Trin {n}: {names[phase]} – {cap} W, {off} mV, {minutes:g} min")

        err = self._apply(cap, off)
        if err:
            if err == "timeout":
                return self._gpu_dead(st, n, step, "Kortet svarede ikke, da indstillingerne blev sat")
            self.finish(st, "error", f"Kunne ikke sætte indstillinger: {err}")
            return

        samples_file = run_dir / f"samples_{n:03d}.jsonl"
        t0 = time.time()
        st["current"] = {"n": n, **step, "started": t0, "ends_at": t0 + dur_s, "duration_s": dur_s,
                         "samples_file": str(samples_file), "images": 0, "img_times": [],
                         "psnrs": [], "last_img_s": None}
        st["status"] = "running"
        st["boot_id"] = boot_id()
        self.save(st)

        sampler = Sampler(self.gpu, samples_file)
        sampler.start()
        fail = None
        try:
            fresh = False
            try:
                fresh = bool(self.comfy.start())
            except ComfyError as e:
                fail = ("comfyui_error", str(e))
            ref = st.get("ref") or {}
            timeout = 900.0
            if ref.get("sec_per_img"):
                timeout = max(120.0, float(cfg["image_timeout_factor"]) * ref["sec_per_img"])
            cur = st["current"]
            min_base = 2 * len(self.variants) + (1 if fresh else 0)
            while fail is None:
                elapsed = time.time() - t0
                if elapsed >= dur_s and not (phase == "baseline" and cur["images"] < min_base):
                    break
                self.check_abort(st)
                vi = self.vi % len(self.variants)
                self.vi += 1
                try:
                    data, secs = self.comfy.run_once(
                        self.variants[vi], 900.0 if fresh else timeout,
                        lambda: self._poll(st, t0), must_run=self.must_run)
                except WaitInterrupted as e:
                    fail = (e.reason, e.detail)
                    break
                except ComfyTimeout as e:
                    fail = ("timeout", str(e))
                    break
                except ComfyError as e:
                    fail = ("comfyui_error", str(e))
                    break
                ok, p = self._check_image(st, n, phase, data, vi)
                cur["images"] += 1
                if fresh:
                    # første billede efter (gen)start indlæser modellerne: tæller ikke i ydelsen
                    cur["cold_img_s"] = round(secs, 2)
                    cur["warm_from"] = time.time()
                    fresh = False
                else:
                    cur["img_times"].append(round(secs, 2))
                cur["psnrs"].append(p)
                cur["last_img_s"] = round(secs, 2)
                self.save(st)
                if not ok:
                    fail = ("mismatch", f"Billedet afveg fra referencen ({fmt_psnr(p)}). "
                                        "Det tyder på regnefejl i GPU'en.")
                    break
                hits = self.sys.gpu_errors_since(t0)
                if hits:
                    fail = ("gpu_reset", "\n".join(hits[:15]))
        finally:
            sampler.stop()

        if fail and fail[0] in ("comfyui_error", "timeout"):
            time.sleep(3)
            hits = self.sys.gpu_errors_since(t0)
            if hits:
                fail = ("gpu_reset", fail[1] + "\n\nKernel-log:\n" + "\n".join(hits[:15]))

        result = self._result(st, n, step, t0, sampler.samples, fail)
        if fail and fail[0] in ("gpu_reset", "timeout"):
            log = run_dir / f"kernel_step{n:03d}.log"
            S.write_text(log, "\n".join(self.sys.gpu_errors_since(t0 - 5)))
            result["kernel_log"] = str(log)
        passed = fail is None
        self._finish_step(st, result, passed)

        if passed:
            st["consecutive_crashes"] = 0
            st["status"] = "between"
            self.save(st)
            return
        if fail[0] != "mismatch":
            self._count_crash(st, step)
        if fail[0] in ("gpu_reset", "timeout") and cfg["reboot_on_gpu_reset"] and not FAKE:
            self._reboot(st, "GPU-fejl. Genstarter for at få en ren driver")
            return
        if fail[0] != "mismatch":
            # efter en driver- eller ComfyUI-fejl startes ComfyUI forfra; efter et forkert
            # billede er driveren i orden, så modellerne bliver liggende
            call_timeout(self.comfy.stop, 30)
        if call_timeout(self.gpu.reset_od, 30) == "timeout":
            if not FAKE:
                self._reboot(st, "Kortet svarer ikke efter fejlen. Genstarter")
                return
        st["status"] = "between"
        self.save(st)

    def _count_crash(self, st, step):
        """Tæller crashes. Kun gentagne crashes på samme indstilling er en crash-løkke;
        crashes mens testen bevæger sig mod sikrere indstillinger er forventede."""
        key = f"{step['phase']}|{step['cap_w']}|{step['offset_mv']}"
        if st.get("last_crash_key") == key:
            st["consecutive_crashes"] = st.get("consecutive_crashes", 0) + 1
        else:
            st["consecutive_crashes"] = 1
        st["last_crash_key"] = key
        st["total_crashes"] = st.get("total_crashes", 0) + 1

    def _gpu_dead(self, st, n, step, msg):
        result = {"n": n, **step, "status": "fail", "reason": "gpu_reset", "detail": msg,
                  "started": time.time(), "duration_s": 0, "images": 0}
        self._finish_step(st, result, False)
        self._count_crash(st, step)
        if not FAKE:
            self._reboot(st, msg)

    def _reboot(self, st, msg):
        st["status"] = "rebooting"
        st["boot_id"] = boot_id()
        self.save(st)
        self.event(st, "warn", msg)
        call_timeout(self.comfy.stop, 20)
        self.sys.reboot()
        time.sleep(600)

    def _check_image(self, st, n, phase, data, vi=0):
        arr = decode(data)
        img_dir = S.ensure_dir(S.Path(st["run_dir"]) / "images")
        ref = st.get("ref")
        if ref is None:
            ref = st["ref"] = {"refs": {}, "mode": None, "threshold": None, "baseline_psnrs": []}
        refs = ref.setdefault("refs", {})
        key = str(vi)
        if key not in refs:
            if phase != "baseline":
                raise RuntimeError("Mangler referencebillede for seed-variant " + key)
            name = "reference.png" if vi == 0 else f"reference_{vi}.png"
            S.write_bytes(img_dir / name, data)
            refs[key] = {"hash": pixel_hash(arr), "file": str(img_dir / name)}
            ref.setdefault("file", refs[key]["file"])
            return True, math.inf
        ref_arr = decode(open(refs[key]["file"], "rb").read())
        p = psnr(arr, ref_arr)
        if phase == "baseline":
            ref["baseline_psnrs"].append(p)
            if p < 15:
                ref["bad_ref"] = refs[key]["file"]
                S.write_bytes(img_dir / f"step{n:03d}_baseline_diff.png", data)
                return False, p
            return True, p
        good = p == math.inf if ref["mode"] == "exact" else p >= ref["threshold"]
        if not good:
            S.write_bytes(img_dir / f"step{n:03d}_bad.png", data)
            st["current"]["bad_ref"] = refs[key]["file"]
        return good, p

    def _result(self, st, n, step, t0, samples, fail):
        cur = st["current"] or {}
        times = cur.get("img_times", [])
        psnrs = [p for p in cur.get("psnrs", []) if p is not None]
        r = {"n": n, **step, "status": "pass" if fail is None else "fail",
             "reason": fail[0] if fail else None, "detail": fail[1] if fail else None,
             "started": t0, "duration_s": round(time.time() - t0), "images": cur.get("images", len(times)),
             "min_psnr": min(psnrs) if psnrs else None}
        warm = cur.get("warm_from")
        if warm:
            ws = [x for x in samples if x.get("t", 0) >= warm]
            if len(ws) >= 30:
                samples = ws
        r.update(step_stats(samples, times))
        if cur.get("cold_img_s"):
            r["cold_img_s"] = cur["cold_img_s"]
        if cur.get("bad_ref"):
            r["bad_ref"] = cur["bad_ref"]
        return r

    def _finish_step(self, st, result, passed):
        run_dir = S.Path(st["run_dir"])
        S.append_jsonl(run_dir / "steps.jsonl", result, sync=True)
        search = Search(st["search"])
        phase = result["phase"]
        if phase == "warmup":
            if passed:
                st["needs_warmup"] = False
            st["current"] = None
            self.event(st, "info" if passed else "warn",
                       "Opvarmning færdig" if passed else
                       f"Opvarmning fejlede ({result.get('reason')}) – prøver igen",
                       step=result["n"], detail=result.get("detail"))
            self.save(st)
            return
        if phase == "baseline" and passed:
            ref = st["ref"]
            ps = ref.get("baseline_psnrs", [])
            if all(p == math.inf for p in ps):
                ref["mode"], ref["threshold"] = "exact", None
                note = "Billederne er bit-identiske. Enhver afvigelse tæller som fejl."
            else:
                ref["mode"] = "psnr"
                ref["threshold"] = round(max(20.0, min(ps) - 3.0), 2)
                note = (f"Billederne varierer lidt selv på standard (laveste {min(ps):.1f} dB). "
                        f"Grænse for fejl: under {ref['threshold']} dB.")
            ref["sec_per_img"] = result.get("sec_per_img")
            self.event(st, "info", note)
        search.record(passed)
        if phase == "baseline" and not passed and result.get("reason") == "mismatch":
            search.d["error"] = ("Workflowet giver meget forskellige billeder hver gang. "
                                 "Tjek at seed er sat til 'fixed'.")
        st["search"] = search.to_dict()
        st["current"] = None
        lvl = "info" if passed else "warn"
        txt = "bestået" if passed else f"FEJLET ({result.get('reason')})"
        self.event(st, lvl, f"Trin {result['n']} {txt}: {result['cap_w']} W, {result['offset_mv']} mV"
                            + (f", {result['img_per_hour']} billeder/t, {result['avg_power_w']} W"
                               if result.get("img_per_hour") else ""),
                   step=result["n"], detail=result.get("detail"))
        self.save(st)

    def record_hard_crash(self, st):
        cur = st.get("current") or {}
        if not cur:
            return
        run_dir = S.Path(st["run_dir"])
        hits, tail = self.sys.previous_boot_log()
        log = run_dir / f"kernel_step{cur['n']:03d}.log"
        S.write_text(log, "GPU-relaterede linjer:\n" + "\n".join(hits) +
                     "\n\nSidste linjer før genstart:\n" + "\n".join(tail))
        samples = S.read_jsonl(cur.get("samples_file", ""))
        step = {k: cur[k] for k in ("phase", "cap_w", "offset_mv")}
        r = {"n": cur["n"], **step, "status": "fail", "reason": "freeze",
             "detail": "Maskinen frøs eller genstartede under trinnet."
                       + ("\n\n" + "\n".join(hits[:15]) if hits else ""),
             "started": cur.get("started"), "duration_s": None,
             "images": cur.get("images", 0), "kernel_log": str(log), "min_psnr": None}
        r.update(step_stats(samples, cur.get("img_times", [])))
        self._count_crash(st, step)
        self._finish_step(st, r, False)

    # ---------- afslutning ----------
    def release(self, st, restart_lactd=True):
        """Sæt kortet og systemet tilbage til normal."""
        call_timeout(lambda: (self.gpu.reset_od(), self.gpu.reset_power_cap()), 30)
        if self.comfy:
            call_timeout(self.comfy.stop, 30)
        if self.watchdog:
            self.watchdog.stop()
            self.watchdog = None
        for k, v in (st.get("sysctl_saved") or {}).items():
            if v is not None:
                self.sys.write_sysctl(k, v)
        if restart_lactd and st.get("lactd_was_active"):
            self.sys.start_lactd()

    def terminate(self):
        st = S.read_json(S.STATE) or {}
        if self.watchdog:  # altid først, så maskinen ikke genstarter uventet
            self.watchdog.stop()
            self.watchdog = None
        if not st.get("active"):
            return
        if st.get("status") == "rebooting":
            return  # vores egen genstart efter en GPU-fejl
        if self.sys.system_stopping():
            # Maskinen lukkes ned/genstartes af brugeren: nulstil kortet, men lad testen
            # fortsætte efter næste boot med samme trin forfra.
            self.release(st, restart_lactd=False)
            if st.get("status") == "running":
                st["step_n"] = max(0, st.get("step_n", 1) - 1)
            st.update(status="between", current=None, needs_warmup=True)
            self.save(st)
            self.event(st, "warn", "Maskinen lukkes ned. Testen fortsætter efter næste opstart.")
        else:
            self.finish(st, "aborted", "Stoppet fordi worker-tjenesten blev stoppet.")

    def finish(self, st, status, message):
        self.release(st)
        cur = st.get("current")
        if cur and status == "aborted":
            S.append_jsonl(S.Path(st["run_dir"]) / "steps.jsonl",
                           {"n": cur["n"], "phase": cur["phase"], "cap_w": cur["cap_w"],
                            "offset_mv": cur["offset_mv"], "status": "aborted",
                            "started": cur["started"], "images": cur.get("images", 0)}, sync=True)
        steps = S.read_jsonl(S.Path(st["run_dir"]) / "steps.jsonl")
        st.update({"active": False, "status": status, "message": message,
                   "finished_at": time.time(), "current": None,
                   "recommendation": recommend(steps, st["cfg"])})
        self.prepared = False
        self.comfy = None
        self.save(st)
        self.event(st, "info" if status == "finished" else "warn",
                   f"{message} Kortet er sat tilbage til standard.")
        cfg = st.get("cfg") or {}
        if cfg.get("auto_report", True):
            try:
                path = report.save(st["run_dir"], cfg.get("report_dir"), st)
                st["report_path"] = str(path)
                self.save(st)
                self.event(st, "info", f"Rapport gemt: {path}")
            except Exception as ex:  # noqa: BLE001
                try:
                    path = report.save(st["run_dir"], None, st)
                    st["report_path"] = str(path)
                    self.save(st)
                    self.event(st, "error", f"Kunne ikke gemme rapporten i {cfg.get('report_dir')} ({ex}). "
                                            f"Den ligger i stedet her: {path}")
                except Exception as ex2:  # noqa: BLE001
                    self.event(st, "error", f"Kunne ikke skrive rapporten: {ex2}")
        if status in ("finished", "error") and cfg.get("shutdown_when_finished"):
            self.shutdown_countdown(st, int(cfg.get("shutdown_delay_s", 60)))

    def shutdown_countdown(self, st, delay):
        """Sluk computeren efter testen, med mulighed for at annullere i appen."""
        st["shutdown_at"] = time.time() + delay
        self.save(st)
        self.event(st, "warn", f"Computeren slukkes om {delay} sekunder. Tryk 'Annullér nedlukning' "
                               "i appen for at stoppe det.")
        end = time.time() + delay
        while time.time() < end:
            cmd = self._read_command()
            if cmd and cmd.get("action") == "abort":
                st.pop("shutdown_at", None)
                self.save(st)
                self.event(st, "info", "Nedlukning annulleret.")
                return
            time.sleep(1)
        st.pop("shutdown_at", None)
        st["shut_down_at"] = time.time()
        self.save(st)
        self.event(st, "warn", "Slukker computeren nu.")
        self.sys.poweroff()


def main():
    import fcntl
    S.ensure_dir(S.DATA)
    lock = open(S.DATA / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("En anden UV Tune-worker kører allerede.", file=sys.stderr)
        sys.exit(1)
    signal.signal(signal.SIGTERM, _on_sigterm)
    if os.geteuid() != 0 and not FAKE:
        print("Advarsel: workeren skal køre som root (via systemd) for at kunne ændre kortet.",
              file=sys.stderr)
    Worker().loop()


if __name__ == "__main__":
    main()
