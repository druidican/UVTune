# UVTune

An automatic undervolt tester for AMD graphics cards on Linux with ROCm. UVTune finds the
lowest stable voltage offset and the best power limit for your card. It uses your own
ComfyUI workflow both as the load and as the error detector, and it keeps going on its own
through driver crashes, freezes and reboots.

Goals: **lower power draw**, **unchanged or better performance**, **stability and less fan noise**.

Developed and tested on an RX 9070 XT (RDNA4) with Ubuntu 26.10 and ROCm 10.2. It should
work on other AMD cards that expose a voltage offset (`OD_VDDGFX_OFFSET`) through amdgpu
overdrive.

> **Note:** the app's user interface is currently in Danish.
> Want to modify UVTune yourself? See [DEVELOPMENT.md](DEVELOPMENT.md).

## How it works

1. **Reference and warm-up (5 min):** runs your workflow at stock settings, measures
   performance, power, temperature and fan speed, and saves the reference images. The card
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

A step fails if any of these happens:

- **Wrong image:** the image differs from the reference. The GPU calculated something wrong
  without crashing. This is the most common sign of a too-low voltage.
- **GPU reset:** amdgpu reports a ring timeout or reset in the kernel log.
- **ComfyUI error or hang:** for example a HIP error, or an image that never finishes.
- **Freeze:** the machine froze or rebooted. This is detected at the next boot.

After a GPU reset the machine reboots (this can be turned off), and the test continues by
itself with the next, safer step. The kernel always resets the card to stock on reboot.

## Requirements

- Linux with the `amdgpu` driver and systemd (tested on Ubuntu)
- An AMD card with voltage offset support in overdrive
- ComfyUI with a working ROCm/PyTorch setup
- Python 3.10 or newer
- Optional: [LACT](https://github.com/ilya-zlobintsev/LACT) to apply the result afterwards
- Optional: `amd-smi` (comes with ROCm) for the *GPU (amd-smi)* tab

## Before you install

1. **Enable overdrive.** In LACT: *Enable overclocking*, then reboot. This is the same as the
   kernel parameter `amdgpu.ppfeaturemask=0xffffffff`.
2. **Prepare a workflow** in ComfyUI with a fixed seed ("control after generate" = *fixed*).
   Each image should take about 20–60 seconds. Save it with **Export (API)**. A ready-made
   example is in [`workflows/UVTune_Test_Workflow_API.json`](workflows/UVTune_Test_Workflow_API.json)
   (adjust the model file names to your own).

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

1. **Setup tab:** upload your workflow file, and check the ComfyUI folder and start command.
   The command should contain `--listen 127.0.0.1 --port 8189`. The test starts its own
   ComfyUI instance on that port.
2. Click **Tjek system** (check system) and fix anything marked red.
3. Close your normal ComfyUI, other GPU programs (such as Ollama) and games. Pausing cloud
   sync tools during the test is a good idea.
4. Click **Start test**. The **Live** tab shows graphs, and **Resultater** (results) shows the
   table, charts and recommendations.
5. When the test is finished, the card is set back to stock. Enter the recommended power
   limit and voltage offset in LACT.

## Report and shutdown

- **Report:** when the test ends, a report is saved in the folder you choose in Setup
  (default `~/Documents/UV Tune`, or `~/Dokumenter/UV Tune` on a Danish system). It consists
  of an HTML file that opens in any browser without internet, and a CSV file with all
  measurements (for LibreOffice Calc, for example). Under **Resultater** you can also save or
  download the report for an earlier run.
- **Shut down when finished:** the report is saved first. Then there is a 60-second countdown
  before the computer powers off. The countdown can be cancelled with the *Annullér
  nedlukning* (cancel shutdown) button in the app on the test machine. The computer also
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
  automatically after the next startup and redoes the interrupted step. Clicking *Afbryd test*
  (abort) ends the run for good and writes a report of what was tested so far.
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

## Uninstall

```bash
sudo ./uninstall.sh           # removes the program, keeps your test results
sudo ./uninstall.sh --purge   # also removes results, logs and settings
```

The script aborts any running test, sets the card back to stock, restarts LACT if the test
had paused it, and removes both services (including remote viewing), `/opt/uvtune`, the
`uvtune-gui` command and the menu entry. `python3-venv` and `python3-pip` are left installed,
since other programs may use them.

## License

MIT. See [LICENSE](LICENSE).

Copyright © 2026 L. G. H. Druidican
