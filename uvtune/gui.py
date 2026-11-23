"""UV Tune – NiceGUI user interface. Runs as your own user and talks to the worker through files."""
import json
import argparse
import os
import socket
import subprocess
import sys
import webbrowser
import time
from datetime import datetime

from nicegui import app, run as ng_run, ui
from starlette.requests import Request

from . import storage as S
from .comfy import seed_variants, validate_workflow
from .config import DEFAULTS, merged, parse_caps
from .imagecmp import fmt_psnr
from . import amdsmi, report
from .reco import PICKS, delta, recommend

REASONS = {"mismatch": "Wrong image (calculation error)", "gpu_reset": "GPU reset / driver error",
           "timeout": "Hang (timeout)", "comfyui_error": "ComfyUI error",
           "freeze": "Freeze or reboot"}
PHASES = {"baseline": "Reference", "warmup": "Warm-up", "coarse": "Screening (coarse)",
          "up": "Screening (up)", "fine": "Screening (fine)", "confirm": "Confirmation",
          "verify": "Verification"}
STATUS = {"between": "Running", "running": "Running", "rebooting": "Rebooting",
          "finished": "Finished", "aborted": "Aborted", "error": "Error"}


def load_cfg():
    raw = S.read_json(S.CONFIG) or {}
    if raw and "screen_minutes" not in raw:  # saved with v0.1: use the new, shorter times
        raw.pop("verify_minutes", None)
    return merged(raw)


def save_cfg(cfg):
    S.write_json(S.CONFIG, cfg)


def worker_alive():
    hb = S.read_json(S.HEARTBEAT) or {}
    return (time.time() - hb.get("t", 0)) < 12, hb


def list_runs():
    if not S.RUNS.exists():
        return []
    return sorted((p.name for p in S.RUNS.iterdir() if p.is_dir()), reverse=True)


def fmt_t(epoch):
    return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M:%S") if epoch else "–"


def fmt_dur(sec):
    if sec is None:
        return "–"
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}t {m:02d}m" if h else f"{m}m {s:02d}s"


def fmt_pct(v, invert=False):
    if v is None:
        return ""
    good = (v < 0) if invert else (v > 0)
    color = "text-positive" if good else ("text-negative" if abs(v) > 0.5 else "text-grey")
    return f'<span class="{color}">{v:+.1f} %</span>'


def run_checks(cfg):
    """Returns a list of (level, text). level: ok / warn / err."""
    out = []
    alive, hb = worker_alive()
    if alive and (hb.get("root") or hb.get("fake")):
        out.append(("ok", f"The worker service is running ({hb.get('gpu')})"
                          + (" – SIMULATION" if hb.get("fake") else "")))
    elif alive:
        out.append(("err", "The worker is running, but not as root."))
    else:
        out.append(("err", "The worker service is not responding. Run: sudo systemctl start uvtune-worker"))
    try:
        from .gpu import open_gpu
        g = open_gpu()
        od = g.od_info()
        pi = g.power_info()
        out.append(("ok", f"Graphics card: {g.name}"))
        if not od["od_available"]:
            out.append(("err", "Overdrive is not enabled. Use 'Enable overclocking' in LACT and reboot."))
        elif not od["vo_supported"]:
            out.append(("err", "The card shows no voltage offset (OD_VDDGFX_OFFSET)."))
        else:
            rng = f" (allowed {od['vo_min']} to {od['vo_max']} mV)" if od["vo_min"] is not None else ""
            out.append(("ok", f"Voltage offset supported, currently {od['vo_current']} mV{rng}"))
            if od["vo_current"]:
                out.append(("warn", "The card already has an offset set (e.g. from LACT). The test resets it."))
        if pi["default_w"]:
            caps = parse_caps(cfg["power_caps_pct"])
            ws = ", ".join(f"{p:g} % = {round(pi['default_w'] * p / 100)} W" for p in caps)
            out.append(("ok", f"Power limit default {pi['default_w']} W "
                              f"(min {pi['min_w']}, max {pi['max_w']}). Testing: {ws}"))
    except Exception as e:  # noqa: BLE001
        out.append(("err", f"Could not read the graphics card: {e}"))

    wp = cfg["workflow_path"]
    if not wp or not os.path.isfile(wp):
        out.append(("err", "Choose or upload your workflow (API format)."))
    else:
        try:
            wf = json.load(open(wp))
            err = validate_workflow(wf)
            out.append(("err", err) if err else ("ok", f"Workflow OK ({len(wf)} nodes)"))
            if not err:
                a, _, _, desc = seed_variants(wf)
                out.append(("ok", desc) if a is not None else ("warn", desc))
        except ValueError:
            out.append(("err", "The workflow file is not valid JSON."))
    if not os.path.isdir(cfg["comfy_cwd"]):
        out.append(("err", f"The ComfyUI folder does not exist: {cfg['comfy_cwd']}"))
    else:
        out.append(("ok", "ComfyUI folder found"))
    cmd = cfg["comfy_command"]
    args = cmd.split()
    if "--cache-none" in args:
        out.append(("warn", "--cache-none is no longer needed and makes the test much slower "
                            "(all models are reloaded for every image). It is removed automatically "
                            "when the workflow has a fixed seed."))
    if "--cache-lru" in args or "--cache-ram" in args:
        out.append(("warn", "Remove --cache-lru/--cache-ram: with them, ComfyUI can reuse images instead of calculating them."))
    if "--listen" in args and "0.0.0.0" in args:
        out.append(("warn", "The test instance listens on the whole network (--listen 0.0.0.0). "
                            "Use --listen 127.0.0.1; it only needs to be reachable locally."))
    if f"--port {int(cfg['comfy_port'])}" not in cmd:
        out.append(("warn", f"The command should contain --port {int(cfg['comfy_port'])}."))
    try:
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{int(cfg['comfy_port'])}/system_stats", timeout=2)
        out.append(("warn", f"Something is already running on port {cfg['comfy_port']}. Close it before starting."))
    except Exception:  # noqa: BLE001
        pass
    out.append(("warn", "Close your normal ComfyUI, other GPU programs (such as Ollama) and games before "
                        "starting, so they do not use the GPU at the same time. Pausing cloud sync tools is a good idea."))
    return out


def estimate_hours(cfg):
    n = max(1, len(parse_caps(cfg["power_caps_pct"])))
    sm, cm, vm = cfg["screen_minutes"], cfg["confirm_minutes"], cfg["verify_minutes"]
    lo = cfg["warmup_minutes"] + n * (4 * sm + cm + vm)
    hi = cfg["warmup_minutes"] * 3 + n * (9 * sm + 2.5 * cm + vm) + 4 * n  # + reboots
    return lo / 60, hi / 60


def _own_addresses():
    addrs = {"127.0.0.1", "::1", "localhost"}
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
        addrs.update(out.split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return addrs


OWN_ADDRS = _own_addresses()
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def remote_view():
    """Clean progress page for other computers: view-only, no buttons or settings."""
    ui.colors(primary="#c0392b")
    with ui.header().classes("items-center justify-between"):
        ui.label("UV Tune – progress").classes("text-h6")
        status_lbl = ui.label().classes("text-subtitle1")

    with ui.card().classes("w-full"):
        overall = ui.label().classes("text-subtitle1")
        title = ui.label().classes("text-h6")
        sub = ui.label()
        prog = ui.linear_progress(value=0, show_value=False).classes("q-my-sm")
        with ui.row().classes("gap-8"):
            stats = {k: ui.label() for k in ("power", "sclk", "vdd", "temp", "fan", "img")}
    result_box = ui.column().classes("w-full")

    def chart(ttl, series, y2):
        return ui.echart({
            "animation": False, "title": {"text": ttl, "left": 10, "textStyle": {"fontSize": 14}},
            "tooltip": {"trigger": "axis"}, "legend": {"top": 0, "right": 10},
            "grid": {"left": 50, "right": 60, "top": 60, "bottom": 30},
            "xAxis": {"type": "value", "name": "min", "min": 0},
            "yAxis": [{"type": "value", "scale": True, "position": "left"},
                      {"type": "value", "scale": True, "name": y2, "position": "right"}],
            "series": [{"name": n, "type": "line", "showSymbol": False, "data": [], "yAxisIndex": i}
                       for n, i in series]}).classes("w-full h-72")
    ch1 = chart("Power, temperature and fan",
                [("Power (W)", 0), ("Junction (°C)", 0), ("Edge (°C)", 0), ("Fan (rpm)", 1)], "rpm")
    ch2 = chart("Clock and voltage", [("GPU-clock (MHz)", 0), ("Vddgfx (mV)", 1)], "mV")

    ui.label("Completed steps").classes("text-h6 q-mt-md")
    cols = [{"name": k, "label": lbl, "field": k} for k, lbl in (
        ("n", "#"), ("phase_txt", "Phase"), ("cap_w", "Power (W)"), ("offset_mv", "Offset (mV)"),
        ("status_txt", "Result"), ("img_per_hour", "Images/h"), ("avg_power_w", "Avg W"),
        ("max_junction_c", "Max junction"), ("avg_fan_rpm", "Fan rpm"))]
    table = ui.table(columns=cols, rows=[], row_key="n").classes("w-full")
    last = {"steps": -1, "run": None}

    def refresh():
        st = S.read_json(S.STATE) or {}
        alive, _ = worker_alive()
        if st.get("shutdown_at"):
            status_lbl.set_text(f"Powering off in {max(0, int(st['shutdown_at'] - time.time()))} s")
        elif not alive:
            status_lbl.set_text("The test machine is not responding (it may be rebooting)")
        elif st.get("active"):
            status_lbl.set_text(STATUS.get(st.get("status"), st.get("status")))
        else:
            status_lbl.set_text(STATUS.get(st.get("status"), "No test running") if st else "No test running")

        sd = st.get("search") or {}
        caps = sd.get("caps") or []
        if st.get("run_id"):
            idx = min(sd.get("cap_idx", 0) + 1, len(caps)) if caps else 0
            started = st.get("created")
            overall.set_text(f"Run {st['run_id']} · power limit {idx} of {len(caps)}"
                             + (f" ({caps[idx - 1]} W)" if caps and idx else "")
                             + f" · step {st.get('step_n', 0)}"
                             + (f" · running for {fmt_dur(time.time() - started)}" if started and st.get("active") else ""))
        else:
            overall.set_text("")

        cur = st.get("current")
        if st.get("grace_until") and st.get("active"):
            title.set_text("Waiting after reboot")
            sub.set_text(f"Continuing in {fmt_dur(st['grace_until'] - time.time())}")
            prog.set_value(0)
        elif cur:
            title.set_text(f"Step {cur['n']}: {PHASES.get(cur['phase'])} – {cur['cap_w']} W, {cur['offset_mv']} mV")
            el = time.time() - cur["started"]
            sub.set_text(f"{fmt_dur(el)} of {fmt_dur(cur['duration_s'])} done")
            prog.set_value(min(1.0, el / max(1, cur["duration_s"])))
        elif st.get("active"):
            title.set_text("Preparing the next step…")
            sub.set_text("")
        else:
            title.set_text(STATUS.get(st.get("status"), "No test running") if st else "No test running")
            sub.set_text(st.get("message", "") if st else "")
            prog.set_value(1.0 if st.get("status") == "finished" else 0)

        if cur:
            samples = S.read_jsonl(cur["samples_file"], tail=900)
            if samples:
                x = samples[-1]
                g = lambda k, u="": f"{x.get(k)}{u}" if x.get(k) is not None else "–"  # noqa: E731
                stats["power"].set_text(f"Power: {g('power_w', ' W')}")
                stats["sclk"].set_text(f"Clock: {g('sclk_mhz', ' MHz')}")
                stats["vdd"].set_text(f"Vddgfx: {g('vddgfx_mv', ' mV')}")
                stats["temp"].set_text(f"Junction: {g('temp_junction', ' °C')}")
                stats["fan"].set_text(f"Fan: {g('fan_rpm', ' rpm')}")
                t0 = cur["started"]

                def ser(k):
                    return [[round((y["t"] - t0) / 60, 3), y.get(k)] for y in samples if y.get(k) is not None]
                for i, k in enumerate(("power_w", "temp_junction", "temp_edge", "fan_rpm")):
                    ch1.options["series"][i]["data"] = ser(k)
                for i, k in enumerate(("sclk_mhz", "vddgfx_mv")):
                    ch2.options["series"][i]["data"] = ser(k)
                ch1.update()
                ch2.update()
            stats["img"].set_text(f"Images: {cur.get('images', 0)}")
        else:
            for lbl in stats.values():
                lbl.set_text("")

        run = st.get("run_id")
        if not run:
            return
        steps = S.read_jsonl(S.RUNS / run / "steps.jsonl")
        if run == last["run"] and len(steps) == last["steps"] and st.get("active"):
            return
        last.update(run=run, steps=len(steps))
        rows = []
        for x in reversed(steps):
            r = dict(x)
            r["phase_txt"] = PHASES.get(x.get("phase"), x.get("phase"))
            r["status_txt"] = {"pass": "✅ Stable", "aborted": "⏹ Aborted"}.get(
                x.get("status"), f"❌ {REASONS.get(x.get('reason'), x.get('reason'))}")
            rows.append(r)
        table.rows = rows
        table.update()
        result_box.clear()
        if not st.get("active") and st.get("status") in ("finished", "error", "aborted"):
            reco = recommend(steps, st.get("cfg") or {})
            with result_box, ui.card().classes("w-full"):
                ui.label("Result").classes("text-h6")
                if not reco["verified"]:
                    ui.label("No setting passed the long verification.")
                for key, ttl, _ in PICKS:
                    p_ = reco.get(key)
                    if p_:
                        ui.label(f"{ttl}: {p_['cap_w']} W, {p_['offset_mv']} mV – "
                                 f"{p_['img_per_hour']} images/h, {p_['avg_power_w']} W")

    ui.timer(2.0, refresh)


@ui.page("/")
def index(request: Request):
    cfg = load_cfg()
    client_ip = request.client.host if request.client else ""
    local = client_ip in LOOPBACK
    if not local:
        remote_view()
        return

    ui.colors(primary="#c0392b")
    with ui.header().classes("items-center justify-between"):
        ui.label("UV Tune – undervolt finder for AMD GPUs"
                 + ("" if local else "  ·  remote view")).classes("text-h6")
        with ui.row().classes("items-center"):
            status_lbl = ui.label().classes("text-subtitle1")
            def on_abort():
                if (S.read_json(S.STATE) or {}).get("shutdown_at"):
                    S.write_json(S.COMMAND, {"action": "abort"})
                    ui.notify("Cancelling shutdown…")
                else:
                    confirm_abort.open()
            abort_btn = ui.button("Abort test", color="white", on_click=on_abort) \
                .props("outline")

    with ui.dialog() as confirm_abort, ui.card():
        ui.label("Abort the test? The card will be set back to stock.")
        with ui.row():
            ui.button("Yes, abort", on_click=lambda: (S.write_json(S.COMMAND, {"action": "abort"}),
                                                     confirm_abort.close(),
                                                     ui.notify("Aborting…")))
            ui.button("Cancel", on_click=confirm_abort.close).props("flat")

    with ui.tabs().classes("w-full") as tabs:
        t_setup = ui.tab("Setup", icon="tune")
        t_live = ui.tab("Live", icon="monitor_heart")
        t_gpu = ui.tab("GPU (amd-smi)", icon="memory")
        t_res = ui.tab("Results", icon="insights")
        t_ev = ui.tab("Events", icon="list")

    st0 = S.read_json(S.STATE) or {}
    with ui.tab_panels(tabs, value=t_live if (st0.get("active") or not local) else t_setup).classes("w-full"):
        # ---------------- SETUP ----------------
        with ui.tab_panel(t_setup):
            if not local:
                with ui.card().classes('w-full'):
                    ui.label('Remote view').classes('text-h6')
                    ui.label('You are viewing UV Tune from another computer. Setup and starting a test are only possible on the test machine itself.')
            else:
                with ui.row().classes("w-full gap-6 items-start"):
                    with ui.card().classes("w-[44rem]"):
                        ui.label("ComfyUI test").classes("text-h6")
                        wf_input = ui.input("Workflow (API format JSON)").bind_value(cfg, "workflow_path") \
                            .classes("w-full")

                        async def on_upload(e):
                            data = await e.file.read()
                            dest = S.DATA / "workflow_api.json"
                            S.write_bytes(dest, data)
                            cfg["workflow_path"] = str(dest)
                            wf_input.value = str(dest)
                            try:
                                err = validate_workflow(json.loads(data))
                            except ValueError:
                                err = "Not valid JSON"
                            ui.notify(err or "Workflow uploaded", type="negative" if err else "positive")
                        ui.upload(label="…or upload the workflow file", on_upload=on_upload,
                                  auto_upload=True).props("accept=.json").classes("w-full")
                        ui.input("ComfyUI folder").bind_value(cfg, "comfy_cwd").classes("w-full")
                        ui.textarea("Start command (run in the folder as your user)") \
                            .bind_value(cfg, "comfy_command").classes("w-full").props("autogrow")
                        ui.number("Port for the test instance", format="%d").bind_value(cfg, "comfy_port")
                        ui.markdown("Use the same command as in your start script, but with "
                                    "`--listen 127.0.0.1 --port 8189` and without `--cache-none`. If your script sets "
                                    "environment variables, put them in front, e.g. `HSA_XYZ=1 venv/bin/python …`.") \
                            .classes("text-caption")

                    with ui.card().classes("w-[30rem]"):
                        ui.label("Test plan").classes("text-h6")
                        ui.input("Power limits in % of default (comma-separated)") \
                            .bind_value(cfg, "power_caps_pct").classes("w-full")
                        with ui.grid(columns=2).classes("w-full"):
                            ui.number("Coarse steps (mV)", min=5, max=100).bind_value(cfg, "coarse_step_mv")
                            ui.number("Fine steps (mV)", min=1, max=25).bind_value(cfg, "fine_step_mv")
                            ui.number("Lowest offset (mV)", max=0).bind_value(cfg, "min_offset_mv")
                            ui.number("Safety margin (mV)", min=0, max=50).bind_value(cfg, "safety_margin_mv")
                            ui.number("Warm-up (min)", min=1).bind_value(cfg, "warmup_minutes")
                            ui.number("Screening per step (min)", min=0.5, step=0.5) \
                                .bind_value(cfg, "screen_minutes")
                            ui.number("Confirmation (min)", min=1).bind_value(cfg, "confirm_minutes")
                            ui.number("Verification (min)", min=1).bind_value(cfg, "verify_minutes")
                            ui.number("Allowed performance loss (%)", min=0, max=20, step=0.5) \
                                .bind_value(cfg, "perf_tolerance_pct")
                        est = ui.label().classes("text-caption")
                        ui.timer(1.0, lambda: est.set_text(
                            "Expected duration: about {:.1f}–{:.1f} hours".format(*estimate_hours(cfg))
                            if all(cfg.get(k) for k in ("warmup_minutes", "screen_minutes", "confirm_minutes",
                                                         "verify_minutes")) else ""))

                        ui.label("Safety").classes("text-h6 q-mt-md")
                        ui.switch("Reboot the machine after a GPU reset (recommended)") \
                            .bind_value(cfg, "reboot_on_gpu_reset")
                        ui.switch("Pause LACT during the test").bind_value(cfg, "stop_lactd")
                        ui.switch("Reboot automatically if the machine freezes completely (watchdog)") \
                            .bind_value(cfg, "auto_reboot_on_freeze")
                        ui.label("Without the watchdog you have to press reset yourself if the machine freezes completely. "
                                 "The test continues by itself after the reboot.").classes("text-caption")

                        ui.label("Report and shutdown").classes("text-h6 q-mt-md")
                        ui.input("Report folder").bind_value(cfg, "report_dir").classes("w-full")
                        ui.switch("Save the report automatically when the test ends") \
                            .bind_value(cfg, "auto_report")
                        ui.switch("Power off the computer when the test is finished") \
                            .bind_value(cfg, "shutdown_when_finished")
                        ui.label("The report is saved first. Then there is a 60-second countdown, "
                                 "which can be cancelled in the app. Does not power off if you abort the test yourself.") \
                            .classes("text-caption")

                check_box = ui.column().classes("w-full q-mt-md")

                def do_check():
                    save_cfg(cfg)
                    check_box.clear()
                    with check_box:
                        for lvl, txt in run_checks(cfg):
                            icon = {"ok": "check_circle", "warn": "warning", "err": "error"}[lvl]
                            color = {"ok": "positive", "warn": "warning", "err": "negative"}[lvl]
                            with ui.row().classes("items-center"):
                                ui.icon(icon, color=color)
                                ui.label(txt)

                def do_start():
                    save_cfg(cfg)
                    checks = run_checks(cfg)
                    errs = [t for lvl, t in checks if lvl == "err"]
                    do_check()
                    if errs:
                        ui.notify("Fix the errors first", type="negative")
                        return
                    wf = json.load(open(cfg["workflow_path"]))
                    S.write_json(S.COMMAND, {"action": "start", "config": cfg, "workflow": wf})
                    ui.notify("Test started", type="positive")
                    tabs.set_value(t_live)

                with ui.row().classes("q-mt-md"):
                    ui.button("Save", icon="save", on_click=lambda: (save_cfg(cfg), ui.notify("Saved"))) \
                        .props("outline")
                    ui.button("Check system", icon="fact_check", on_click=do_check).props("outline")
                    ui.button("Start test", icon="play_arrow", on_click=do_start)
                    ui.button("Reset to defaults", on_click=lambda: (cfg.update(DEFAULTS),
                                                                        ui.notify("Default values restored"))) \
                        .props("flat")

        # ---------------- LIVE ----------------
        with ui.tab_panel(t_live):
            with ui.card().classes("w-full"):
                live_title = ui.label().classes("text-h6")
                live_sub = ui.label()
                live_prog = ui.linear_progress(value=0, show_value=False).classes("q-my-sm")
                with ui.row().classes("gap-8"):
                    live_stats = {k: ui.label() for k in
                                  ("power", "sclk", "vdd", "temp", "fan", "img", "psnr")}
                live_msg = ui.label().classes("text-caption")

            def chart(title, series, y2=None):
                opts = {"animation": False, "title": {"text": title, "left": 10, "textStyle": {"fontSize": 14}},
                        "tooltip": {"trigger": "axis"}, "legend": {"top": 0, "right": 10},
                        "grid": {"left": 50, "right": 60, "top": 60, "bottom": 30},
                        "xAxis": {"type": "value", "name": "min", "min": 0},
                        "yAxis": [{"type": "value", "scale": True, "position": "left"}],
                        "series": [{"name": n, "type": "line", "showSymbol": False, "data": [],
                                    "yAxisIndex": i} for n, i in series]}
                if y2:
                    opts["yAxis"].append({"type": "value", "scale": True, "name": y2, "position": "right"})
                return ui.echart(opts).classes("w-full h-72")
            ch1 = chart("Power, temperature and fan",
                        [("Power (W)", 0), ("Junction (°C)", 0), ("Edge (°C)", 0), ("Fan (rpm)", 1)],
                        y2="rpm")
            ch2 = chart("Clock and voltage", [("GPU-clock (MHz)", 0), ("Vddgfx (mV)", 1)], y2="mV")

        # ---------------- GPU (amd-smi) ----------------
        with ui.tab_panel(t_gpu):
            smi_msg = ui.label().classes("text-negative")
            with ui.row().classes("items-center"):
                ui.label("Live (updated every 5 seconds)").classes("text-h6")
            smi_live = ui.row().classes("w-full gap-4 items-start")
            with ui.row().classes("items-center q-mt-md"):
                ui.label("Card information").classes("text-h6")
                smi_refresh = ui.button(icon="refresh").props("flat")
            smi_static = ui.row().classes("w-full gap-4 items-start")

        # ---------------- RESULTS ----------------
        with ui.tab_panel(t_res):
            with ui.row().classes("items-center"):
                run_sel = ui.select(list_runs(), label="Test run").classes("w-64")
                ui.button(icon="refresh", on_click=lambda: run_sel.set_options(list_runs())).props("flat")

                def sel_run_dir():
                    st_ = S.read_json(S.STATE) or {}
                    r = current_run(st_)
                    return (S.RUNS / r) if r else None

                async def save_report():
                    rd = sel_run_dir()
                    if not rd:
                        ui.notify("No test run selected", type="warning")
                        return
                    save_cfg(cfg)
                    try:
                        path = await ng_run.io_bound(report.save, rd, cfg.get("report_dir"))
                        ui.notify(f"Report saved: {path}", type="positive", timeout=8000)
                    except Exception as ex:  # noqa: BLE001
                        ui.notify(f"Could not save the report: {ex}", type="negative", timeout=10000)

                async def download_report():
                    rd = sel_run_dir()
                    if not rd:
                        ui.notify("No test run selected", type="warning")
                        return
                    html_txt, _ = await ng_run.io_bound(report.build, rd)
                    ui.download.content(html_txt.encode(), f"uvtune-report-{rd.name}.html", "text/html")

                if local:
                    ui.button("Save report", icon="save", on_click=save_report).props("outline")
                ui.button("Download report", icon="download", on_click=download_report).props("outline")
            reco_box = ui.column().classes("w-full")
            with ui.row().classes("w-full no-wrap gap-4"):
                cmp_scatter = ui.echart({
                    "animation": False, "title": {"text": "Power vs performance (images/h)", "left": 10,
                                                  "textStyle": {"fontSize": 14}},
                    "tooltip": {"trigger": "item"}, "legend": {"bottom": 0},
                    "grid": {"left": 60, "right": 30, "top": 50, "bottom": 100},
                    "xAxis": {"type": "value", "name": "Average power (W)", "nameLocation": "middle",
                              "nameGap": 30, "scale": True},
                    "yAxis": {"type": "value", "scale": True},
                    "series": []}).classes("w-1/2 h-96")
                cmp_bar = ui.echart({
                    "animation": False, "title": {"text": "Change compared with stock (%)", "left": 10,
                                                  "textStyle": {"fontSize": 14}},
                    "tooltip": {"trigger": "axis"}, "legend": {"bottom": 0},
                    "grid": {"left": 50, "right": 20, "top": 50, "bottom": 60},
                    "xAxis": {"type": "category", "data": ["Performance", "Power", "Wh per image", "Fan"]},
                    "yAxis": {"type": "value"},
                    "series": []}).classes("w-1/2 h-96")
            ui.label("Performance: plus is good. Power, Wh per image and fan: minus is good.") \
                .classes("text-caption")
            res_chart = ui.echart({
                "animation": False, "tooltip": {"trigger": "item"}, "legend": {"top": 0},
                "grid": {"left": 60, "right": 60, "top": 40, "bottom": 40},
                "xAxis": {"type": "value", "name": "Voltage offset (mV)", "nameLocation": "middle",
                          "nameGap": 28, "inverse": True, "max": 0, "scale": True},
                "yAxis": [{"type": "value", "name": "images/h", "scale": True, "position": "left"},
                          {"type": "value", "name": "W", "scale": True, "position": "right"}],
                "series": []}).classes("w-full h-80")
            cols = [
                {"name": "n", "label": "#", "field": "n", "sortable": True},
                {"name": "phase", "label": "Phase", "field": "phase_txt"},
                {"name": "cap", "label": "Power (W)", "field": "cap_w", "sortable": True},
                {"name": "off", "label": "Offset (mV)", "field": "offset_mv", "sortable": True},
                {"name": "status", "label": "Result", "field": "status_txt"},
                {"name": "iph", "label": "Images/h", "field": "img_per_hour", "sortable": True},
                {"name": "pw", "label": "Avg W", "field": "avg_power_w", "sortable": True},
                {"name": "wh", "label": "Wh/image", "field": "wh_per_img", "sortable": True},
                {"name": "clk", "label": "MHz", "field": "avg_sclk_mhz", "sortable": True},
                {"name": "vdd", "label": "Vddgfx", "field": "avg_vddgfx_mv"},
                {"name": "tj", "label": "Max junction", "field": "max_junction_c", "sortable": True},
                {"name": "fan", "label": "Fan rpm", "field": "avg_fan_rpm", "sortable": True},
                {"name": "psnr", "label": "Image", "field": "psnr_txt"},
                {"name": "imgs", "label": "Count", "field": "images"},
            ]
            res_table = ui.table(columns=cols, rows=[], row_key="n").classes("w-full")

        # ---------------- EVENTS ----------------
        with ui.tab_panel(t_ev):
            ev_box = ui.column().classes("w-full")

    # ---------------- opdatering ----------------
    last = {"ev_count": -1, "steps_count": -1, "run": None}

    def current_run(st):
        if run_sel.value:
            return run_sel.value
        return st.get("run_id") or (list_runs() or [None])[0]

    def refresh_live():
        st = S.read_json(S.STATE) or {}
        alive, hb = worker_alive()
        if not alive:
            status_lbl.set_text("Worker: offline")
        elif st.get("active"):
            status_lbl.set_text(f"Status: {STATUS.get(st.get('status'), st.get('status'))}"
                                + (" (SIMULATION)" if hb.get("fake") else ""))
        else:
            status_lbl.set_text("Status: ready" + (" (SIMULATION)" if hb.get("fake") else ""))
        if st.get("shutdown_at"):
            left = max(0, int(st["shutdown_at"] - time.time()))
            status_lbl.set_text(f"Powering off in {left} s")
            abort_btn.set_text("Cancel shutdown")
            abort_btn.set_visibility(True)
        else:
            abort_btn.set_text("Abort test")
            abort_btn.set_visibility(bool(st.get("active")))

        cur = st.get("current")
        if st.get("grace_until") and st.get("active"):
            live_title.set_text("Waiting after reboot")
            live_sub.set_text(f"Continuing in {fmt_dur(st['grace_until'] - time.time())}")
        elif cur:
            live_title.set_text(f"Step {cur['n']}: {PHASES.get(cur['phase'])} – "
                                f"{cur['cap_w']} W, {cur['offset_mv']} mV")
            el = time.time() - cur["started"]
            live_sub.set_text(f"{fmt_dur(el)} of {fmt_dur(cur['duration_s'])} done")
            live_prog.set_value(min(1.0, el / max(1, cur["duration_s"])))
        elif st.get("active"):
            live_title.set_text("Preparing the next step…")
            live_sub.set_text("")
        else:
            live_title.set_text(STATUS.get(st.get("status"), "No test running") if st else
                                "No test running")
            live_sub.set_text(st.get("message", "") if st else "")
            live_prog.set_value(0)
        live_msg.set_text(f"Run {st.get('run_id')}" if st.get("run_id") else "")

        if not cur:
            return
        samples = S.read_jsonl(cur["samples_file"], tail=900)
        if samples:
            s = samples[-1]
            g = lambda k, u="": f"{s.get(k)}{u}" if s.get(k) is not None else "–"  # noqa: E731
            live_stats["power"].set_text(f"Power: {g('power_w', ' W')}")
            live_stats["sclk"].set_text(f"Clock: {g('sclk_mhz', ' MHz')}")
            live_stats["vdd"].set_text(f"Vddgfx: {g('vddgfx_mv', ' mV')}")
            live_stats["temp"].set_text(f"Junction: {g('temp_junction', ' °C')}")
            live_stats["fan"].set_text(f"Fan: {g('fan_rpm', ' rpm')}")
            t0 = cur["started"]

            def ser(k):
                return [[round((x["t"] - t0) / 60, 3), x.get(k)] for x in samples if x.get(k) is not None]
            for i, k in enumerate(("power_w", "temp_junction", "temp_edge", "fan_rpm")):
                ch1.options["series"][i]["data"] = ser(k)
            for i, k in enumerate(("sclk_mhz", "vddgfx_mv")):
                ch2.options["series"][i]["data"] = ser(k)
            ch1.update()
            ch2.update()
        live_stats["img"].set_text(f"Images: {cur.get('images', 0)}"
                                   + (f" (last {cur['last_img_s']} s)" if cur.get("last_img_s") else ""))
        ps = cur.get("psnrs") or []
        live_stats["psnr"].set_text(f"Last image: {fmt_psnr(ps[-1])}" if ps else "")

    def refresh_results():
        st = S.read_json(S.STATE) or {}
        run = current_run(st)
        if not run:
            return
        run_dir = S.RUNS / run
        steps = S.read_jsonl(run_dir / "steps.jsonl")
        events = S.read_jsonl(run_dir / "events.jsonl")
        if run == last["run"] and len(steps) == last["steps_count"] and len(events) == last["ev_count"]:
            return
        last.update(run=run, steps_count=len(steps), ev_count=len(events))
        run_st = st if st.get("run_id") == run else {}
        cfg_run = run_st.get("cfg") or cfg

        rows = []
        for s in steps:
            r = dict(s)
            r["phase_txt"] = PHASES.get(s.get("phase"), s.get("phase"))
            r["status_txt"] = {"pass": "✅ Stable", "aborted": "⏹ Aborted"}.get(
                s.get("status"), f"❌ {REASONS.get(s.get('reason'), s.get('reason'))}")
            r["psnr_txt"] = fmt_psnr(s.get("min_psnr")) if s.get("images") else "–"
            rows.append(r)
        res_table.rows = rows
        res_table.update()

        series = []
        for cap in sorted({s["cap_w"] for s in steps if s.get("cap_w")}, reverse=True):
            pts = sorted([s for s in steps if s.get("cap_w") == cap and s.get("status") == "pass"
                          and s.get("img_per_hour")], key=lambda s: s["offset_mv"])
            series.append({"name": f"{cap} W – images/h", "type": "line", "symbolSize": 8,
                           "data": [[s["offset_mv"], s["img_per_hour"]] for s in pts]})
            series.append({"name": f"{cap} W – power", "type": "line", "yAxisIndex": 1,
                           "lineStyle": {"type": "dashed"}, "symbol": "diamond",
                           "data": [[s["offset_mv"], s["avg_power_w"]] for s in pts]})
        res_chart.options["series"] = series
        res_chart.update()

        reco = recommend(steps, cfg_run)
        base = reco["baseline"]
        passed = [x for x in steps if x.get("status") == "pass" and x.get("img_per_hour")
                  and x.get("avg_power_w") and x.get("phase") != "verify"]
        sc = [{"name": "Passed tests", "type": "scatter", "symbolSize": 7,
               "itemStyle": {"color": "#bbbbbb"},
               "data": [[x["avg_power_w"], x["img_per_hour"]] for x in passed]},
              {"name": "Stable (verified)", "type": "scatter", "symbolSize": 11,
               "data": [[x["avg_power_w"], x["img_per_hour"]] for x in reco["verified"]]}]
        if base:
            sc.append({"name": "Stock", "type": "scatter", "symbol": "diamond", "symbolSize": 16,
                       "itemStyle": {"color": "#333333"},
                       "data": [[base["avg_power_w"], base["img_per_hour"]]]})
        colors = {"most_uv": "#8e44ad", "least": "#27ae60", "fastest": "#e67e22"}
        bars = []
        for key, title, _ in PICKS:
            p = reco.get(key)
            if not p:
                continue
            sc.append({"name": title, "type": "scatter", "symbol": "pin", "symbolSize": 34,
                       "itemStyle": {"color": colors[key]},
                       "label": {"show": True, "formatter": title.split(".")[0], "color": "#fff"},
                       "data": [[p["avg_power_w"], p["img_per_hour"]]]})
            if base:
                vals = [delta(p.get(k), base.get(k)) for k in
                        ("img_per_hour", "avg_power_w", "wh_per_img", "avg_fan_rpm")]
                bars.append({"name": title, "type": "bar", "itemStyle": {"color": colors[key]},
                             "data": [round(v, 1) if v is not None else None for v in vals]})
        cmp_scatter.options["series"] = sc
        pts = [pt for ser in sc for pt in ser["data"]]
        if pts:
            xs, ys = [pt[0] for pt in pts], [pt[1] for pt in pts]
            padx, pady = (max(xs) - min(xs)) * 0.08 + 5, (max(ys) - min(ys)) * 0.15 + 20
            cmp_scatter.options["xAxis"].update(min=round(min(xs) - padx), max=round(max(xs) + padx))
            cmp_scatter.options["yAxis"].update(min=round(min(ys) - pady), max=round(max(ys) + pady))
        cmp_scatter.update()
        cmp_bar.options["series"] = bars
        cmp_bar.update()

        reco_box.clear()
        with reco_box:
            base = reco["baseline"]
            if not base:
                ui.label("No reference measurement yet.").classes("text-grey")
            else:
                ui.label(f"Reference (stock, {base['cap_w']} W, 0 mV): {base['img_per_hour']} images/h, "
                         f"{base['avg_power_w']} W, junction max {base['max_junction_c']} °C, "
                         f"fan {base['avg_fan_rpm']} rpm")

                tol = cfg_run.get("perf_tolerance_pct", 2)
                seen = {}
                with ui.row().classes("w-full no-wrap gap-4 items-stretch"):
                    for key, title, note in PICKS:
                        p = reco.get(key)
                        with ui.card().classes("w-1/3"):
                            ui.label(title).classes("text-h6")
                            ui.label(note.format(tol=tol)).classes("text-caption")
                            if not p:
                                ui.label("Not found yet.").classes("text-grey")
                                continue
                            if key == "least" and not reco["least_within_tol"]:
                                ui.label(f"No stable setting was within {tol} %; "
                                         "showing the one with the smallest loss.").classes("text-caption text-warning")
                            if p["n"] in seen:
                                ui.label(f"Same setting as {seen[p['n']]}").classes("text-caption")
                            seen.setdefault(p["n"], title.split(".")[0] + ".")
                            ui.markdown(f"**Set in LACT:** Power limit **{p['cap_w']} W**, "
                                        f"voltage offset **{p['offset_mv']} mV**")
                            dj = (p.get("max_junction_c") or 0) - (base.get("max_junction_c") or 0)
                            ui.html(
                                f"Performance: {p['img_per_hour']} images/h "
                                f"{fmt_pct(delta(p['img_per_hour'], base['img_per_hour']))}<br>"
                                f"Power: {p['avg_power_w']} W "
                                f"{fmt_pct(delta(p['avg_power_w'], base['avg_power_w']), True)}<br>"
                                f"Wh per image: {p.get('wh_per_img')} "
                                f"{fmt_pct(delta(p.get('wh_per_img'), base.get('wh_per_img')), True)}<br>"
                                f"Junction max: {p['max_junction_c']} °C ({dj:+.1f} °C)<br>"
                                f"Fan: {p['avg_fan_rpm']} rpm "
                                f"{fmt_pct(delta(p['avg_fan_rpm'], base['avg_fan_rpm']), True)}")
                if not reco["verified"]:
                    ui.label("No settings are verified yet. The three picks appear when "
                             "the first long verification has passed.").classes("text-grey")
            if run_st.get("message"):
                ui.label(run_st["message"]).classes("text-subtitle2")

        ev_box.clear()
        with ev_box:
            for e in reversed(events[-300:]):
                color = {"error": "negative", "warn": "warning"}.get(e.get("level"), "grey-8")
                txt = f"{fmt_t(e.get('t'))}  {e.get('msg')}"
                if e.get("detail") or e.get("trace"):
                    with ui.expansion(txt, icon="warning" if e["level"] != "info" else "info") \
                            .classes(f"w-full text-{color}"):
                        ui.code(e.get("detail") or e.get("trace")).classes("w-full")
                        n = e.get("step")
                        if n:
                            for name in (f"step{n:03d}_bad.png", f"step{n:03d}_baseline_diff.png"):
                                bad = run_dir / "images" / name
                                if bad.exists():
                                    st_ = next((x for x in steps if x.get("n") == n), {})
                                    ref = S.Path(st_.get("bad_ref") or run_dir / "images" / "reference.png")
                                    with ui.row():
                                        with ui.column():
                                            ui.label("Reference")
                                            ui.image(ref).classes("w-64")
                                        with ui.column():
                                            ui.label("Failed image")
                                            ui.image(bad).classes("w-64")
                            klog = run_dir / f"kernel_step{n:03d}.log"
                            if klog.exists():
                                ui.label(f"Kernel log: {klog}").classes("text-caption")
                else:
                    ui.label(txt).classes(f"text-{color}")

    def render_smi(container, data):
        container.clear()
        with container:
            for sec, rows in amdsmi.sections(data).items():
                with ui.card().classes("w-80"):
                    ui.label(sec).classes("text-subtitle1 text-weight-medium")
                    for k, v in rows[:60]:
                        with ui.row().classes("w-full justify-between no-wrap"):
                            ui.label(k).classes("text-caption text-grey-8")
                            ui.label(v).classes("text-caption text-right")

    async def load_static():
        data, err = await ng_run.io_bound(amdsmi.run, "static")
        smi_msg.set_text(err or "")
        if data is not None:
            render_smi(smi_static, data)

    smi_busy = {"v": False}

    async def refresh_smi():
        if smi_busy["v"] or tabs.value not in (t_gpu, "GPU (amd-smi)"):
            return
        smi_busy["v"] = True
        try:
            data, err = await ng_run.io_bound(amdsmi.run, "metric")
            smi_msg.set_text(err or "")
            if data is not None:
                render_smi(smi_live, data)
        finally:
            smi_busy["v"] = False

    smi_refresh.on_click(load_static)
    ui.timer(0.5, load_static, once=True)
    ui.timer(5.0, refresh_smi)
    tabs.on_value_change(refresh_smi)

    run_sel.on_value_change(lambda: last.update(run=None) or refresh_results())
    ui.timer(2.0, refresh_live)
    ui.timer(5.0, refresh_results)


@app.get("/uvtune-health")
def _health():
    return {"app": "uvtune"}


def _is_uvtune(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/uvtune-health", timeout=3) as r:
            return json.loads(r.read()).get("app") == "uvtune"
    except Exception:  # noqa: BLE001
        return False


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser(description="UV Tune user interface")
    ap.add_argument("--host", default=os.environ.get("UVTUNE_GUI_HOST", "127.0.0.1"),
                    help="0.0.0.0 = visible from other computers on the network (view-only progress page)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UVTUNE_GUI_PORT", 8090)))
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if _port_in_use(args.port):
        if _is_uvtune(args.port):
            # already running (e.g. as a service): just open it
            print(f"UV Tune is already running at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return
        msg = (f"Port {args.port} is already used by another program (e.g. ComfyUI), so UV Tune "
               f"cannot start there. Find the program with:  sudo ss -ltnp | grep :{args.port}")
        print(msg, file=sys.stderr)
        try:
            subprocess.run(["notify-send", "UV Tune could not start", msg], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        sys.exit(1)
    S.ensure_dir(S.DATA)
    if args.host not in ("127.0.0.1", "localhost"):
        ips = [a for a in OWN_ADDRS if a not in ("127.0.0.1", "::1", "localhost") and ":" not in a]
        for ip in ips:
            print(f"Remote view: http://{ip}:{args.port}")
    ui.run(title="UV Tune", host=args.host, port=args.port, reload=False,
           show=not args.no_browser, favicon="⚡")


if __name__ in {"__main__", "__mp_main__"}:
    main()
