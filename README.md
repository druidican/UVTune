# UVTune

An automatic undervolt tester for AMD graphics cards on Linux with ROCm. UVTune finds the
lowest stable voltage offset and the best power limit for your card. It uses an AI workload
both as the load and as the error detector: either ComfyUI image generation, or a built-in
PyTorch test that needs no ComfyUI and no models. It keeps going on its own through driver
crashes, freezes and reboots.

Goals: **lower power draw**, **unchanged or better performance**, **stability and less fan noise**.

**Tested on Ubuntu 26.04 with ROCm 10.2.0** and an RX 9070 XT (RDNA4). It should also work on
other Linux distributions with the `amdgpu` driver and systemd, and on other AMD cards that
expose a voltage offset (`OD_VDDGFX_OFFSET`) through amdgpu overdrive.

| | Status |
|---|---|
| Ubuntu 26.04, ROCm 10.2.0, GNOME | Tested |
| Other Debian/Ubuntu-based distributions | Should work |
| Fedora | Should work, not tested yet (see [Fedora notes](#fedora-notes)) |
| KDE Plasma | Should work: the app runs in the browser, and the menu entry and icon follow freedesktop standards |

> Want to modify UVTune yourself? See [DEVELOPMENT.md](DEVELOPMENT.md).

## How it works

1. **Reference and warm-up (5 min):** runs the test load at stock settings, measures
   performance, power, temperature and fan speed, and saves the reference results. The card
   is warm afterwards.
2. **Screening (3 min per step):** lowers the voltage 25 mV at a time until something fails,
   then moves 5 mV at a time towards the limit. Clearly unstable settings show up quickly.
3. **Confirmation (15 min):** starts with the most aggressive setting that passed screening.
   If it fails, it goes 5 mV up and tries again.
4. **Verification (30 min):** the confirmed setting plus a 10 mV safety margin.
5. **Next power limit** (default 100, 90, 85 and 80 %): screening starts at the previous
   limit's confirmed offset instead of at 0 mV.
6. **After every reboot**, the card is warmed up for 5 minutes at stock voltage before the
   test continues.
7. **Result:** three picks among the verified (stable) settings, with comparison charts
   against stock:
   1. *Most undervolted, stable:* the lowest voltage that passed verification.
   2. *Least negative impact:* the lowest power draw where performance is at most 2 % below stock.
   3. *Best positive impact:* the highest performance.

### Manual list

Want to test specific settings instead? Choose **Manual list** under *Test plan* and enter
one setting per line, as watts and mV:

```
254 -80
254 -75
269 -75
```

After the reference at stock, each setting is tested for the chosen time (default 60 minutes).
If a setting fails, it is tested again 5 mV closer to 0 until it holds, so you always find
out the nearest stable value. The report lists each setting with what was requested and
where it turned out stable, and the three picks and charts work as usual.

A step fails if any of these happens:

- **Wrong image / wrong result:** the result differs from the reference. The GPU calculated
  something wrong without crashing. This is the most common sign of a too-low voltage.
- **GPU reset:** amdgpu reports a ring timeout or reset in the kernel log.
- **Test load error or hang:** for example a HIP error, or a result that never finishes.
- **Freeze:** the machine froze or rebooted. This is detected at the next boot.

After a GPU reset the machine reboots (this can be turned off), and the test continues by
itself with the next, safer step. The kernel always resets the card to stock on reboot.

## Requirements

- Linux with the `amdgpu` driver and systemd (tested on Ubuntu 26.04; other distributions should work)
- An AMD card with voltage offset support in overdrive
- One of these test loads:
  - **ComfyUI** with a working ROCm setup (tested with ROCm 10.2.0), or
  - **any Python environment with PyTorch for ROCm**, for the built-in test (no ComfyUI or
    models needed). The ComfyUI environment works for this too.
- Python 3.10 or newer
- Optional: [LACT](https://github.com/ilya-zlobintsev/LACT) to apply the result afterwards
- Optional: `amd-smi` (comes with ROCm) for the *GPU (amd-smi)* tab

The install script uses `apt` to install `python3-venv` and `python3-pip`. On a distribution
without `apt`, install those two packages (or your distribution's equivalent) yourself first,
then run the script; it skips the `apt` step when `apt-get` is not available.

## Before you install

1. **Enable overdrive.** In LACT: *Enable overclocking*, then reboot. This is the same as the
   kernel parameter `amdgpu.ppfeaturemask=0xffffffff`.
2. **Nothing else is required.** The app finds ROCm, ComfyUI and PyTorch for you (see
   *Choosing the test load* below).

## Installation

```bash
git clone https://github.com/druidican/UVTune.git
cd UVTune
sudo ./install.sh
```

Then start **UV Tune** from the application menu, or run `uvtune-gui`. The app opens in your
browser at http://127.0.0.1:8090.

To update later:

```bash
cd UVTune
git pull
sudo ./install.sh          # add --remote if you use remote viewing
```

## Usage

1. **Setup tab → Scan system.** UVTune looks for ROCm, ComfyUI installations and Python
   environments with PyTorch. Click **Use** next to what you want to test with.
2. **Choose the test load** (see below) and the test plan.
3. Click **Check system** and fix anything marked red.
4. Close your normal ComfyUI, other GPU programs (such as Ollama) and games. Pausing cloud
   sync tools during the test is a good idea.
5. Click **Start test**. The **Live** tab shows graphs, and **Results** shows the table,
   charts and recommendations.
6. When the test is finished, the card is set back to stock. Enter the recommended power
   limit and voltage offset in LACT.

## Choosing the test load

**ComfyUI workflow** (closest to real image generation). Choose the workflow under *Workflow*:

- **Built-in** (default): a two-pass text-to-image workflow with locked seeds that uses only
  standard ComfyUI nodes. Pick one of your own checkpoints from the dropdown (it lists
  `models/checkpoints` in your ComfyUI folder) and a resolution: 1024 for SDXL and newer
  models, 512 for SD 1.5. Adjust the steps so one image takes about 20–60 seconds.
- **Preset: Anima int8 + Qwen:** the workflow UVTune was developed with. It needs the Anima
  and Qwen models and two custom nodes (`ComfyUI-INT8-Fast-ROCM` and `ComfyUI-Pixaroma`).
- **My own workflow file:** any workflow you saved with **Export (API)**. It needs a fixed seed
  ("control after generate" = *fixed*).

The test starts its own ComfyUI instance. The start command should contain
`--listen 127.0.0.1 --port 8189`; the scan fills this in for you.

**Built-in PyTorch test** (no ComfyUI, no models). Runs AI-style calculations in your own
PyTorch environment: transformer attention and MLP layers (like a diffusion transformer) and
3x3 convolutions (like a VAE), in bf16, with fixed weights and seeded inputs. It sizes itself
once so one result takes about 30 seconds, and every result is compared bit for bit with the
reference. It needs a Python with the ROCm build of PyTorch; **Scan system** finds it. If your
card needs a ROCm override (for example `HSA_OVERRIDE_GFX_VERSION`), enter it under *Extra
environment variables*.

## Report and shutdown

- **Report:** when the test ends, a report is saved in the folder you choose in Setup
  (default `~/Documents/UV Tune`). It consists
  of an HTML file that opens in any browser without internet, and a CSV file with all
  measurements (for LibreOffice Calc, for example). Under **Results** you can also save or
  download the report for an earlier run.
- **Shut down when finished:** the report is saved first. Then there is a 60-second countdown
  before the computer powers off. The countdown can be cancelled with the **Cancel shutdown**
  button in the app on the test machine. The computer also
  shuts down if the test stops by itself because of an error, but not if you abort it yourself.

## Remote viewing from another computer

```bash
sudo ./install.sh --remote
```

The app then always runs as a service, also after a reboot and without anyone logged in.
From other computers on the network, open `http://<test-machine-IP>:8090` to see a clean
progress page: the current step, live graphs, completed steps and, at the end, the result.
It has no buttons and no settings. It is view-only.

The full app with setup, start and abort is only available on the test machine itself at
`http://127.0.0.1:8090` (or from the *UV Tune* menu entry). Run `sudo ./install.sh` without
`--remote` to turn remote viewing off again.

If another program already uses port 8090, pick a different one:
`sudo ./install.sh --remote --port 8095`.

Note that the test's ComfyUI has its own port (for example 8189). If you open that port from
another computer, you will see ComfyUI, not UVTune. Keep the test ComfyUI listening on
`127.0.0.1`.

## Good to know

- **Stopping and continuing:** if you shut down or reboot during a test, it continues
  automatically after the next startup and redoes the interrupted step. Clicking **Abort test**
  ends the run for good and writes a report of what was tested so far.
- **Watchdog (optional):** if the machine freezes completely, the motherboard's hardware
  watchdog reboots it after about a minute. Without the watchdog you have to press reset
  yourself, and then the test continues.
- **Duration:** about 1–1.5 hours per power limit, plus a few minutes per reboot. With 2
  power limits about 2–3 hours, with 4 about 5 hours. The app shows an estimate.
- **Emergency stop:** if the app does not respond, run `sudo systemctl stop uvtune-worker`.
  The card is set back to stock immediately.
- **Data:** results, logs, failure images and kernel logs are stored in
  `~/.local/share/uvtune/runs/`.
- **Models stay loaded:** the test alternates between two fixed seeds (A, B, A, B), and each
  seed has its own reference image. This forces ComfyUI to calculate every image from scratch
  without reloading the models. `--cache-none` is not needed and is removed automatically.
  Only if your workflow has no fixed seed that can be changed do you need `--cache-none`.
- **Workflow length:** aim for 20–60 seconds per image. The first image after ComfyUI
  (re)starts loads the models and does not count towards the performance numbers.
- **Gaming:** an undervolt that is stable in AI workloads is usually stable in games too.
  Still, keep an eye on it for the first few days, and go 5–10 mV up if a game crashes.
- **Safety:** undervolting cannot damage the card. The worst case is a crash or wrong
  results, and the card is always back at stock after a reboot.

## Fedora notes

UVTune has not been tested on Fedora yet, but nothing in it is Ubuntu-specific: it uses
systemd, journalctl, the amdgpu driver and Python. Things to know:

- `install.sh` skips the `apt` step. Fedora's Python already includes `venv` and `pip`.
- `amd-smi` is packaged as `amdsmi` on Fedora. The app shows the right install hint.
- With `--remote`, the install script tells you if firewalld needs the port opened.
- If SELinux is enforcing and the worker cannot write to `~/.local/share/uvtune`, check
  `journalctl -u uvtune-worker` and report it as an issue.

## Uninstall

```bash
sudo ./uninstall.sh           # removes the program, keeps your test results
sudo ./uninstall.sh --purge   # also removes results, logs and settings
```

The script aborts any running test, sets the card back to stock, restarts LACT if the test
had paused it, and removes both services (including remote viewing), `/opt/uvtune`, the
`uvtune-gui` command, the icon and the menu entry. `python3-venv` and `python3-pip` are left installed,
since other programs may use them.

## License

MIT. See [LICENSE](LICENSE).

Copyright © 2026 L. G. H. Druidican
