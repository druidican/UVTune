# Developing and modding UVTune

This guide is for anyone who wants to change UVTune, adapt it to another card or load,
or just understand how it works. For normal use, see [README.md](README.md).

UVTune is plain Python (about 3,000 lines) with three dependencies: `nicegui`, `numpy` and
`pillow`. There is no build step.

## Contents

- [Architecture](#architecture)
- [Module map](#module-map)
- [Data directory and file formats](#data-directory-and-file-formats)
- [The test run, step by step](#the-test-run-step-by-step)
- [Crash recovery](#crash-recovery)
- [Simulation mode (no GPU, no root)](#simulation-mode-no-gpu-no-root)
- [Development setup](#development-setup)
- [Common mods](#common-mods)
- [Ground rules](#ground-rules)
- [Testing checklist](#testing-checklist)

## Architecture

UVTune runs as two separate processes that only talk through files:

```
 ┌──────────────────────────┐        files in        ┌───────────────────────────┐
 │  GUI  (uvtune-gui)       │   ~/.local/share/uvtune │  Worker (uvtune-worker)   │
 │  runs as your user       │ ──── command.json ────▶ │  runs as root (systemd)   │
 │  NiceGUI web app         │ ◀─── state.json ─────── │  changes GPU settings     │
 │  port 8090               │ ◀─── runs/<id>/... ──── │  starts the test load     │
 └──────────────────────────┘ ◀─── heartbeat.json ── │  survives reboots         │
                                                     └───────────────────────────┘
```

- **Worker** (`uvtune/worker.py`): a systemd service running as root, because writing GPU
  overdrive and power limit values in sysfs requires root. It holds all the test logic,
  starts and stops the test load (a dedicated ComfyUI instance or the built-in PyTorch load,
  always as your user, via `runuser`), checks results, reads the kernel log, reboots after
  GPU faults, and writes all results.
- **GUI** (`uvtune/gui.py`): a NiceGUI app running as your normal user. It never changes the
  GPU. It writes settings and commands, and reads state and results. It can be closed and
  reopened at any time without affecting a running test.
- **Why files instead of a socket:** files survive crashes and reboots, and every write is
  atomic and fsync'ed. After a hard freeze, the worker can see exactly where it was.

The worker writes files as root and then `chown`s them to your user (the `UVTUNE_USER`
environment variable), so the GUI can read them and write commands back.

## Module map

| File | What it does |
|---|---|
| `worker.py` | Main loop, command handling, run lifecycle, running one step, crash recovery, report and shutdown at the end. |
| `search.py` | The search algorithm as a small, pure state machine (`Search`). No I/O, so it is easy to test. |
| `gpu.py` | sysfs access: sensors (hwmon), voltage offset (`pp_od_clk_voltage`), power limit (`power1_cap`). `FakeGpu` for simulation. |
| `comfy.py` | Starts/stops the test ComfyUI, submits workflows over the HTTP API, fetches the result image. Seed alternation (`seed_variants`). `FakeComfy` for simulation. |
| `workflows.py` | The built-in workflow (standard nodes only), presets in `uvtune/workflows/`, checkpoint listing, and `resolve()` which picks the workflow from the settings. |
| `torchbackend.py` | The built-in PyTorch load: starts `torchload.py` in the user's own Python and talks to it over stdin/stdout JSON lines. `FakeTorch` for simulation. |
| `torchload.py` | Runs **inside the user's PyTorch environment**, so it imports nothing from UV Tune. Transformer and convolution work in bf16 with fixed weights; returns a float32 fingerprint as `.npy`. |
| `scan.py` | Finds ROCm, ComfyUI installations and Python environments with PyTorch. Each Python is probed in its own subprocess. Also `install_hint()` for distribution-specific package names. |
| `imagecmp.py` | Decoding of PNG images and `.npy` arrays, hash and PSNR (works for both). |
| `system.py` | boot id, kernel log via `journalctl`, LACT service control, sysctls, reboot/poweroff, hardware watchdog. `FakeSystem` for simulation. |
| `reco.py` | Picks the three recommendations from the verified steps (`PICKS`, `recommend`). |
| `report.py` | Builds the HTML report (with dependency-free SVG charts) and the CSV. |
| `amdsmi.py` | Runs `amd-smi` and flattens its JSON for display. Tolerates format changes between ROCm versions. |
| `config.py` | All settings and their defaults (`DEFAULTS`). |
| `storage.py` | Paths, atomic JSON writes, JSONL append/read, ownership handling. |
| `gui.py` | Full local app (tabs: Setup, Live, GPU, Results, Events) and the read-only remote page (`remote_view`). |

Outside the package:

| File | What it does |
|---|---|
| `install.sh` | Creates `/opt/uvtune/venv`, installs the package, creates the `uvtune-worker` service and, with `--remote`, the `uvtune-gui` service. Installs `uvtune/icon.svg` as the `uvtune` icon in the hicolor theme. |
| `uninstall.sh` | Stops everything safely, resets the card if needed, removes services and files. `--purge` also deletes data. |
| `uvtune/workflows/` | Preset ComfyUI workflows in API format (listed in `workflows.PRESETS`). |

## Data directory and file formats

Default location: `~/.local/share/uvtune/` (override with `UVTUNE_DATA`).

| Path | Written by | Contents |
|---|---|---|
| `config.json` | GUI | Saved settings (see `config.py` for all keys). |
| `command.json` | GUI | One pending command: `{"action": "start", "config": {...}, "workflow": {...}}` or `{"action": "abort"}`. The worker deletes it after reading. |
| `state.json` | Worker | The current run: status, search state, current step, references, crash counters. This is the single source of truth. |
| `heartbeat.json` | Worker | Updated every 3 s. The GUI treats the worker as offline after 12 s without an update. |
| `runs/<run_id>/workflow.json` | Worker | Copy of the workflow used. |
| `runs/<run_id>/steps.jsonl` | Worker | One JSON line per finished step (see below). |
| `runs/<run_id>/events.jsonl` | Worker | Log messages shown in the Events tab. |
| `runs/<run_id>/samples_NNN.jsonl` | Worker | Sensor samples for step NNN, one per second. |
| `runs/<run_id>/images/` | Worker | `reference.png`/`.npy`, `reference_1.png`/`.npy` (one per seed) and `stepNNN_bad.png`/`.npy` for failures. `.npy` is used by the PyTorch load. |
| `runs/<run_id>/kernel_stepNNN.log` | Worker | Kernel log lines for GPU failures and freezes. |
| `runs/<run_id>/report.html`, `steps.csv` | Worker / GUI | The report. |

**A line in `steps.jsonl`** (fields from `worker.py` → `_result` and `step_stats`):

```json
{"n": 12, "phase": "verify", "cap_w": 254, "offset_mv": -80, "status": "pass",
 "reason": null, "detail": null, "started": 1795431978.3, "duration_s": 1898,
 "images": 58, "sec_per_img": 31.2, "img_per_hour": 115.4, "avg_power_w": 160.4,
 "wh_per_img": 1.39, "avg_sclk_mhz": 2582.3, "avg_vddgfx_mv": 795.9,
 "max_junction_c": 78.0, "avg_edge_c": 51.3, "avg_fan_rpm": 794.3,
 "max_fan_rpm": 1500.0, "min_psnr": Infinity}
```

`status` is `pass`, `fail` or `aborted`. `reason` is one of `mismatch`, `gpu_reset`,
`timeout`, `comfyui_error` or `freeze`. `phase` is one of `baseline`, `warmup`, `coarse`,
`up`, `fine`, `confirm` or `verify`.

**A sample line** (keys depend on what hwmon exposes, see `AmdGpu._scan_sensors`):

```json
{"t": 1795431980.1, "power_w": 161.0, "temp_edge": 51.0, "temp_junction": 77.0,
 "temp_mem": 70.0, "sclk_mhz": 2590.0, "mclk_mhz": 1258.0, "vddgfx_mv": 796.0,
 "fan_rpm": 790.0, "busy_pct": 100.0, "vram_mb": 11234.5}
```

## The test run, step by step

1. **Start:** the GUI writes `command.json`. `Worker.new_run` validates the workflow,
   checks root and overdrive support, turns the power limit percentages into watts, and
   creates `state.json` with a fresh `Search`.
2. **Prepare** (`Worker.prepare`, once per worker process and boot): pauses LACT, sets
   sysctls and the watchdog if enabled, builds the two seed variants of the workflow, and
   creates the ComfyUI controller.
3. **Each step** (`Worker.run_step`):
   1. Applies the power limit and voltage offset (in a thread with a timeout, because sysfs
      calls can hang on a broken GPU).
   2. Saves `status = "running"` and the current boot id **before** anything risky happens.
   3. Starts the sensor sampler thread.
   4. Generates results in a loop, alternating seed A and B, until the step's duration is up.
      Every result is compared with the reference for its seed. (The PyTorch load sizes
      itself once, during the first step, and stores the step count in `torch_steps`.) While waiting for each image,
      it checks the kernel log every 5 s and checks for an abort command.
   5. Computes statistics, appends to `steps.jsonl`, and feeds pass/fail into `Search.record`.
4. **The search** (`search.py`) decides the next step: `baseline` → `coarse` → (`up`) →
   `fine` → `confirm` → `verify`, then the next power limit. The `Search` docstring has the
   exact rules.
5. **End** (`Worker.finish`): resets the card and system (`release`), computes the
   recommendation, writes the report, and optionally starts the shutdown countdown.

**Image checking.** In the baseline step, the first image of each seed becomes that seed's
reference. If all later baseline images are bit-identical, the run uses exact mode (any
difference is a failure). Otherwise it uses PSNR mode, with a threshold 3 dB below the worst
baseline result (but at least 20 dB).

**Why two seeds?** ComfyUI only keeps the latest result per node. Alternating A, B, A, B
forces the samplers to run again on every image, while model loader nodes stay cached.
`seed_variants` finds the first sampler's seed: either a literal integer on the sampler, or a
single integer on a node that only feeds seed inputs.

## Crash recovery

The worker records state before each step, so it can tell what happened after a restart:

| `status` in `state.json` | Same boot? | Meaning | What the worker does |
|---|---|---|---|
| `running` | different boot id | Machine froze or rebooted mid-step | Records the step as `freeze`, saves the previous boot's kernel log, warms up, continues |
| `rebooting` | different boot id | The worker rebooted on purpose after a GPU fault | Continues with the next step after warm-up |
| `running` | same boot id | Only the worker process restarted | Redoes the step, no failure recorded |
| `between` | any | Between steps | Continues normally |

Other safety parts:

- **Shutdown during a test** (`Worker.terminate`, on SIGTERM while systemd is stopping):
  resets the card but keeps the run active, so it continues after the next boot.
- **`systemctl stop`** while the system is running: aborts the run cleanly and resets the card.
- **Crash loop protection:** stops after 3 crashes in a row on the *same* setting, or 40
  crashes in total (`max_consecutive_crashes`, `max_total_crashes`).
- **Watchdog:** `system.Watchdog` keeps `/dev/watchdog` open and always sends the magic close
  character `V` when it stops, so a normal stop never triggers a reboot.

## Simulation mode (no GPU, no root)

Set `UVTUNE_FAKE=1` to swap in `FakeGpu`, `FakeComfy` and `FakeSystem`. Everything else runs
for real: the search, state files, reports and GUI. The fake card produces wrong images
below about −95 mV and GPU resets below about −115 mV (a bit more headroom at lower power
limits), with some randomness, much like a real card.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

mkdir -p /tmp/uvd
export UVTUNE_FAKE=1 UVTUNE_DATA=/tmp/uvd

python -m uvtune.worker &          # terminal 1 (or in the background)
python -m uvtune.gui               # opens http://127.0.0.1:8090
```

In simulation mode, start a test from the GUI as usual. Both test loads work: the
simulated ComfyUI accepts any workflow, and the simulated PyTorch load needs no Python path. Set short durations such as
`0.1` minutes for warm-up and screening to get through a whole run in a few minutes.

You can also start a run without the GUI:

```bash
python - <<'EOF'
import json
cfg = {"power_caps_pct": "100, 85", "warmup_minutes": 0.1, "screen_minutes": 0.1,
       "confirm_minutes": 0.15, "verify_minutes": 0.2, "report_dir": "/tmp/uvd/reports"}
wf = json.load(open("uvtune/workflows/anima_int8.json"))
json.dump({"action": "start", "config": cfg, "workflow": wf}, open("/tmp/uvd/command.json", "w"))
EOF
```

To test crash recovery, kill the worker with `kill -9` during a step, change `boot_id` in
`/tmp/uvd/state.json` to any other text, and start the worker again. It should record the
step as a freeze and continue.

## Development setup

- Python 3.10 or newer. `pip install -e .` gives you the `uvtune-worker` and `uvtune-gui`
  commands, using your working copy.
- Run `python -m pyflakes uvtune` before committing (install with `pip install pyflakes`).
- To try your changes on real hardware, run `sudo ./install.sh` again. It reinstalls the
  package into `/opt/uvtune/venv` and restarts the worker. Don't do this during a running test.
- Worker log: `journalctl -u uvtune-worker -f`. GUI service log (remote mode):
  `journalctl -u uvtune-gui -f`.
- The test ComfyUI's own log is in `runs/<run_id>/comfyui.log`.

## Common mods

**Change the defaults** (step sizes, durations, power limits, tolerance): edit `DEFAULTS` in
`config.py`. Most of them can also be changed in the GUI. If you add a new key, add a
field for it in `gui.py` (Setup tab) and read it with `cfg["your_key"]` in the worker.

**Add a sensor:** add it in `AmdGpu._scan_sensors` (`gpu.py`) with a key, a path and a scale
factor. It is then logged automatically in the `samples_*.jsonl` files. To show it, add a
series to the charts in `gui.py`. To summarize it per step, add it to `step_stats` in
`worker.py` (and to `CSV_FIELDS` in `report.py` if you want it in the CSV).

**Change the search strategy:** everything is in `search.py`. `current()` returns the next
step, and `record(passed)` moves the state machine. Because it is pure logic, you can test
it with a small loop and a fake stability function, without the rest of the program:

```python
from uvtune.search import Search
s = Search.new([304, 274], {"coarse_step_mv": 25, "fine_step_mv": 5,
                            "safety_margin_mv": 10}, floor_mv=-200)
while not s.done:
    step = s.current()
    s.record(step["offset_mv"] >= -90)   # pretend everything below -90 mV fails
print(s.d["cap_results"])
```

A new phase needs a name in `PHASE_MINUTES` and `PHASE_NAMES` (`worker.py`) and in `PHASES`
(`gui.py` and `report.py`).

**Change the recommendations:** edit `PICKS` and `recommend()` in `reco.py`. The GUI,
remote page and report all use them, so new picks show up everywhere.

**Add another test load:** write a class with the same methods as `ComfyServer`:
`start()` (return `True` if it started a fresh process), `stop()`, `alive()` and
`run_once(workload, timeout, poll_cb, must_run=None)`, which returns `(bytes, seconds)`.
The bytes can be a PNG image or a `.npy` array; `imagecmp` handles both. It must return the
same result every time for the same input. Choose it in `Worker.prepare` with a new
`load_type`. `TorchBackend` is a complete example and `FakeComfy` a minimal one.

**Change the PyTorch load:** the model sizes are the constants at the top of
`torchload.py` (`B, T, D, H, L` for the transformer, `C, R, NCONV` for the convolutions).
Keep everything deterministic: fixed weights from a CPU generator, seeded inputs, no
random dropout. You can try it without UV Tune:
`printf '{"cmd":"run","seed":1,"steps":4}\n' | UVTUNE_TORCH_DEVICE=cpu UVTUNE_TORCH_SMALL=1 python3 uvtune/torchload.py`

**Add a workflow preset:** put an API-format workflow in `uvtune/workflows/` and add it to
`PRESETS` in `workflows.py`. It needs a fixed seed on its first sampler.

**Support another GPU or driver:** `gpu.py` assumes amdgpu sysfs. The voltage offset is set
with the `vo <mV>` and `c` commands on `pp_od_clk_voltage`, which RDNA3 and RDNA4 both
support. Older generations use different overdrive commands (voltage curve points), so you
would extend `set_voltage_offset` and `parse_od`.

**Translate the interface:** user-facing text is in English and is written directly in the
code: mainly `gui.py`, `report.py`, `reco.py` (pick names), `search.py` (result notes) and the
event messages in `worker.py`. There is no translation layer yet. Adding one, for example a
dictionary per language selected by a setting, would be a welcome contribution.

## Ground rules

These keep a test from leaving the system in a bad state. Please keep them when modding:

1. **Never block the worker's main loop on GPU sysfs.** A crashed GPU can make sysfs calls
   hang forever. Use `call_timeout(...)` for anything that writes to the card.
2. **Save state before a risky action.** Write `state.json` (`self.save(st)`) before applying
   new settings or rebooting, so a freeze can always be recognized afterwards.
3. **Every way out must call `release()`:** reset the voltage offset and power limit, stop
   ComfyUI, close the watchdog with the magic `V`, and restore sysctls and LACT.
4. **The GUI never touches the GPU directly.** It only writes `config.json` and
   `command.json`. Keep the root-level work in the worker.
5. **The remote page stays read-only.** `remote_view()` in `gui.py` must not create buttons
   or settings. The ComfyUI start command is run by the worker, so letting anyone on the
   network change it would let them run arbitrary commands on the test machine.
6. **Use the `storage.py` helpers for writing files** (`write_json`, `append_jsonl`,
   `write_text`). They write atomically and set the right file owner.

## Testing checklist

Before opening a pull request or pushing a release, check at least:

- [ ] `python -m pyflakes uvtune` reports nothing.
- [ ] A full simulated run with 2 power limits finishes and produces a report.
- [ ] Killing the worker mid-step (with a changed `boot_id`) records a freeze and continues.
- [ ] **Abort test** during a step resets the (fake) card and ends the run.
- [ ] The shutdown countdown can be cancelled.
- [ ] The remote page (opened from a non-loopback address) has no buttons.
- [ ] `bash -n install.sh uninstall.sh` passes.

If you have the hardware, run a short real test as well: one power limit with 1-minute steps.
