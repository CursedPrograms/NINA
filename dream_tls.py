"""
dream_tls.py - how NINA trusts DREAM's HTTPS.

DREAM's dashboard (port 5009) is HTTPS with her own certificate (DREAM/certs/cert.pem,
made by her app.py for this PC's addresses), so RIFT hands out https://<ip>:5009/ for
her. A plain requests.get() refuses it: it isn't signed by a public authority. NINA
checks DREAM against that certificate instead, so the link is still verified.

    requests.get(url, verify=tls(url))

Where it is: "DreamCert" in config.json, else ../DREAM/certs/cert.pem (the repos side
by side). DREAM on another PC: copy her cert.pem here and point DreamCert at it.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _cert_path():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            p = json.load(f)["Config"]["NINA"].get("DreamCert", "")
    except (OSError, ValueError, KeyError):
        p = ""
    p = p or os.path.join("..", "DREAM", "certs", "cert.pem")
    p = p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))
    return p if os.path.exists(p) else None


def tls(url):
    """requests' verify= for this URL: DREAM's certificate for https, the normal check otherwise."""
    if str(url).startswith("https://"):
        return _cert_path() or True
    return True
