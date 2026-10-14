"""checkup.py: how NINA reads each robot's state, and her WHIP checkup against a fake WHIP.
Offline: the fake WHIP is a local Flask app; nothing is spoken (note() is recorded).
Run from the NINA folder:  venv\\Scripts\\python -m pytest tests"""
import os
import sys
import threading

import pytest
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import checkup  # noqa: E402
from checkup import Checkups, describe, read_state  # noqa: E402


# ---------------------------------------------------------------- reading each robot
def test_nora():
    st, s = read_state("NORA", {"F": 34.5, "L": 120, "B": -1, "R": 80, "mode": 0, "ms": 1, "mt": 101, "lock": 1})
    assert st == {"moving": False, "turning": None, "wheels_locked": True, "music": "playing track 101", "mode": "manual"}
    assert s == {"front": 34.5, "left": 120.0, "back": -1.0, "right": 80.0}
    assert describe(st) == "wheels locked, standing still, manual mode, music playing track 101"
    st, _ = read_state("NORA", {"mode": 1, "lock": 0, "ms": 0})
    assert st["moving"] is True and describe(st) == "moving, auto mode"


def test_mila():
    st, s = read_state("MILA", {"ml": 1, "mr": -1, "dist": 23.0, "mode": "wasd"})
    assert st["moving"] is True and st["turning"] is True and s == {"front": 23.0}
    assert describe(st) == "turning, wasd mode"
    st, _ = read_state("MILA", {"ml": 0, "mr": 0, "dist": 999})
    assert st["moving"] is False


def test_whip():
    st, s = read_state("WHIP", {"move": 3, "controlMode": 2, "distance": 55})
    assert st["moving"] is True and st["turning"] is True and st["mode"] == "web" and s == {"front": 55.0}
    st, _ = read_state("WHIP", {"move": 0, "controlMode": 0})
    assert describe(st) == "standing still, obstacle mode"


def test_kida00_old_status():
    st, s = read_state("KIDA00", {"direction": "LEFT", "mode": "USER", "music_playing": True})
    assert st == {"moving": True, "turning": True, "wheels_locked": None, "music": "playing", "mode": "USER"}
    assert s == {}


def test_kida00_new_status():
    st, _ = read_state("KIDA00", {"direction": "FORWARD", "moving": False, "motor_lock": True,
                                  "music": "stopped", "music_playing": True, "mode": "USER", "uptime_s": 9})
    assert st["moving"] is False and st["wheels_locked"] is True and st["music"] == "stopped"
    assert describe(st) == "wheels locked, standing still, USER mode"


def test_kida01():
    st, s = read_state("KIDA01", {"mode": "MANUAL", "music_on": False, "motor_lock": True,
                                  "sensors": {"US0": 41.0, "US1": 77.5, "IMU": "ok"}})
    assert st["moving"] is None and st["wheels_locked"] is True and st["music"] == "stopped"
    assert s == {"us0": 41.0, "us1": 77.5}


def test_arm_and_anyone_else():
    st, s = read_state("ARM", {"connected": False, "motors": {}})
    assert st["moving"] is False and describe(st) == "standing still, board unplugged" and s == {}
    st, s = read_state("NEWBOT", {"moving": True, "front_cm": 12, "battery": 80, "flag": True})
    assert st["moving"] is True and s == {"front_cm": 12.0}
    assert describe({}) == "unknown" and describe({"moving": None}) == "it doesn't say"


# ---------------------------------------------------------------- WHIP's checkup, against a fake WHIP
@pytest.fixture
def whip(monkeypatch):
    seen = {"hits": [], "distance": 55.0, "broken": False}
    app = Flask("fake_whip")

    @app.route("/<path:path>")
    def any_path(path):
        seen["hits"].append((path, dict(request.args)))
        if path == "web" and not seen["broken"]:
            seen["distance"] += {"fwd": -2.0, "bwd": 2.0}.get(request.args.get("cmd"), 0.0)
        if path == "status":
            return jsonify({"move": 0, "controlMode": 2, "distance": seen["distance"]})
        return "OK"

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen["url"] = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(checkup.time, "sleep", lambda s: None)     # no real walking time
    yield seen
    srv.shutdown()


def nina(whip, pin):
    notes = []
    c = Checkups({"StuckMinutes": 10, "Passwords": {"WHIP": pin} if pin else {}},
                 lambda level, text, why="", **kw: notes.append((level, text, why)))
    st, s = read_state("WHIP", {"move": 0, "controlMode": 2, "distance": whip["distance"]})
    c.robots["WHIP"] = {"url": whip["url"], "state": st, "sensors": s, "at": 0, "polled": 0}
    return c, notes


def test_whip_nudge_with_pin(whip):
    c, _ = nina(whip, "1234")
    after = c.nudge("WHIP", "front")
    assert after == {"front": 45.0}                          # five 'fwd' sends while walking a step
    cmds = [a.get("cmd") for p, a in whip["hits"] if p == "web"]
    assert ("unlock", {"pw": "1234"}) in whip["hits"]
    assert cmds.count("fwd") == 5 and cmds.count("bwd") == 5 and cmds.count("stop") == 2
    assert whip["distance"] == 55.0                         # walked back where it was


def test_whip_nudge_without_pin(whip):
    c, _ = nina(whip, "")
    assert "PIN" in c.nudge("WHIP", "front") and whip["hits"] == []


def test_whip_not_in_web_mode(whip):
    c, _ = nina(whip, "1234")
    c.robots["WHIP"]["state"]["mode"] = "obstacle"
    assert c.nudge("WHIP", "front") == "it isn't in web mode." and whip["hits"] == []


def test_whip_checkup_healthy(whip):
    c, notes = nina(whip, "1234")
    c.checkup("WHIP", "front", 55.0, 900)
    assert any(level == "info" and "is healthy: 55 -> 45 cm" in text for level, text, _ in notes)


def test_whip_checkup_broken_sensor(whip):
    whip["broken"] = True                                   # it walks, the reading stays the same
    c, notes = nina(whip, "1234")
    c.checkup("WHIP", "front", 55.0, 900)
    assert any(level == "problem" and "didn't change" in text for level, text, _ in notes)


def test_whip_checkup_no_pin_dead_sensor(whip):
    c, notes = nina(whip, "")
    c.checkup("WHIP", "front", 0.0, 900)
    assert any(level == "warn" and "couldn't test it" in text and "PIN" in text for level, text, _ in notes)
