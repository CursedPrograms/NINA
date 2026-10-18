"""nina.py's NORA check: her page/API (:5002) silent while her fleet registry (:5000) answers is
"busy", not gone; one miss isn't gone either; two are. NORA's answers are faked (timed_get)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nina  # noqa: E402


class Answer:
    def __init__(self, body=None, ok=True, text=""):
        self.ok, self.status_code, self._body, self.text = ok, 200 if ok else 503, body or {}, text

    def json(self):
        return self._body


@pytest.fixture
def nora(monkeypatch):
    state = {"api": True, "registry": True, "now": 600000, "ping": "NORA alive", "asked": []}
    notes = []

    def timed_get(url, timeout=3):
        state["asked"].append(url)
        if ":5002/talk" in url:
            return (Answer({"now": state["now"]}) if state["api"] else None), 40
        if ":5000/ping" in url:
            return (Answer(text=state["ping"]) if state["registry"] else None), 20
        return None, 0                                   # /sensors, her page: not part of this test
    monkeypatch.setattr(nina, "timed_get", timed_get)
    monkeypatch.setattr(nina, "note", lambda level, text, why="", **kw: notes.append((level, text)))
    w = nina.Watch()
    w._last_page_check = 1e18                            # skip the full-page timing
    return w, state, notes


def test_online(nora):
    w, state, notes = nora
    w.nora_check()
    assert w.nora["up"] is True and w.nora["busy"] is False and notes[-1][1].startswith("NORA is online")


def test_busy_page_is_not_gone(nora):
    w, state, notes = nora
    w.nora_check()
    state["api"] = False                                 # someone has her page open
    w.nora_check()
    w.nora_check()
    assert w.nora["up"] is True and w.nora["busy"] is True
    assert [n for n in notes if "busy" in n[1]] == [("info", "NORA's web page is busy.")]   # said once
    assert not any(level == "problem" for level, _ in notes)


def test_one_miss_then_gone(nora):
    w, state, notes = nora
    w.nora_check()
    state["api"] = state["registry"] = False
    w.nora_check()
    assert w.nora["up"] is True and not any(level == "problem" for level, _ in notes)
    w.nora_check()
    assert w.nora["up"] is False and ("problem", "NORA isn't answering.") in notes
    state["api"] = state["registry"] = True
    w.nora_check()
    assert w.nora["up"] is True and ("info", "NORA is back.") in notes


def test_rift_at_her_old_address_isnt_her(nora):
    w, state, notes = nora
    w.nora_check()
    state.update(api=False, ping="RIFT alive")           # the RIFT network: 192.168.4.1 is RIFT itself
    w.nora_check()
    w.nora_check()
    assert w.nora["up"] is False and w.nora["busy"] is False


def test_her_address_from_rifts_roster(nora):
    w, state, notes = nora
    w.nora_ip = "192.168.4.23"                           # what Watch.fleet() found in RIFT's /fleet
    w.nora_check()
    assert w.nora["up"] is True and all("192.168.4.23" in u for u in state["asked"])
