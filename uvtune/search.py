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
