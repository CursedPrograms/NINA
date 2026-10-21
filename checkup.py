"""
checkup.py - NINA's checkups: she asks every robot how it is and watches its sensors.

How each robot is right now (its state): moving or standing still, turning, wheels
locked, playing music, its mode. Read from the robot's own status page every
POLL_S (pull), or sent by the robot itself (POST /report on NINA's page:
{"name", "state": {...}, "sensors": {"front": 23.5, ...}}).

A distance sensor that reads nothing (0, -1 or 999) or exactly the same value for
StuckMinutes is a symptom. Then she asks the robot whether it's standing still:
  - it's moving, yet the reading never changes -> the sensor is stuck: tell the user
  - it's standing still -> ask it to move a little and measure again, like a doctor:
    the reading changed = healthy, it was only parked; unchanged = tell the user
  - she can't tell, or can't move it (wheels locked, no PIN) -> tell the user what
    she saw and what would let her test it
She only moves a robot that is standing still in manual mode, a short nudge out and
back, and only with the key the user gave her (Passwords in config.json).

She also watches for a robot in distress, and only warns (she never stops one herself):
  - moving nonstop for DistressMinutes
  - turning in nearly every reading for PanicMinutes: bouncing off walls
  - moving while another robot's radio stays right on top of it for GluedMinutes (RIFT's
    /near): something stuck to it, like RITA glued onto NORA
"""

import re
import threading
import time

import requests

POLL_S = 15                 # each robot's status page, at most this often (ESP web servers are slow)
RECHECK_S = 2 * 3600        # after a checkup, leave that sensor alone this long
NUDGE_S = 0.4               # how long the little move lasts
CHANGED_CM = 1.0            # a real change, not sensor jitter
DIST_KEY = re.compile(r"dist|ultra|sonar|range|front|back|left|right", re.I)
NORA_MODES = ["manual", "auto", "line", "remote"]
WHIP_MODES = ["obstacle", "ir", "web"]
WHIP_STEP_S = 1.5           # WHIP walks: a 0.4 s nudge is less than a step
PANIC_SHARE = 0.75          # turning in this share of the readings = panic laps
GLUED_RSSI = -35            # dBm: stronger than this is right on top of her (very close starts at -45)
DISTRESS_RECHECK_S = 30 * 60   # after a distress warning, the same one waits this long


def dead(v):
    """No reading: 0 / -1 (no echo) / 999 (out of range)."""
    return v is None or v <= 0 or v >= 999


def read_state(name, d):
    """(state, sensors) from a robot's own status JSON. state: moving / turning / wheels_locked /
    music / mode (None = it doesn't say); sensors: {label: cm}."""
    n = name.upper()
    st = {"moving": None, "turning": None, "wheels_locked": None, "music": None, "mode": None}
    if n == "NORA":                                      # /sensors
        locked = d.get("lock") == 1
        mode = d.get("mode")
        st.update(wheels_locked=locked, mode=NORA_MODES[mode] if isinstance(mode, int) and 0 <= mode < 4 else mode,
                  moving=False if locked else (True if mode in (1, 2) else None),
                  music={0: "stopped", 1: f"playing track {d.get('mt')}", 2: "paused"}.get(d.get("ms")))
        sensors = {side: d.get(k) for k, side in (("F", "front"), ("L", "left"), ("B", "back"), ("R", "right"))}
    elif n.startswith("MILA"):                           # /status
        ml, mr = d.get("ml"), d.get("mr")
        if isinstance(ml, int) and isinstance(mr, int):
            st.update(moving=bool(ml or mr), turning=bool(ml or mr) and ml != mr)
        st.update(mode=d.get("mode"))
        sensors = {"front": d.get("dist")}
    elif n.startswith("WHIP"):                           # /status (esp32.ino handleStatus)
        move = d.get("move")                             # 0 stand, 1 walk, 2 backward, 3/4 turning
        if isinstance(move, int):
            st.update(moving=move != 0, turning=move in (3, 4))
        mode = d.get("controlMode")
        st.update(mode=WHIP_MODES[mode] if isinstance(mode, int) and 0 <= mode < 3 else mode)
        sensors = {"front": d.get("distance")}
    elif n.startswith("KIDA") and "direction" in d:      # KIDA00's /status (shared_state.py): no distance sensors
        direction = str(d.get("direction", "")).upper()
        st.update(moving=d["moving"] if isinstance(d.get("moving"), bool) else direction not in ("STOPPED", "STOP", ""),
                  turning=direction in ("LEFT", "RIGHT"), mode=d.get("mode"),
                  wheels_locked=bool(d["motor_lock"]) if "motor_lock" in d else None,
                  music=d.get("music") if d.get("music") in ("playing", "stopped") else
                        ("playing" if d.get("music_playing") else "stopped") if "music_playing" in d else None)
        sensors = {}
    elif n.startswith("KIDA") and isinstance(d.get("sensors"), dict):   # KIDA01's /status (server.py)
        st.update(wheels_locked=bool(d.get("motor_lock")), mode=d.get("mode"),
                  music="playing" if d.get("music_on") else "stopped")       # she doesn't say whether she's moving
        sensors = {k.lower(): v for k, v in d["sensors"].items() if k in ("US0", "US1")}
    elif n == "ARM":                                     # controller.py's fleet /status: servos, no sensors
        st.update(mode="board connected" if d.get("connected") else "board unplugged",
                  moving=False if d.get("connected") is False else None)
        sensors = {}
    else:                                                # anyone else: the fields it happens to have
        st.update(moving=d.get("moving") if isinstance(d.get("moving"), bool) else None,
                  turning=d.get("turning") if isinstance(d.get("turning"), bool) else None,
                  wheels_locked=d.get("motor_lock", d.get("wheels_locked")),
                  music=("playing" if d.get("music_on") else "stopped") if "music_on" in d else d.get("music"),
                  mode=d.get("mode"))
        sensors = {k: v for k, v in d.items() if DIST_KEY.search(k) and isinstance(v, (int, float)) and not isinstance(v, bool)}
    sensors = {k: float(v) for k, v in sensors.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return st, sensors


def describe(st):
    """A robot's state in a few words."""
    if not st:
        return "unknown"
    bits = []
    if st.get("wheels_locked"):
        bits.append("wheels locked")
    if st.get("moving") is True:
        bits.append("turning" if st.get("turning") else "moving")
    elif st.get("moving") is False:
        bits.append("standing still")
    if st.get("mode"):
        bits.append(st["mode"] if str(st["mode"]).startswith("board ") else f"{st['mode']} mode")
    if st.get("music") and st["music"] != "stopped":
        bits.append(f"music {st['music']}")
    return ", ".join(bits) or "it doesn't say"


class Checkups:
    def __init__(self, cfg, note):
        self.cfg, self.note = cfg, note
        self.stuck_s = cfg.get("StuckMinutes", 10) * 60
        self.passwords = cfg.get("Passwords", {}) or {}
        self.robots = {}          # name -> {"url", "state", "sensors", "at", "polled"}
        self.watch = {}           # (name, sensor) -> {"value", "since", "next"}
        self._busy = set()        # robots being checked right now
        self.distress_s = cfg.get("DistressMinutes", 15) * 60   # 0 = off, for each of the three
        self.panic_s = cfg.get("PanicMinutes", 3) * 60
        self.glued_s = cfg.get("GluedMinutes", 5) * 60
        self.moves = {}           # name -> {"since": moving since, "turns": [(t, turning)]}
        self.glued = {}           # (name, other) -> since when the other's radio is on top of it
        self.warned = {}          # distress warning -> not again before

    # ---- readings in
    def poll(self, name, url, path="/status"):
        """Ask a robot how it is (its status page), at most every POLL_S."""
        r = self.robots.setdefault(name, {"url": url, "state": {}, "sensors": {}, "at": 0, "polled": 0})
        r["url"] = url or r["url"]
        if not r["url"] or time.time() - r["polled"] < POLL_S:
            return
        r["polled"] = time.time()
        try:
            d = requests.get(r["url"].rstrip("/") + path, timeout=3).json()
        except (requests.RequestException, ValueError):
            return
        if isinstance(d, dict):
            self.feed(name, *read_state(name, d))

    def take(self, name, url, d):
        """Readings NINA already fetched herself (NORA's /sensors in her own NORA check)."""
        r = self.robots.setdefault(name, {"url": url, "state": {}, "sensors": {}, "at": 0, "polled": 0})
        r["url"] = url
        if isinstance(d, dict):
            self.feed(name, *read_state(name, d))

    def report(self, body):
        """POST /report from a robot that sends its own readings."""
        name = str(body.get("name") or "").strip()
        if not name:
            return False
        state = {k: body.get("state", {}).get(k) for k in ("moving", "turning", "wheels_locked", "music", "mode")}
        sensors = {k: float(v) for k, v in (body.get("sensors") or {}).items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
        self.robots.setdefault(name, {"url": None, "state": {}, "sensors": {}, "at": 0, "polled": 0})
        self.feed(name, state, sensors)
        return True

    def feed(self, name, state, sensors, now=None):
        """New readings: note what changed about the robot, then look at each sensor."""
        now = now or time.time()
        r = self.robots.setdefault(name, {"url": None, "state": {}, "sensors": {}, "at": 0, "polled": 0})
        old = r["state"]
        if old and describe(old) != describe(state):
            self.note("info", f"{name}: {describe(state)}.")
        r.update(state=state, sensors=sensors, at=now)
        self._distress(name, state, now)
        for label, v in sensors.items():
            w = self.watch.setdefault((name, label), {"value": v, "since": now, "next": 0})
            if v != w["value"]:
                w["value"], w["since"] = v, now
                continue
            if now - w["since"] >= self.stuck_s and now >= w["next"] and name not in self._busy:
                w["next"] = now + RECHECK_S
                self._busy.add(name)
                threading.Thread(target=self._checkup, args=(name, label, v, now - w["since"]),
                                 daemon=True, name=f"checkup-{name}").start()

    # ---- distress: warn only
    def _warn(self, key, now, text, why):
        if now < self.warned.get(key, 0):
            return
        self.warned[key] = now + DISTRESS_RECHECK_S
        self.note("problem", text, why, speak=True, mission=True)

    def _distress(self, name, st, now):
        m = self.moves.setdefault(name, {"since": None, "turns": []})
        if st.get("moving") is False or st.get("wheels_locked"):
            m["since"], m["turns"] = None, []
            return
        if st.get("moving") is not True:
            return                                       # it doesn't say: keep what we had
        m["since"] = m["since"] or now
        if self.distress_s and now - m["since"] >= self.distress_s:
            self._warn(f"run-{name}", now, f"{name} has been moving nonstop for {int((now - m['since']) // 60)} min.",
                       "If that isn't on purpose (something stuck to her, a sensor fooling her), "
                       "lock her wheels: RIFT's Lock all, or her own lock.")
        if st.get("turning") is not None:
            m["turns"] = [t for t in m["turns"] if now - t[0] <= self.panic_s] + [(now, bool(st["turning"]))]
            turns = m["turns"]
            if (self.panic_s and len(turns) >= 4 and now - turns[0][0] >= self.panic_s * 0.9
                    and sum(t for _, t in turns) / len(turns) >= PANIC_SHARE):
                self._warn(f"panic-{name}", now, f"{name} keeps turning: {int(self.panic_s // 60)} min of laps, "
                           "like she's bouncing off walls.", "Is she trapped, or running from something? "
                           "Have a look, and lock her wheels if she's in trouble.")

    def near(self, data, now=None):
        """RIFT's /near: whose radio each robot hears, how strongly. A robot that's moving while
        another stays right on top of it for GluedMinutes has something stuck to it."""
        now = now or time.time()
        seen = set()
        for name, st in ((data or {}).get("robots") or {}).items():
            moving = self.robots.get(name, {}).get("state", {}).get("moving")
            for peer in (st or {}).get("robots", []) or []:
                other, rssi = peer.get("name"), peer.get("rssi")
                if not other or not isinstance(rssi, (int, float)) or rssi < GLUED_RSSI or moving is not True:
                    continue
                key = (name, other)
                seen.add(key)
                since = self.glued.setdefault(key, now)
                if self.glued_s and now - since >= self.glued_s:
                    self._warn(f"glued-{name}-{other}", now,
                               f"{other}'s radio has been right on top of {name} for {int((now - since) // 60)} min "
                               f"({rssi:g} dBm) while she moves.",
                               f"Is {other} stuck to her? She can't get away from it. Lock her wheels if so.")
        for key in list(self.glued):
            if key not in seen:
                del self.glued[key]

    # ---- the checkup
    def _checkup(self, name, label, v, for_s):
        try:
            self.checkup(name, label, v, for_s)
        except Exception as e:      # a checkup must never stop her watching
            self.note("warn", f"My checkup of {name} failed: {e}")
        finally:
            self._busy.discard(name)

    def checkup(self, name, label, v, for_s):
        what = f"{name}'s {label} sensor"
        reading = "has read nothing" if dead(v) else f"has read {v:g} cm"
        mins = int(for_s // 60)
        st = self.robots[name]["state"]
        moving = st.get("moving")
        self.note("info", f"{what} {reading} for {mins} min. {name}, are you standing still?",
                  f"{name}: {describe(st)}.")
        if moving is True:
            self.note("problem", f"{what} {reading} for {mins} min while {name} is moving.",
                      "It's probably stuck or loose: check its wiring.", speak=True, mission=True)
            return
        if moving is None:
            self.note("warn", f"{what} {reading} for {mins} min, and I can't tell whether {name} is moving.",
                      "Have a look at it, or let it report whether it's moving.", speak=dead(v))
            return
        moved = self.nudge(name, label)
        if isinstance(moved, str):                      # couldn't test it
            if dead(v):
                self.note("warn", f"{what} reads nothing and {name} is standing still. I couldn't test it: {moved}",
                          "Check the sensor, or let me move her a little.", speak=True)
            else:
                self.note("info", f"{name} is just standing still, so {v:g} cm is probably right.",
                          f"I couldn't test it: {moved}")
            return
        before, after = v, moved.get(label)
        if after is not None and not dead(after) and abs(after - before) >= CHANGED_CM:
            self.note("info", f"{what} is healthy: {before:g} -> {after:g} cm when {name} moved a little.",
                      f"{name} was only standing still.")
        else:
            self.note("problem", f"{what} didn't change when {name} moved a little ({before:g} -> {after if after is not None else '?'}).",
                      "It's probably broken or unplugged: check its wiring.", speak=True, mission=True)

    def nudge(self, name, label):
        """Move a little (a side sensor: turn; front/back: forward), read, move back.
        The readings while moved, or why she couldn't."""
        r = self.robots[name]
        url = (r["url"] or "").rstrip("/")
        n, pw = name.upper(), self.passwords.get(name.upper(), "")
        sideways = label in ("left", "right")
        get = lambda path, **p: requests.get(url + path, params=p, timeout=3)
        try:
            if n == "NORA":
                if r["state"].get("mode") != "manual":
                    return "she isn't in manual mode."
                if not pw:
                    return "her wheels are locked and I don't have the password (Passwords.NORA in my config.json)."
                get("/motorlockOff", pw=pw)
                go, back = ("/turnL", "/turnR") if sideways else ("/fw", "/bw")
                stop = lambda: get("/stop")
            elif n.startswith("MILA"):
                if r["state"].get("mode") == "obstacle":
                    return "she's in obstacle mode."
                if not pw:
                    return "I don't have her PIN (Passwords.MILA in my config.json)."
                get("/unlock", pw=pw)
                go, back = ("LEFT", "RIGHT") if sideways else ("FORWARD", "BACKWARD")
                go, back = (lambda c=go: get("/cmd", v=c)), (lambda c=back: get("/cmd", v=c))
                stop = lambda: get("/cmd", v="STOP")
            elif n.startswith("WHIP"):
                if r["state"].get("mode") != "web":
                    return "it isn't in web mode."
                if not pw:
                    return "I don't have its PIN (Passwords.WHIP in my config.json)."
                get("/unlock", pw=pw)
                go, back = ("left", "right") if sideways else ("fwd", "bwd")
                # a web command lasts 500 ms (WEB_CMD_TIMEOUT_MS): keep sending it for a whole step
                walk = lambda c: [(get("/web", cmd=c), time.sleep(0.3)) for _ in range(int(WHIP_STEP_S / 0.3))]
                go, back = (lambda c=go: walk(c)), (lambda c=back: walk(c))
                stop = lambda: get("/web", cmd="stop")
            else:
                return f"I don't know how to move {name} yet."
            move = lambda step: step() if callable(step) else get(step)
            move(go)
            time.sleep(0 if n.startswith("WHIP") else NUDGE_S)
            stop()
            time.sleep(0.6)                                 # let the sensors catch up
            self.robots[name]["polled"] = 0
            self.poll(name, url, "/sensors" if n == "NORA" else "/status")
            after = dict(self.robots[name]["sensors"])
            move(back)                                      # and back where she was
            time.sleep(0 if n.startswith("WHIP") else NUDGE_S)
            stop()
            if n == "NORA":
                get("/motorlockOn")
            return after
        except requests.RequestException as e:
            return f"{name} didn't answer ({type(e).__name__})."

    # ---- for her page and TINA
    def status(self):
        now = time.time()
        return {name: {"state": r["state"], "says": describe(r["state"]), "sensors": r["sensors"],
                       "age_s": int(now - r["at"]) if r["at"] else None,
                       "checking": name in self._busy}
                for name, r in self.robots.items()}
