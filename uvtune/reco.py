"""Picks the best settings among the verified (stable) results.

1. most_uv  – most undervolted: the lowest voltage offset that passed verification
2. least    – least negative impact: the lowest power draw where performance is at most
              'perf_tolerance_pct' below stock (if none, the one with the smallest loss)
3. fastest  – best positive impact: the highest performance
"""

PICKS = [
    ("most_uv", "1. Most undervolted, stable",
     "The lowest voltage that passed the long verification."),
    ("least", "2. Least negative impact",
     "The lowest power draw where performance is at most {tol} % below stock."),
    ("fastest", "3. Best positive impact",
     "The highest performance among the stable settings."),
]


def recommend(steps, cfg):
    tol = float(cfg.get("perf_tolerance_pct", 2.0)) / 100
    base = next((s for s in steps if s.get("phase") == "baseline" and s.get("status") == "pass"), None)
    verified = [s for s in steps if s.get("phase") in ("verify", "manual") and s.get("status") == "pass"
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
    # backwards-compatible name
    out["best"] = out["least"]
    return out


def delta(new, old):
    if new is None or not old:
        return None
    return (new - old) / old * 100
