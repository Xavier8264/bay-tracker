"""
notify.py -- delay notifications: enqueue, send, retry.

THE reliability principle (mirrors the rest of this app's "DB is the single
source of truth" design):

    The web request that logs a delay NEVER sends anything over the network.

When a bay flips to DELAYED, app.py calls enqueue_notifications(), which only
writes 'pending' rows to notification_outbox -- instant, and fine even with the
wifi down. A background daemon thread (start_outbox_worker) does the actual
sending and retries failures with exponential backoff. This is the exact pattern
already proven by app._start_heartbeat: a forever-loop on its OWN db.connect()
that never lets an exception kill the thread.

Phase 1 ships email (Postmark). SMS (Twilio) is fully wired but stays dark until
the Twilio values are configured (see notify_config.sms_configured); an SMS row
enqueued before then simply waits as 'pending' rather than failing.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from . import db
from . import notify_config as cfg


# --- message construction --------------------------------------------------

# in_out_of_control is stored as 'in' | 'out' | NULL on the DELAY_START event.
_CONTROL_TEXT = {"in": "in control", "out": "out of control"}


def enqueue_notifications(conn: sqlite3.Connection, event: sqlite3.Row) -> None:
    """Write outbox rows for one DELAY_START event. Network-free; instant.

    `event` is the sqlite3.Row returned by actions.flag_delay / events.append --
    it already carries the reason label, division and control tag *snapshotted*
    onto it, so we never re-query delay_reasons (that would let a later rename
    rewrite this delay's history, which the snapshot design exists to prevent).
    """
    bay = conn.execute("SELECT name FROM bays WHERE id = ?;",
                       (event["bay_id"],)).fetchone()
    bay_name = bay["name"] if bay else f"Bay {event['bay_id']}"

    control = _CONTROL_TEXT.get(event["in_out_of_control"])   # may be None
    control_suffix = f" ({control})" if control else ""

    subject = f"DELAY: {bay_name} — {event['reason_label']}"
    email_body = (
        f"{bay_name} is DELAYED.\n"
        f"Reason: {event['reason_label']}{control_suffix}\n"
        f"Work order: {event['work_order'] or '—'}\n"
        f"Logged by: {event['initials']} at {event['ts']}\n"
        f"Note: {event['note'] or '—'}\n\n"
        f"{cfg.DASHBOARD_URL}"
    )
    sms_body = (
        f"{bay_name} DELAYED: {event['reason_label']}{control_suffix}, "
        f"by {event['initials']}. {cfg.DASHBOARD_URL}"
    )

    for r in conn.execute("SELECT * FROM recipients WHERE active = 1;").fetchall():
        if not _recipient_matches(r, event):
            continue
        if r["notify_email"] and r["email"]:
            conn.execute(
                """INSERT INTO notification_outbox
                   (delay_event_id, recipient_id, channel, destination, subject, body)
                   VALUES (?,?,?,?,?,?)""",
                (event["id"], r["id"], "email", r["email"], subject, email_body))
        if r["notify_sms"] and r["phone"]:
            conn.execute(
                """INSERT INTO notification_outbox
                   (delay_event_id, recipient_id, channel, destination, body)
                   VALUES (?,?,?,?,?)""",
                (event["id"], r["id"], "sms", r["phone"], sms_body))
    conn.commit()


def _recipient_matches(r: sqlite3.Row, event: sqlite3.Row) -> bool:
    """Apply a recipient's bay/control scope to one delay event."""
    if r["bay_scope"] != "all":
        bay_ids = {int(b) for b in r["bay_scope"].split(",") if b.strip()}
        if event["bay_id"] not in bay_ids:
            return False
    if r["control_scope"] == "out" and event["in_out_of_control"] != "out":
        return False
    return True


# --- send adapters (one per vendor; each RAISES on any failure) ------------

def send_email_postmark(to: str, subject: str, body: str) -> None:
    """Send one email via Postmark. Raises on misconfig or any non-2xx."""
    import requests   # imported lazily so the app boots even if not installed yet
    if not cfg.email_configured():
        raise RuntimeError("Postmark is not configured (POSTMARK_TOKEN / POSTMARK_FROM).")
    resp = requests.post(
        "https://api.postmarkapp.com/email",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": cfg.POSTMARK_TOKEN,
        },
        json={
            "From": cfg.POSTMARK_FROM,
            "To": to,
            "Subject": subject,
            "TextBody": body,
            "MessageStream": "outbound",
        },
        timeout=10,
    )
    resp.raise_for_status()


def send_sms_twilio(to: str, body: str) -> None:
    """Send one SMS via Twilio. Raises on misconfig or any non-2xx."""
    import requests
    if not cfg.sms_configured():
        raise RuntimeError("Twilio is not configured (TWILIO_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM).")
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{cfg.TWILIO_SID}/Messages.json",
        auth=(cfg.TWILIO_SID, cfg.TWILIO_AUTH_TOKEN),
        data={"From": cfg.TWILIO_FROM, "To": to, "Body": body},
        timeout=10,
    )
    resp.raise_for_status()


# --- the background worker -------------------------------------------------

BACKOFF_SECONDS = [30, 120, 300, 900, 3600]   # waits between successive attempts
MAX_ATTEMPTS = len(BACKOFF_SECONDS)


def process_outbox_once(conn: sqlite3.Connection) -> None:
    """Send one batch of due 'pending' rows. Called repeatedly by the worker."""
    rows = conn.execute("""
        SELECT * FROM notification_outbox
        WHERE status = 'pending' AND next_attempt_at <= datetime('now','localtime')
        ORDER BY id LIMIT 20
    """).fetchall()

    for row in rows:
        # If this channel isn't configured yet, leave the row PENDING rather than
        # burning its attempts on a guaranteed failure -- it flows the moment the
        # credentials are added (e.g. SMS lights up when Twilio is set in Phase 2).
        if row["channel"] == "email" and not cfg.email_configured():
            continue
        if row["channel"] == "sms" and not cfg.sms_configured():
            continue

        # Claim the row so a second pass (or a future second worker) can't grab it.
        claimed = conn.execute(
            "UPDATE notification_outbox SET status='sending' WHERE id=? AND status='pending'",
            (row["id"],)).rowcount
        conn.commit()
        if not claimed:
            continue

        try:
            if row["channel"] == "email":
                send_email_postmark(row["destination"], row["subject"], row["body"])
            else:
                send_sms_twilio(row["destination"], row["body"])
            conn.execute(
                "UPDATE notification_outbox SET status='sent', "
                "sent_at=datetime('now','localtime') WHERE id=?", (row["id"],))
        except Exception as e:
            attempts = row["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE notification_outbox SET status='failed', attempts=?, "
                    "last_error=? WHERE id=?", (attempts, str(e)[:500], row["id"]))
            else:
                wait = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
                conn.execute(
                    "UPDATE notification_outbox SET status='pending', attempts=?, "
                    "last_error=?, next_attempt_at=datetime('now','localtime', ?) WHERE id=?",
                    (attempts, str(e)[:500], f"+{wait} seconds", row["id"]))
        conn.commit()


_worker_started = False
_worker_lock = threading.Lock()


def start_outbox_worker(interval: int = 20) -> None:
    """Start the single outbox daemon thread (idempotent: starts at most once).

    Mirrors app._start_heartbeat: the thread opens its OWN short-lived SQLite
    connection each pass (connections can't cross threads), works, closes, and
    never lets an exception kill the loop.
    """
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    def loop():
        while True:
            try:
                conn = db.connect()
                try:
                    process_outbox_once(conn)
                finally:
                    conn.close()
            except Exception:
                # Never let a transient error kill the worker thread.
                pass
            time.sleep(interval)

    threading.Thread(target=loop, name="bt-outbox", daemon=True).start()


# --- small read helper for the admin failure view --------------------------

def recent_failures(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Permanently-failed sends, newest first, for the admin banner (section 8)."""
    rows = conn.execute(
        "SELECT * FROM notification_outbox WHERE status = 'failed' "
        "ORDER BY created_at DESC LIMIT ?;", (limit,)).fetchall()
    return [dict(r) for r in rows]
