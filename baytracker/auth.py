"""
auth.py -- the lightweight PIN gate for /stats and /admin.

This is intentionally modest: the system lives on a private plant LAN with no
internet exposure (Appendix A4), so the goal is "keep cost data off the shop
floor and stop accidental edits", not defend against a determined attacker.

A correct PIN sets a flag in Flask's signed session cookie. Two independent
PINs exist (stats and admin); the admin PIN also unlocks stats (admin is the
superset). If a PIN has never been set, that area is OPEN and the page shows a
"set a PIN in Admin" banner -- otherwise a fresh install would lock you out of
the very page you need to set the PIN in.
"""

from functools import wraps

from flask import session, jsonify

from . import db


def pin_hash_key(area: str) -> str:
    return "pin_admin_hash" if area == "admin" else "pin_stats_hash"


def set_pin(conn, area: str, pin: str) -> None:
    """Store a hashed PIN. An empty pin clears it (area becomes open again)."""
    from werkzeug.security import generate_password_hash
    key = pin_hash_key(area)
    if pin:
        db.set_setting(conn, key, generate_password_hash(pin))
    else:
        db.set_setting(conn, key, None)


def _pin_is_set(conn, area: str) -> bool:
    return bool(db.get_setting(conn, pin_hash_key(area), None))


def verify_pin(conn, area: str, pin: str) -> bool:
    from werkzeug.security import check_password_hash
    stored = db.get_setting(conn, pin_hash_key(area), None)
    if not stored:
        return False
    return check_password_hash(stored, pin or "")


def is_unlocked(conn, area: str) -> bool:
    """True if the user may view ``area`` right now."""
    if not _pin_is_set(conn, area):
        return True                       # no PIN configured => open (with banner)
    if session.get(f"unlocked_{area}"):
        return True
    if area == "stats" and session.get("unlocked_admin"):
        return True                       # admin implies stats
    return False


def unlock(area: str) -> None:
    session[f"unlocked_{area}"] = True
    session.permanent = True


def require_area(area: str):
    """Decorator for JSON API endpoints: 403 if the area is locked."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from .app_db import get_db
            conn = get_db()
            if not is_unlocked(conn, area):
                return jsonify({"ok": False, "error": f"{area} is locked.",
                                "locked": True}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
