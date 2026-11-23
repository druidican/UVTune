"""Search logic: find the lowest stable voltage offset for each power limit.

Order:
  baseline – reference at stock (5 min, also warms the card up)
  coarse   – screening, short tests: 25 mV down at a time until the first failure
  up       – (only if the starting point fails) 25 mV up at a time until it holds
  fine     – screening, short tests: 5 mV down towards the failure limit
  confirm  – longer test from the most aggressive setting that passed screening;
             if it fails, 5 mV up at a time
  verify   – long final test at confirmed + safety margin
The next power limit starts screening at the previous limit's confirmed offset.
"""

SHORT = ("coarse", "up", "fine")


class Search:
    def __init__(self, d):
        self.d = d

    @classmethod
    def new(cls, caps_w, cfg, floor_mv):
        return cls({
            "caps": caps_w, "cap_idx": 0, "phase": "baseline", "offset": 0,
            "last_good": None, "first_fail": None, "confirmed": None, "start_next": 0,
            "coarse": abs(int(cfg["coarse_step_mv"])), "fine": abs(int(cfg["fine_step_mv"])),
            "margin": abs(int(cfg["safety_margin_mv"])), "floor": int(floor_mv),
            "cap_results": [], "error": None,
        })

    def to_dict(self):
        return self.d

    @property
    def done(self):
        return self.d["phase"] == "done"

    def current(self):
        if self.done:
            return None
        d = self.d
        return {"cap_w": d["caps"][d["cap_idx"]], "offset_mv": d["offset"], "phase": d["phase"]}

    def record(self, passed):
        d = self.d
        ph = d["phase"]
        if ph == "baseline":
            if not passed:
                d["error"] = "Stock settings were not stable. The test is stopped."
                d["phase"] = "done"
                return
            d["last_good"] = 0
            self._next_coarse()
        elif ph == "coarse":
            if passed:
                d["last_good"] = d["offset"]
                self._next_coarse()
            else:
                d["first_fail"] = d["offset"]
                if d["last_good"] is None:
                    self._up()
                else:
                    self._start_fine()
        elif ph == "up":
            if passed:
                d["last_good"] = d["offset"]
                self._start_fine()
            else:
                d["first_fail"] = d["offset"]
                self._up()
        elif ph == "fine":
            if passed:
                d["last_good"] = d["offset"]
                nxt = d["offset"] - d["fine"]
                if nxt <= d["first_fail"] or nxt < d["floor"]:
                    self._start_confirm()
                else:
                    d["offset"] = nxt
            else:
                d["first_fail"] = d["offset"]
                self._start_confirm()
        elif ph == "confirm":
            if passed:
                d["confirmed"] = d["offset"]
                d["phase"] = "verify"
                d["offset"] = min(0, d["offset"] + d["margin"])
            else:
                self._step_up("No setting passed confirmation")
        elif ph == "verify":
            if passed:
                self._cap_result(d["offset"], "Stable")
                self._next_cap()
            else:
                self._step_up("Verification failed all the way up to 0 mV")

    def _step_up(self, note):
        d = self.d
        nxt = d["offset"] + d["fine"]
        if nxt > 0:
            self._cap_result(None, note)
            self._next_cap()
        else:
            d["offset"] = nxt

    def _up(self):
        d = self.d
        if d["offset"] >= 0:
            self._cap_result(None, "Unstable already at 0 mV")
            self._next_cap()
        else:
            d["phase"] = "up"
            d["offset"] = min(0, d["offset"] + d["coarse"])

    def _next_coarse(self):
        d = self.d
        nxt = d["offset"] - d["coarse"]
        if nxt < d["floor"]:
            if d["offset"] > d["floor"]:
                d["offset"], d["phase"] = d["floor"], "coarse"
            else:
                self._start_confirm()
        else:
            d["offset"], d["phase"] = nxt, "coarse"

    def _start_fine(self):
        d = self.d
        d["phase"] = "fine"
        d["offset"] = d["last_good"] - d["fine"]
        if d["offset"] <= d["first_fail"]:
            self._start_confirm()

    def _start_confirm(self):
        d = self.d
        d["phase"] = "confirm"
        d["offset"] = d["last_good"]

    def _cap_result(self, offset, note):
        d = self.d
        d["cap_results"].append({"cap_w": d["caps"][d["cap_idx"]], "offset_mv": offset,
                                 "confirmed_mv": d.get("confirmed"),
                                 "screen_limit_mv": d["last_good"], "note": note})
        if offset is not None and d.get("confirmed") is not None:
            d["start_next"] = d["confirmed"]

    def _next_cap(self):
        d = self.d
        d["cap_idx"] += 1
        if d["cap_idx"] >= len(d["caps"]):
            d["phase"] = "done"
            return
        d.update(phase="coarse", offset=max(d["floor"], d["start_next"]), last_good=None,
                 first_fail=None, confirmed=None)


def parse_manual(text):
    """Parse the manual test list: one setting per line (or separated by ';'),
    each as watts and mV, e.g. "254 -80", "254W / -80mV". Returns (items, errors)."""
    import re
    items, errors = [], []
    for raw in re.split(r"[\n;]+", str(text or "")):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        nums = re.findall(r"-?\d+(?:\.\d+)?", entry)
        if len(nums) != 2:
            errors.append(f"'{entry}': write watts and mV, e.g. 254 -80")
            continue
        watts, mv = float(nums[0]), int(round(float(nums[1])))
        if watts <= 0:
            errors.append(f"'{entry}': the power limit must be above 0 W")
        elif mv > 0:
            errors.append(f"'{entry}': the voltage offset must be 0 or negative (e.g. -80)")
        else:
            items.append({"cap_w": round(watts), "offset_mv": mv})
    return items, errors


class ManualSearch:
    """Manual mode: test a list of (power limit, offset) settings, each for manual_minutes.
    If a setting fails, it is retried fine_step_mv closer to 0 until one holds."""

    def __init__(self, d):
        self.d = d

    @classmethod
    def new(cls, items, cfg, default_w):
        return cls({
            "kind": "manual", "items": items, "idx": 0, "phase": "baseline",
            "base_cap": default_w, "offset": 0, "fine": abs(int(cfg["fine_step_mv"])),
            "cap_results": [], "error": None,
        })

    def to_dict(self):
        return self.d

    @property
    def done(self):
        return self.d["phase"] == "done"

    def current(self):
        d = self.d
        if self.done:
            return None
        if d["phase"] == "baseline":
            return {"cap_w": d["base_cap"], "offset_mv": 0, "phase": "baseline"}
        return {"cap_w": d["items"][d["idx"]]["cap_w"], "offset_mv": d["offset"], "phase": "manual"}

    def record(self, passed):
        d = self.d
        if d["phase"] == "baseline":
            if not passed:
                d["error"] = "Stock settings were not stable. The test is stopped."
                d["phase"] = "done"
                return
            self._start_item()
            return
        item = d["items"][d["idx"]]
        if passed:
            note = "Stable as requested" if d["offset"] == item["offset_mv"] else \
                f"Stable after stepping up from {item['offset_mv']} mV"
            self._result(d["offset"], note)
            self._next_item()
            return
        nxt = d["offset"] + d["fine"]
        if nxt > 0:
            self._result(None, "Failed even at 0 mV")
            self._next_item()
        else:
            d["offset"] = nxt

    def _result(self, offset, note):
        item = self.d["items"][self.d["idx"]]
        self.d["cap_results"].append({"cap_w": item["cap_w"], "requested_mv": item["offset_mv"],
                                      "offset_mv": offset, "note": note})

    def _start_item(self):
        d = self.d
        d["phase"] = "manual"
        d["offset"] = d["items"][d["idx"]]["offset_mv"]

    def _next_item(self):
        d = self.d
        d["idx"] += 1
        if d["idx"] >= len(d["items"]):
            d["phase"] = "done"
        else:
            self._start_item()


def from_dict(d):
    return ManualSearch(d) if d.get("kind") == "manual" else Search(d)
