"""ir_reader.py - reading a robot remote's button codes through an Arduino on this PC.

NINA can flash scripts/ir_reader onto the board, then shows each button's code on her page. The port
opens with DTR high and RTS low: with DTR low, a UNO's USB chip passes nothing back.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

import serial

from board_id import identify_ports

BAUD = 115200
SKETCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "ir_reader")
BOARD = "arduino:avr:uno"
PINS = ("6", "A3", "11")      # A3: MILA and NIKA; 11: IDA
CODE = re.compile(r"(?:Command HEX|Raw Data HEX): 0x([0-9A-Fa-f]+)")
IAM = re.compile(r"I am (\w+)", re.IGNORECASE)
WHO_EVERY_S = 1.0
WHO_TRIES = 4
BOARD_BAUDS = [115200, 9600]     # ARM and this reader, DREAM's sensors
FLASH_TIMEOUT_S = 180


def code_of(line):
    """A remote button's code in a line from the board, as 0x19, or None."""
    m = CODE.search(line)
    return "0x" + m.group(1).upper() if m else None


def arduino_cli():
    found = shutil.which("arduino-cli")
    local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "arduino-cli", "arduino-cli.exe")
    return found or (local if os.path.exists(local) else None)


class IrReader:
    def __init__(self):
        self.lines = deque(maxlen=200)
        self.codes = deque(maxlen=40)
        self.port, self.error, self.flashing = None, "", False
        self.who, self.boards, self.asking = None, {}, False
        self._stop = threading.Event()
        self._thread = None
        self._seen = 0

    def state(self):
        return {"port": self.port, "error": self.error, "flashing": self.flashing, "bytes": self._seen,
                "who": self.who, "boards": self.boards, "asking": self.asking,
                "lines": list(self.lines)[-30:], "codes": list(self.codes)}

    def start(self, port):
        self.stop()
        self.error, self._seen, self.who = "", 0, None
        self.lines.clear()
        self.codes.clear()
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, BAUD, 0.3
        s.dtr, s.rts = True, False
        try:
            s.open()
        except serial.SerialException as e:
            self.error = f"{port} won't open: is another program using it? ({e})"
            return False
        self._stop.clear()
        self.port = port
        self._thread = threading.Thread(target=self._read, args=(s,), daemon=True, name="ir-reader")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self._thread, self.port = None, None

    def _read(self, s):
        tries, next_who = 0, time.time() + 0.3
        try:
            while not self._stop.is_set():
                if not self.who and tries < WHO_TRIES and time.time() >= next_who:   # the handshake
                    s.write(b"WHO\n")
                    tries, next_who = tries + 1, time.time() + WHO_EVERY_S
                raw = s.readline()
                if not raw:
                    continue
                self._seen += len(raw)
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self.lines.append(line)
                    who = IAM.search(line)
                    if who:
                        self.who = who.group(1)
                    code = code_of(line)
                    if code:
                        self.codes.append({"t": time.time(), "code": code})
        except serial.SerialException as e:
            self.error = f"Lost {self.port}: {e}"
        finally:
            s.close()

    def ask_boards(self):
        """The handshake with every free board: ARM, DREAM's sensors, this reader. Opening a port
        resets its Arduino, so this only ever runs when someone asks. Blocks."""
        self.stop()
        self.asking = True
        try:
            self.boards = identify_ports(BOARD_BAUDS)
        finally:
            self.asking = False

    def flash(self, port, pin):
        """Builds and uploads the reader. Blocks, so call it on its own thread."""
        cli = arduino_cli()
        if not cli:
            self.error = "arduino-cli isn't installed"
            return False
        if pin not in PINS:
            self.error = f"Pin {pin} isn't one of {', '.join(PINS)}"
            return False
        self.stop()
        self.flashing, self.error = True, ""
        env = dict(os.environ)
        user = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Arduino15", "user")
        if os.path.isdir(user):
            env.setdefault("ARDUINO_DIRECTORIES_USER", user)
        cmd = [cli, "compile", "--fqbn", BOARD, "--upload", "--port", port,
               "--build-property", f"compiler.cpp.extra_flags=-DIR_PIN={pin}", SKETCH]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=FLASH_TIMEOUT_S, env=env)
            if r.returncode:
                self.error = "Flash failed: " + (r.stderr or r.stdout).strip()[-200:]
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            self.error = "Flash took too long"
            return False
        finally:
            self.flashing = False
