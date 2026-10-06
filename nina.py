"""
NINA - Network Inspector & Notification Assistant: the fleet's caretaker.

She watches every robot so problems get noticed and explained instead of
puzzled over:

  fleet      every robot RIFT knows (GET /fleet): answering or not, and how fast.
             Gone twice in a row = offline; back = online again
  NORA       her web server's speed (a slow page blocks her whole loop), her
             uptime (/talk "now" falling back = she rebooted), and her drive
             mode with who changed it (/sensors mode + msrc)
  this PC    which WiFi it's on (NORA unreachable because the PC is on another
             network?), and the serial ports coming and going
  ports      listed only, never opened: opening a port resets an Arduino and
             fights the program that owns it (that's how the ARM lost COM11)

Everything goes to her log (web page on :5012, GET /events), the important
things are said out loud and written to RIFT's mission log, and she registers
with RIFT like any robot, so she's on its dashboard and can join the Brainfuck
chatter (/chirp).

    python nina.py            (run.bat sets up the venv first)
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

import story

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "NINA"


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)["Config"]["NINA"]


CFG = load_config()
PORT = CFG["Port"]
RIFT = f"http://{CFG['RiftHost']}:{CFG['RiftPort']}"
NORA = CFG["NoraHost"]

# ---------------------------------------------------------------- her log
events = deque(maxlen=300)
_lock = threading.Lock()
_last_said = {}


def note(level, text, why="", speak=False, mission=False, key=None):
    """level: info / warn / problem. `key` stops the same thing being said again within a minute."""
    e = {"t": time.time(), "level": level, "text": text, "why": why}
    with _lock:
        events.append(e)
    print(f"[{time.strftime('%H:%M:%S')}] {level.upper():7} {text}" + (f"  ({why})" if why else ""), flush=True)
    k = key or text
    if speak and CFG.get("Speak", True) and time.time() - _last_said.get(k, 0) > 60:
        _last_said[k] = time.time()
        say(text)
    if mission and CFG.get("MissionLog", True):
        threading.Thread(target=_post, args=(f"{RIFT}/mission", {"who": NAME, "text": f"{text} {why}".strip()}), daemon=True).start()


def _post(url, data):
    try:
        requests.post(url, json=data, timeout=2)
    except requests.RequestException:
        pass


# ---------------------------------------------------------------- her voice
_speech = deque()
_speech_ready = threading.Event()


def say(text):
    _speech.append(text)
    _speech_ready.set()


def _speaker():
    """Windows text-to-speech, one line at a time (the same voice flash.bat uses)."""
    while True:
        _speech_ready.wait()
        while _speech:
            text = _speech.popleft().replace("'", "''")
            if os.name == "nt":
                voice = CFG.get("Voice", "")
                pick = f"try {{ $s.SelectVoice('{voice}') }} catch {{}};" if voice else ""
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; {pick} $s.Speak('{text}')"],
                               capture_output=True)
        _speech_ready.clear()


# ---------------------------------------------------------------- the checks
def timed_get(url, timeout=3):
    """(response or None, milliseconds)."""
    t = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        return r, int((time.time() - t) * 1000)
    except requests.RequestException:
        return None, int((time.time() - t) * 1000)


class Watch:
    def __init__(self):
        self.robots = {}            # name -> {"url", "up", "ms", "misses", "seen"}
        self.nora = {"up": None, "ms": None, "uptime_s": None, "mode": None, "msrc": None, "page_ms": None}
        self.ports = None
        self.wifi = None
        self.rift_up = None
        self._last_page_check = 0

    # -- the fleet, through RIFT
    def fleet(self):
        r, _ = timed_get(f"{RIFT}/fleet", 12)   # RIFT probes NORA itself first: up to 8 s when she's away
        up = r is not None and r.ok
        if up != self.rift_up:
            if up:
                note("info", "RIFT is up.")
            else:
                note("problem", "I can't reach RIFT." if self.rift_up else "RIFT isn't running.",
                     "Start RIFT's run.bat - without it I can't see the rest of the fleet.", speak=True)
        self.rift_up = up
        if not up:
            return
        try:
            robots = r.json().get("robots", [])
        except ValueError:
            return
        for rb in robots:
            name = rb.get("name")
            if not name or name == NAME or name == "NORA":   # NORA gets her own, closer look
                continue
            url = rb.get("url") or (f"http://{rb.get('ip')}:{_port_of(rb)}/" if _port_of(rb) else None)
            st = self.robots.setdefault(name, {"url": url, "up": None, "ms": None, "misses": 0, "seen": time.time()})
            st["url"], st["seen"] = url or st["url"], time.time()
        for name, st in list(self.robots.items()):
            if time.time() - st["seen"] > 120:     # RIFT dropped it: it stopped heartbeating
                if st["up"]:
                    note("warn", f"{name} left the fleet.", "It stopped checking in with RIFT.", speak=True, mission=True)
                del self.robots[name]
                continue
            self._probe(name, st)

    def _probe(self, name, st):
        if not st["url"]:
            return
        r, ms = timed_get(st["url"].rstrip("/") + "/ping", 3)
        if r is None or r.status_code >= 500:
            r, ms = timed_get(st["url"], 3)            # not every robot has /ping
        ok = r is not None and r.status_code < 500
        st["ms"] = ms if ok else None
        if ok:
            if st["up"] is False:
                note("info", f"{name} is back.", speak=True, mission=True)
            elif st["up"] is None:
                note("info", f"{name} is online ({ms} ms).")
            st["up"], st["misses"] = True, 0
            if ms > CFG["SlowMs"]:
                note("warn", f"{name} took {ms / 1000:.1f} seconds to answer.",
                     "Something is blocking its main loop, or the network is struggling.", key=f"slow-{name}")
        else:
            st["misses"] += 1
            if st["misses"] == 2 and st["up"] is not False:
                st["up"] = False
                note("problem", f"{name} isn't answering.", "Powered off, crashed, or on another network.",
                     speak=True, mission=True)

    # -- NORA, closely
    def nora_check(self):
        r, ms = timed_get(f"http://{NORA}:5002/talk", 3)
        if r is None or not r.ok:
            if self.nora["up"] is not False:
                why = "Her WiFi is gone - she may be off or rebooting."
                if self.wifi and self.wifi != CFG["NoraSsid"]:
                    why = f"This PC is on {self.wifi}, not her network."
                note("problem", "NORA isn't answering.", why, speak=True, mission=self.nora["up"] is True)
            self.nora.update(up=False, ms=None)
            return
        if self.nora["up"] is False:
            note("info", "NORA is back.", speak=True, mission=True)
        elif self.nora["up"] is None:
            note("info", f"NORA is online ({ms} ms).")
        self.nora.update(up=True, ms=ms)
        try:
            uptime = r.json().get("now", 0) / 1000
        except ValueError:
            uptime = None
        prev = self.nora["uptime_s"]
        if uptime is not None and prev is not None and uptime + 5 < prev:
            note("problem", f"NORA rebooted (she'd been up {_dur(prev)}).",
                 "A power dip? Check what was just switched on: UV, motors.", speak=True, mission=True)
        self.nora["uptime_s"] = uptime

        s, _ = timed_get(f"http://{NORA}:5002/sensors", 3)
        if s is not None and s.ok:
            try:
                d = s.json()
            except ValueError:
                d = {}
            mode, src = d.get("mode"), d.get("msrc", "?")
            if self.nora["mode"] is not None and mode != self.nora["mode"]:
                names = ["Manual", "Auto", "Line", "Remote"]
                label = names[mode] if isinstance(mode, int) and 0 <= mode < 4 else mode
                lvl, why = ("info", f"Changed by {src}.") if src in ("web", "bt", "ir") else ("warn", f"Nobody I know changed it (source: {src}).")
                note(lvl, f"NORA switched to {label} mode.", why, speak=lvl == "warn")
            self.nora.update(mode=mode, msrc=src)
            blind = [side for side, k in (("front", "F"), ("left", "L"), ("back", "B"), ("right", "R")) if d.get(k) == -1]
            self.nora["blind"] = blind[0] if len(blind) == 1 else None   # one sensor always silent = probably broken

        if time.time() - self._last_page_check > 60:   # her full page: the slow one that used to block her
            self._last_page_check = time.time()
            p, pms = timed_get(f"http://{NORA}:5002/", 15)
            self.nora["page_ms"] = pms if p is not None else None
            if p is not None and pms > CFG["SlowMs"]:
                note("warn", f"NORA's web page took {pms / 1000:.1f} seconds.",
                     "Her loop is frozen while it sends - RIFT will think she's offline.", speak=True, key="nora-page")

    # -- this PC
    def pc(self):
        ssid = None
        if os.name == "nt":
            try:
                out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
                ssid = m.group(1).strip() if m else None
            except (OSError, subprocess.SubprocessError):
                pass
        if ssid != self.wifi and self.wifi is not None:
            note("info", f"This PC moved to {ssid or 'no'} WiFi.", "" if ssid == CFG["NoraSsid"] else
                 "NORA's network has no internet, so Windows may hop back to a known one.")
        self.wifi = ssid

        try:
            from serial.tools import list_ports
            ports = {p.device: p.description for p in list_ports.comports()}
        except Exception:
            return
        if self.ports is not None:
            for dev in ports.keys() - self.ports.keys():
                note("info", f"{dev} plugged in: {ports[dev]}.", _guess_board(ports[dev]))
            for dev in self.ports.keys() - ports.keys():
                note("info", f"{dev} unplugged ({self.ports[dev]}).")
        self.ports = ports

    def snapshot(self):
        return {"robots": self.robots, "nora": self.nora, "rift_up": self.rift_up, "wifi": self.wifi,
                "ports": self.ports or {}, "events": list(events)[-80:][::-1]}


def troubles_for(name):
    """What NINA has noticed about a robot, as phrases a children's story can use."""
    name = name.upper()
    out = []
    recent = [e for e in list(events)[-150:] if name in e["text"].upper() and time.time() - e["t"] < 6 * 3600]
    if name == "NORA":
        n = watch.nora
        if n["up"] is False:
            out.append("fell asleep and would not wake up")
        if any("rebooted" in e["text"] for e in recent):
            out.append("suddenly fell down and had to start all over again")
        if n.get("page_ms") and n["page_ms"] > CFG["SlowMs"]:
            out.append("became very, very slow")
        if any("Nobody I know changed it" in e.get("why", "") for e in recent):
            out.append("started doing things all by herself")
        if n.get("blind"):
            out.append(f"could not see on her {n['blind']} side")
    else:
        r = watch.robots.get(name) or next((v for k, v in watch.robots.items() if k.upper() == name), None)
        if r is None or r.get("up") is False:
            out.append("went away and did not come back")
        elif r.get("ms") and r["ms"] > CFG["SlowMs"]:
            out.append("became very, very slow")
    return out


def _port_of(rb):
    for c in rb.get("capabilities", []):
        for pre in ("web:", "talk:"):
            if str(c).startswith(pre) and str(c)[len(pre):].isdigit():
                return str(c)[len(pre):]
    return None


def _guess_board(desc):
    d = desc.lower()
    if "cp210" in d or "silicon labs" in d:
        return "Probably NORA's or WHIP's ESP32."
    if "ftdi" in d or "usb serial port" in d:
        return "Probably the ARM's Arduino."
    if "ch340" in d or "ch341" in d:
        return "A CH340 board: an ESP32 or an Arduino clone."
    if "arduino" in d:
        return "An Arduino."
    return ""


def _dur(s):
    s = int(s)
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


watch = Watch()


def watcher():
    note("info", "NINA is watching the fleet.", speak=True)
    while True:
        for check in (watch.pc, watch.nora_check, watch.fleet):
            try:
                check()
            except Exception as e:   # a bug in one check mustn't stop the others
                note("warn", f"My {check.__name__} check failed: {e}")
        time.sleep(CFG["CheckSeconds"])


def heartbeat():
    """Register with RIFT like any robot: on its dashboard, and in the conversations (talk:)."""
    while True:
        try:
            requests.post(f"{RIFT}/register", data={"name": NAME, "type": "caretaker",
                                                     "capabilities": f"fleet_health,web:{PORT},talk:{PORT}"}, timeout=2)
        except requests.RequestException:
            pass
        time.sleep(10)


# ---------------------------------------------------------------- Brainfuck chirps
def chirp(u):
    """Say utterance u the way the robots do: its Brainfuck program, one beep per
    symbol (RIFT's Fleet/brainfuck_talk.py has the phrasebook), in NINA's own pitch."""
    sys.path.insert(0, os.path.join(HERE, "..", "RIFT", "Fleet"))
    try:
        import brainfuck_talk as bf
    except ImportError:
        return False
    code = next((c for i, _, _, _, c in bf.utterances() if i == u), None)
    if code is None:
        return False
    if os.name == "nt":
        import winsound
        pitch = CFG.get("VoicePitch", 0.85)
        for ch in code:
            winsound.Beep(max(37, int(bf.TONES.get(ch, 1000) * pitch)), bf.LENGTHS.get(ch, bf.DEFAULT_MS))
    return True


# ---------------------------------------------------------------- web
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", name=NAME, port=PORT)


@app.route("/status")
def status():
    return jsonify(watch.snapshot())


@app.route("/events")
def events_json():
    return jsonify(list(events)[::-1])


@app.route("/ping")
def ping():
    return f"{NAME} alive", 200, {"Content-Type": "text/plain"}


@app.route("/colour_scheme.xml")
def colours():
    with open(os.path.join(HERE, "colour_scheme.xml"), encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "application/xml"}


@app.route("/avatar.jpg")
def avatar():
    return send_from_directory(os.path.join(HERE, "images"), "nina_avatar.jpg", max_age=86400)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(HERE, "site"), "favicon.ico", max_age=86400)


@app.route("/story")
def story_route():
    """GET /story?robot=NORA - how that robot is doing, told as a bedtime story (text only)."""
    robot = (request.args.get("robot") or "NORA").strip()[:20]
    troubles = troubles_for(robot)
    try:
        text = story.tell_story(robot, troubles)
    except Exception as e:
        return jsonify({"robot": robot, "troubles": troubles, "error": f"the storyteller couldn't start: {e}"}), 503
    return jsonify({"robot": robot, "troubles": troubles, "story": text})


@app.route("/chirp")
def chirp_route():
    try:
        u = int(request.args.get("u", ""))
    except ValueError:
        u = -1
    if not 0 <= u <= 13:
        return jsonify({"ok": False, "error": "use /chirp?u=0-13"}), 400
    threading.Thread(target=chirp, args=(u,), daemon=True).start()
    return jsonify({"ok": True})


def main():
    for target in (_speaker, watcher, heartbeat):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
    print(f"NINA on http://127.0.0.1:{PORT}/")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
