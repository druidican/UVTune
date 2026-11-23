"""Rapport over en testkørsel: én selvstændig HTML-fil (kan åbnes uden internet) + CSV med alle data."""
import csv
import html
import io
import math
import os
import time
from pathlib import Path

from . import storage as S
from .reco import PICKS, delta, recommend

REASONS = {"mismatch": "Forkert billede (regnefejl)", "gpu_reset": "GPU-reset / driverfejl",
           "timeout": "Hang (timeout)", "comfyui_error": "ComfyUI-fejl",
           "freeze": "Frys eller genstart"}
PHASES = {"baseline": "Reference", "warmup": "Opvarmning", "coarse": "Screening (grov)",
          "up": "Screening (op)", "fine": "Screening (fin)", "confirm": "Bekræftelse",
          "verify": "Verifikation"}
STATUS = {"finished": "Færdig", "aborted": "Afbrudt", "error": "Stoppet med fejl"}
COLORS = {"most_uv": "#8e44ad", "least": "#27ae60", "fastest": "#e67e22"}

CSV_FIELDS = ["n", "phase", "cap_w", "offset_mv", "status", "reason", "started", "duration_s",
              "images", "sec_per_img", "img_per_hour", "avg_power_w", "wh_per_img",
              "avg_sclk_mhz", "avg_vddgfx_mv", "max_junction_c", "avg_edge_c", "avg_fan_rpm",
              "max_fan_rpm", "min_psnr"]


def e(v):
    return html.escape("" if v is None else str(v))


def num(v, suffix=""):
    if v is None:
        return "–"
    if isinstance(v, float) and math.isinf(v):
        return "identisk"
    return f"{v:g}{suffix}" if isinstance(v, (int, float)) else e(v)


def pct(v, invert=False):
    if v is None:
        return ""
    good = (v < 0) if invert else (v > 0)
    cls = "good" if good else ("bad" if abs(v) > 0.5 else "")
    return f'<span class="{cls}">{v:+.1f} %</span>'


def fmt_t(epoch):
    return time.strftime("%d-%m-%Y %H:%M", time.localtime(epoch)) if epoch else "–"


# ---------- små SVG-grafer uden afhængigheder ----------
def _ticks(lo, hi, n=5):
    step = (hi - lo) / n or 1
    return [lo + i * step for i in range(n + 1)]


def svg_scatter(steps, reco):
    W, H, L, R, T, B = 640, 380, 60, 20, 20, 50
    pts = []
    for s in steps:
        if s.get("status") == "pass" and s.get("img_per_hour") and s.get("avg_power_w"):
            pts.append((s["avg_power_w"], s["img_per_hour"]))
    if not pts:
        return ""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1 = min(xs) - 10, max(xs) + 10
    y0, y1 = min(ys) * 0.97, max(ys) * 1.03
    X = lambda x: L + (x - x0) / (x1 - x0 or 1) * (W - L - R)  # noqa: E731
    Y = lambda y: H - B - (y - y0) / (y1 - y0 or 1) * (H - T - B)  # noqa: E731
    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    for t in _ticks(x0, x1):
        o.append(f'<line x1="{X(t):.1f}" y1="{T}" x2="{X(t):.1f}" y2="{H - B}" stroke="#eee"/>'
                 f'<text x="{X(t):.1f}" y="{H - B + 15}" text-anchor="middle">{t:.0f}</text>')
    for t in _ticks(y0, y1):
        o.append(f'<line x1="{L}" y1="{Y(t):.1f}" x2="{W - R}" y2="{Y(t):.1f}" stroke="#eee"/>'
                 f'<text x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:.0f}</text>')
    o.append(f'<text x="{(W + L) / 2}" y="{H - 12}" text-anchor="middle">Snit effekt (W)</text>')
    o.append(f'<text x="14" y="{(H - B) / 2}" transform="rotate(-90 14 {(H - B) / 2})" '
             f'text-anchor="middle">Billeder pr. time</text>')
    ver = {id(s) for s in reco["verified"]}
    for s in steps:
        if s.get("status") != "pass" or not s.get("img_per_hour") or not s.get("avg_power_w"):
            continue
        if s.get("phase") == "baseline":
            continue
        big = id(s) in ver
        o.append(f'<circle cx="{X(s["avg_power_w"]):.1f}" cy="{Y(s["img_per_hour"]):.1f}" '
                 f'r="{6 if big else 4}" fill="{"#4a69bd" if big else "#bbb"}"/>')
    b = reco["baseline"]
    if b:
        cx, cy = X(b["avg_power_w"]), Y(b["img_per_hour"])
        o.append(f'<rect x="{cx - 6:.1f}" y="{cy - 6:.1f}" width="12" height="12" fill="#333" '
                 f'transform="rotate(45 {cx:.1f} {cy:.1f})"/>'
                 f'<text x="{cx + 10:.1f}" y="{cy + 4:.1f}">Standard</text>')
    for key, title, _ in PICKS:
        p = reco.get(key)
        if p:
            cx, cy = X(p["avg_power_w"]), Y(p["img_per_hour"])
            nr = title.split(".")[0]
            dx = {"most_uv": -14, "least": 0, "fastest": 14}[key]
            o.append(f'<circle cx="{cx + dx:.1f}" cy="{cy - 18:.1f}" r="9" fill="{COLORS[key]}"/>'
                     f'<text x="{cx + dx:.1f}" y="{cy - 14:.1f}" fill="#fff" text-anchor="middle" '
                     f'font-weight="bold">{nr}</text>'
                     f'<line x1="{cx + dx:.1f}" y1="{cy - 9:.1f}" x2="{cx:.1f}" y2="{cy:.1f}" '
                     f'stroke="{COLORS[key]}"/>')
    o.append("</svg>")
    return "".join(o)


def svg_bars(reco):
    b = reco["baseline"]
    picks = [(k, t, reco[k]) for k, t, _ in PICKS if reco.get(k)]
    if not b or not picks:
        return ""
    metrics = [("Ydelse", "img_per_hour"), ("Effekt", "avg_power_w"),
               ("Wh pr. billede", "wh_per_img"), ("Blæser", "avg_fan_rpm")]
    vals = [[delta(p.get(k), b.get(k)) or 0 for _, k in metrics] for _, _, p in picks]
    m = max(5, max(abs(v) for row in vals for v in row)) * 1.15
    W, H, L, T, B = 640, 320, 50, 20, 60
    Y = lambda v: T + (m - v) / (2 * m) * (H - T - B)  # noqa: E731
    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    for t in _ticks(-m, m, 4):
        o.append(f'<line x1="{L}" y1="{Y(t):.1f}" x2="{W - 10}" y2="{Y(t):.1f}" '
                 f'stroke="{"#999" if abs(t) < 1e-9 else "#eee"}"/>'
                 f'<text x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:+.0f} %</text>')
    gw = (W - L - 10) / len(metrics)
    bw = (gw - 30) / len(picks)
    for mi, (name, _) in enumerate(metrics):
        gx = L + mi * gw + 15
        o.append(f'<text x="{gx + (gw - 30) / 2:.1f}" y="{H - B + 18}" text-anchor="middle">{name}</text>')
        for pi, (k, _, _) in enumerate(picks):
            v = vals[pi][mi]
            y, h = (Y(v), Y(0) - Y(v)) if v >= 0 else (Y(0), Y(v) - Y(0))
            o.append(f'<rect x="{gx + pi * bw:.1f}" y="{y:.1f}" width="{bw - 3:.1f}" height="{h:.1f}" '
                     f'fill="{COLORS[k]}"><title>{v:+.1f} %</title></rect>')
    lx = L
    for k, t, _ in picks:
        o.append(f'<rect x="{lx}" y="{H - 22}" width="12" height="12" fill="{COLORS[k]}"/>'
                 f'<text x="{lx + 16}" y="{H - 12}">{e(t)}</text>')
        lx += 200
    o.append("</svg>")
    return "".join(o)


# ---------- rapport ----------
def build(run_dir, state=None):
    run_dir = Path(run_dir)
    st = state or S.read_json(S.STATE) or {}
    if st.get("run_dir") != str(run_dir):
        st = {}
    cfg = st.get("cfg") or {}
    steps = S.read_jsonl(run_dir / "steps.jsonl")
    events = S.read_jsonl(run_dir / "events.jsonl")
    reco = recommend(steps, cfg)
    base = reco["baseline"]
    run_id = run_dir.name
    started = min((s.get("started") or 1e18 for s in steps), default=None)
    ended = st.get("finished_at") or max((ev.get("t", 0) for ev in events), default=None)
    tol = cfg.get("perf_tolerance_pct", 2)

    h = [f"""<!DOCTYPE html><html lang="da"><head><meta charset="utf-8">
<title>UV Tune-rapport {e(run_id)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;color:#222}}
h1{{color:#c0392b;margin-bottom:.2em}} h2{{border-bottom:2px solid #eee;padding-bottom:.2em;margin-top:2em}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #ddd;padding:4px 6px;text-align:left}}
th{{background:#f6f6f6}} tr.fail td{{background:#fff3f3}} .good{{color:#1e8449}} .bad{{color:#c0392b}}
.cards{{display:flex;gap:1em;flex-wrap:wrap}} .card{{flex:1;min-width:280px;border:1px solid #ddd;border-radius:8px;padding:1em}}
.card h3{{margin-top:0}} .lact{{background:#f6f6f6;padding:.5em;border-radius:6px}} pre{{background:#f6f6f6;padding:.6em;
overflow-x:auto;font-size:12px;white-space:pre-wrap}} .muted{{color:#777;font-size:13px}}
</style></head><body>
<h1>UV Tune – testrapport</h1>
<p class="muted">Kørsel {e(run_id)} · {e(st.get("gpu_name", ""))} · start {fmt_t(started)} · slut {fmt_t(ended)}
· status: {e(STATUS.get(st.get("status"), st.get("status", "ukendt")))}</p>"""]
    if st.get("message"):
        h.append(f"<p>{e(st['message'])}</p>")

    h.append("<h2>Anbefalede indstillinger</h2>")
    if not base:
        h.append("<p>Ingen referencemåling blev gennemført.</p>")
    else:
        h.append(f"<p>Reference (standard, {num(base['cap_w'])} W, 0 mV): {num(base['img_per_hour'])} billeder/t, "
                 f"{num(base['avg_power_w'])} W, junction maks {num(base['max_junction_c'])} °C, "
                 f"blæser {num(base['avg_fan_rpm'])} rpm.</p>")
        if not reco["verified"]:
            h.append("<p>Ingen indstilling nåede at bestå den lange verifikation.</p>")
        else:
            h.append('<div class="cards">')
            seen = {}
            for key, title, note in PICKS:
                p = reco.get(key)
                if not p:
                    continue
                dj = (p.get("max_junction_c") or 0) - (base.get("max_junction_c") or 0)
                warn = ""
                if key == "least" and not reco["least_within_tol"]:
                    warn = f"<p class='bad'>Ingen stabil indstilling var inden for {tol} %; viser den med mindst tab.</p>"
                if p.get("n") in seen:
                    warn += f"<p class='muted'>Samme indstilling som {seen[p['n']]}</p>"
                seen.setdefault(p.get("n"), title.split(".")[0] + ".")
                h.append(f"""<div class="card" style="border-top:4px solid {COLORS[key]}"><h3>{e(title)}</h3>
<p class="muted">{e(note.format(tol=tol))}</p>{warn}
<p class="lact"><b>Sæt i LACT:</b> Power limit <b>{num(p['cap_w'])} W</b>, voltage offset <b>{num(p['offset_mv'])} mV</b></p>
<p>Ydelse: {num(p['img_per_hour'])} billeder/t {pct(delta(p['img_per_hour'], base['img_per_hour']))}<br>
Effekt: {num(p['avg_power_w'])} W {pct(delta(p['avg_power_w'], base['avg_power_w']), True)}<br>
Wh pr. billede: {num(p.get('wh_per_img'))} {pct(delta(p.get('wh_per_img'), base.get('wh_per_img')), True)}<br>
Junction maks: {num(p['max_junction_c'])} °C ({dj:+.1f} °C)<br>
Blæser: {num(p['avg_fan_rpm'])} rpm {pct(delta(p['avg_fan_rpm'], base['avg_fan_rpm']), True)}</p></div>""")
            h.append("</div>")
            h.append("<h2>Sammenligning</h2>")
            h.append("<p class='muted'>Grå: beståede korte tests. Blå: stabile (verificeret). "
                     "Sort: standard. 1–3: de tre valg.</p>")
            h.append(svg_scatter(steps, reco))
            h.append("<p class='muted'>Ændring i forhold til standard. Ydelse: plus er godt. "
                     "Effekt, Wh pr. billede og blæser: minus er godt.</p>")
            h.append(svg_bars(reco))

    caps = (st.get("search") or {}).get("cap_results") or []
    if caps:
        h.append("<h2>Resultat pr. power limit</h2><table><tr><th>Power limit</th><th>Grænse i screening</th>"
                 "<th>Bekræftet</th><th>Verificeret (med margin)</th><th>Note</th></tr>")
        for c in caps:
            h.append(f"<tr><td>{num(c['cap_w'])} W</td><td>{num(c.get('screen_limit_mv'), ' mV')}</td>"
                     f"<td>{num(c.get('confirmed_mv'), ' mV')}</td><td>{num(c.get('offset_mv'), ' mV')}</td>"
                     f"<td>{e(c.get('note'))}</td></tr>")
        h.append("</table>")

    h.append("<h2>Alle trin</h2><table><tr><th>#</th><th>Fase</th><th>W</th><th>mV</th><th>Resultat</th>"
             "<th>Min</th><th>Billeder</th><th>Billeder/t</th><th>Snit W</th><th>Wh/billede</th>"
             "<th>MHz</th><th>Vddgfx</th><th>Junction maks</th><th>Blæser</th><th>Billede</th></tr>")
    for s in steps:
        ok = s.get("status") == "pass"
        res = "Stabil" if ok else ("Afbrudt" if s.get("status") == "aborted"
                                   else REASONS.get(s.get("reason"), s.get("reason")))
        h.append(f"<tr class='{'' if ok else 'fail'}'><td>{num(s.get('n'))}</td>"
                 f"<td>{e(PHASES.get(s.get('phase'), s.get('phase')))}</td><td>{num(s.get('cap_w'))}</td>"
                 f"<td>{num(s.get('offset_mv'))}</td><td>{e(res)}</td>"
                 f"<td>{num(round(s['duration_s'] / 60, 1) if s.get('duration_s') else None)}</td>"
                 f"<td>{num(s.get('images'))}</td><td>{num(s.get('img_per_hour'))}</td>"
                 f"<td>{num(s.get('avg_power_w'))}</td><td>{num(s.get('wh_per_img'))}</td>"
                 f"<td>{num(s.get('avg_sclk_mhz'))}</td><td>{num(s.get('avg_vddgfx_mv'))}</td>"
                 f"<td>{num(s.get('max_junction_c'))}</td><td>{num(s.get('avg_fan_rpm'))}</td>"
                 f"<td>{num(round(s['min_psnr'], 1) if isinstance(s.get('min_psnr'), float) and not math.isinf(s['min_psnr']) else s.get('min_psnr'))}</td></tr>")
    h.append("</table>")

    fails = [s for s in steps if s.get("status") == "fail"]
    if fails:
        h.append("<h2>Fejl</h2>")
        for s in fails:
            h.append(f"<h3>Trin {num(s.get('n'))}: {num(s.get('cap_w'))} W, {num(s.get('offset_mv'))} mV – "
                     f"{e(REASONS.get(s.get('reason'), s.get('reason')))}</h3>")
            if s.get("detail"):
                h.append(f"<pre>{e(s['detail'])[:4000]}</pre>")
            klog = s.get("kernel_log")
            if klog and os.path.exists(klog):
                try:
                    lines = open(klog, errors="replace").read().splitlines()[:40]
                    h.append(f"<p class='muted'>Kernel-log ({e(klog)}):</p><pre>{e(chr(10).join(lines))}</pre>")
                except OSError:
                    pass

    if cfg:
        h.append("<h2>Testindstillinger</h2><table>")
        for k in ("power_caps_pct", "coarse_step_mv", "fine_step_mv", "min_offset_mv", "safety_margin_mv",
                  "warmup_minutes", "screen_minutes", "confirm_minutes", "verify_minutes",
                  "perf_tolerance_pct", "comfy_command"):
            if k in cfg:
                h.append(f"<tr><th>{e(k)}</th><td>{e(cfg[k])}</td></tr>")
        h.append("</table>")
    h.append("<p class='muted'>Lavet med UV Tune.</p></body></html>")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for s in steps:
        w.writerow(s)
    return "\n".join(h), buf.getvalue()


def save(run_dir, dest_dir, state=None):
    """Gem rapport i run-mappen og i dest_dir (hvis angivet). Returnerer stien til HTML-filen."""
    run_dir = Path(run_dir)
    html_txt, csv_txt = build(run_dir, state)
    S.write_text(run_dir / "rapport.html", html_txt)
    S.write_text(run_dir / "trin.csv", csv_txt)
    if not dest_dir:
        return run_dir / "rapport.html"
    dest = S.ensure_dir(Path(os.path.expanduser(str(dest_dir))))
    base = f"uvtune-rapport-{run_dir.name}"
    S.write_text(dest / f"{base}.html", html_txt)
    S.write_text(dest / f"{base}.csv", csv_txt)
    return dest / f"{base}.html"
