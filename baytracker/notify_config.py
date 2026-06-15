"""
notify_config.py -- secrets and settings for delay notifications.

This project's iron rule (config.py / Appendix B2) is that secrets and data live
OUTSIDE the repository. The Flask secret key already sits in the data folder and
is never committed; notification credentials follow the same model rather than a
repo-root .env:

  1. PRIMARY -- environment variables, set in the NSSM service definition right
     next to BAYTRACKER_DATA (see setup.ps1). Nothing secret touches git or the
     backed-up database.
  2. DEV CONVENIENCE -- an optional `notify.env` file in the data folder
     (DATA_DIR/notify.env, outside the repo), loaded with python-dotenv if that
     package and the file are both present.

Everything is read with os.environ.get (never os.environ[...]), so a PC where
notifications are not configured yet still boots cleanly -- the admin "failure
view" plus the email_configured()/sms_configured() flags make the not-set-up
state visible instead of crashing the app at import time.
"""

import os

from . import config

# Load DATA_DIR/notify.env if python-dotenv is installed and the file exists.
# Both are optional: missing dotenv or a missing file just means we fall back to
# whatever is already in the process environment (the production path).
try:
    from dotenv import load_dotenv

    _envfile = config.DATA_DIR / "notify.env"
    if _envfile.exists():
        load_dotenv(_envfile)
except ImportError:
    pass


# --- Email (Postmark) -- Phase 1 ------------------------------------------
POSTMARK_TOKEN = os.environ.get("POSTMARK_TOKEN")
POSTMARK_FROM = os.environ.get("POSTMARK_FROM")     # a verified sender, e.g. baytracker@yourco.com

# Link included in every alert. Defaults to localhost so dev works with no
# config; set DASHBOARD_URL to the floor PC's LAN address in production.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:5000/dashboard")

# --- SMS (Twilio) -- Phase 2 (left blank until 10DLC registration is real) -
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM")         # +1XXXXXXXXXX


def email_configured() -> bool:
    """True only when Postmark can actually send (token + verified sender set)."""
    return bool(POSTMARK_TOKEN and POSTMARK_FROM)


def sms_configured() -> bool:
    """True only when Twilio can actually send (SID + token + from-number set)."""
    return bool(TWILIO_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM)
