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
        bits.append(f"{st['mode']} mode")
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
            else:
                return f"I don't know how to move {name} yet."
            move = lambda step: step() if callable(step) else get(step)
            move(go)
            time.sleep(NUDGE_S)
            stop()
            time.sleep(0.6)                                 # let the sensors catch up
            self.robots[name]["polled"] = 0
            self.poll(name, url, "/sensors" if n == "NORA" else "/status")
            after = dict(self.robots[name]["sensors"])
            move(back)                                      # and back where she was
            time.sleep(NUDGE_S)
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
