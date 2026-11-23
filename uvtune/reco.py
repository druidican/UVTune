"""Udvælgelse af de bedste indstillinger blandt de verificerede (stabile) resultater.

1. most_uv  – mest undervoltet: laveste voltage offset, der bestod verifikationen
2. least    – mindst negativ effekt: laveste strømforbrug, hvor ydelsen højst er
              'perf_tolerance_pct' under standard (findes ingen, den med mindst tab)
3. fastest  – bedst positiv effekt: højeste ydelse
"""

PICKS = [
    ("most_uv", "1. Mest undervoltet, stabil",
     "Laveste spænding, der bestod den lange verifikation."),
    ("least", "2. Mindst negativ effekt",
     "Laveste strømforbrug, hvor ydelsen højst er {tol} % under standard."),
    ("fastest", "3. Bedst positiv effekt",
     "Højeste ydelse blandt de stabile indstillinger."),
]


def recommend(steps, cfg):
    tol = float(cfg.get("perf_tolerance_pct", 2.0)) / 100
    base = next((s for s in steps if s.get("phase") == "baseline" and s.get("status") == "pass"), None)
    verified = [s for s in steps if s.get("phase") == "verify" and s.get("status") == "pass"
                and s.get("img_per_hour")]
    out = {"baseline": base, "verified": verified, "most_uv": None, "least": None,
           "fastest": None, "least_within_tol": False}
    if not verified:
        return out
    out["most_uv"] = min(verified, key=lambda s: (s["offset_mv"], s.get("avg_power_w") or 1e9))
    out["fastest"] = max(verified, key=lambda s: (s["img_per_hour"], -(s.get("avg_power_w") or 0)))
    if base:
        floor = base["img_per_hour"] * (1 - tol)
        ok = [s for s in verified if s["img_per_hour"] >= floor]
        if ok:
            out["least"] = min(ok, key=lambda s: (s.get("avg_power_w") or 1e9, s.get("avg_fan_rpm") or 0))
            out["least_within_tol"] = True
        else:
            out["least"] = out["fastest"]
    # bagudkompatibelt navn
    out["best"] = out["least"]
    return out


def delta(new, old):
    if new is None or not old:
        return None
    return (new - old) / old * 100
