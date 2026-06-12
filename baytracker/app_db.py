"""
app_db.py -- per-request SQLite connection helper for the Flask app.

SQLite connections must not be shared across threads, and waitress serves each
request (and each long-lived SSE stream) on its own thread. So we open one
connection per request, stash it on Flask's request-scoped ``g``, and close it
when the request ends. Background threads (the SSE heartbeat) open their own
short-lived connection instead -- see app.py.
"""

from flask import g

from . import db


def get_db():
    """Return this request's SQLite connection, opening it on first use."""
    if "db" not in g:
        g.db = db.connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()
