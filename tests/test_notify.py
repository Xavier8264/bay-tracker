"""
test_notify.py -- regression tests for delay notifications (notify.py).

The notification path is reliability-critical: logging a delay must enqueue the
right alerts, the worker must retry rather than lose them, and an unconfigured
channel must never burn its attempts on a guaranteed failure. Run it after
touching notify.py / notify_config.py / the recipient routes:

    python tests/test_notify.py

It uses a throwaway temp database and asserts:
  * scope routing -- bay_scope and control_scope filter recipients correctly,
  * retired recipients and channel-without-address are skipped,
  * the message is built from the snapshotted DELAY_START event (no re-query),
  * the worker leaves rows PENDING when the channel isn't configured,
  * a configured send flips the row to 'sent',
  * a failing send retries with backoff, then lands in 'failed' after the cap,
  * destination is snapshotted (editing the recipient never rewrites history).

No pytest required (keeps the dependency list minimal). Exits non-zero on any
failure so it can gate an update.
"""

import os
import shutil
import sys
import tempfile
import uuid as _uuid

# Make the repo importable and point the app at a temp data dir BEFORE import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="bt_ntfy_")
os.environ["BAYTRACKER_DATA"] = _TMP

from baytracker import db, events, notify                       # noqa: E402
from baytracker import notify_config as cfg                     # noqa: E402

_FAILS = []


def check(name, got, want):
    ok = (got == want)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, want {want!r}")
    if not ok:
        _FAILS.append(name)


def fresh_conn():
    # An ISOLATED database per test (a unique file, like test_undo.py): ids start
    # at 1 so 'WHERE id=1' means "the one row this test created", and -- because
    # pytest imports every test module into ONE process -- no other module can
    # ever see this database (the old shared-file version deleted the seeded
    # bays that test_time_engine.py depends on).
    conn = db.connect(os.path.join(_TMP, f"t_{_uuid.uuid4().hex}.db"))
    db.create_schema(conn)
    conn.execute("INSERT INTO bays (id, name, sort_order) VALUES (7,'Bay 7',7),(8,'Bay 8',8);")
    conn.commit()
    return conn


def add_recipient(conn, name, **kw):
    cols = {"email": None, "phone": None, "notify_email": 0, "notify_sms": 0,
            "bay_scope": "all", "control_scope": "all", "active": 1}
    cols.update(kw)
    conn.execute(
        "INSERT INTO recipients (name,email,phone,notify_email,notify_sms,bay_scope,control_scope,active)"
        " VALUES (?,?,?,?,?,?,?,?);",
        (name, cols["email"], cols["phone"], cols["notify_email"], cols["notify_sms"],
         cols["bay_scope"], cols["control_scope"], cols["active"]))
    conn.commit()


def delay(conn, bay_id, control, **kw):
    """Append a DELAY_START event the way actions.flag_delay does."""
    f = {"work_order": "W", "reason_label": "Reason", "division": "Div",
         "note": "n", "initials": "JP"}
    f.update(kw)
    return events.append(conn, "DELAY_START", bay_id=bay_id, in_out_of_control=control, **f)


def _reset_config():
    cfg.POSTMARK_TOKEN = None
    cfg.POSTMARK_FROM = None
    cfg.TWILIO_SID = cfg.TWILIO_AUTH_TOKEN = cfg.TWILIO_FROM = None


def _configure_email():
    cfg.POSTMARK_TOKEN = "tok"
    cfg.POSTMARK_FROM = "bt@co"


# ---------------------------------------------------------------------------


def test_scope_routing():
    print("== scope routing: bay_scope, control_scope, retired, no-address ==")
    conn = fresh_conn()
    add_recipient(conn, "All-bays", email="all@co", notify_email=1)
    add_recipient(conn, "Only-8", email="eight@co", notify_email=1, bay_scope="8")
    add_recipient(conn, "OOC-only", email="ooc@co", notify_email=1, control_scope="out")
    add_recipient(conn, "Retired", email="x@co", notify_email=1, active=0)
    add_recipient(conn, "Email-on-no-address", notify_email=1)            # no email -> skip
    add_recipient(conn, "SMS-only", phone="+1414", notify_sms=1)          # not email channel

    # Bay 7, in-control: All-bays gets an email; SMS-only (all/all scope) gets an
    # SMS. Only-8 (wrong bay), OOC-only (wrong control), Retired, and the
    # address-less email recipient are all skipped.
    ev = delay(conn, 7, "in", reason_label="Missing part", work_order="W1")
    notify.enqueue_notifications(conn, ev)
    emails = sorted(r["destination"] for r in conn.execute(
        "SELECT destination FROM notification_outbox WHERE delay_event_id=? AND channel='email';",
        (ev["id"],)).fetchall())
    smses = sorted(r["destination"] for r in conn.execute(
        "SELECT destination FROM notification_outbox WHERE delay_event_id=? AND channel='sms';",
        (ev["id"],)).fetchall())
    check("bay7 in-control emails", emails, ["all@co"])
    check("bay7 in-control sms", smses, ["+1414"])

    # Bay 8, out-of-control: All-bays (email) + Only-8 (email) + OOC-only (email)
    # + SMS-only (sms). Retired and no-address are still skipped.
    ev2 = delay(conn, 8, "out", reason_label="Machine down", work_order="W2")
    notify.enqueue_notifications(conn, ev2)
    dests = sorted(r["destination"] for r in conn.execute(
        "SELECT destination FROM notification_outbox WHERE delay_event_id=?;", (ev2["id"],)).fetchall())
    check("bay8 out-of-control -> 4 rows", len(dests), 4)
    check("bay8 destinations", dests, ["+1414", "all@co", "eight@co", "ooc@co"])
    conn.close()


def test_message_content_from_event():
    print("== message body/subject come from the snapshotted event ==")
    conn = fresh_conn()
    add_recipient(conn, "R", email="r@co", notify_email=1)
    ev = delay(conn, 7, "out", reason_label="Crane down", work_order="WO9",
               note="rigging", initials="AB")
    notify.enqueue_notifications(conn, ev)
    row = conn.execute("SELECT subject, body FROM notification_outbox WHERE delay_event_id=?;",
                       (ev["id"],)).fetchone()
    check("subject names bay + reason", row["subject"], "DELAY: Bay 7 — Crane down")
    body_ok = ("Bay 7 is DELAYED." in row["body"]
               and "out of control" in row["body"]
               and "WO9" in row["body"] and "AB" in row["body"]
               and "rigging" in row["body"])
    check("body has bay/control/WO/initials/note", body_ok, True)
    conn.close()


def test_worker_waits_when_unconfigured():
    print("== worker leaves rows PENDING when the channel isn't configured ==")
    _reset_config()
    conn = fresh_conn()
    add_recipient(conn, "R", email="r@co", notify_email=1)
    ev = delay(conn, 7, "in")
    notify.enqueue_notifications(conn, ev)
    notify.process_outbox_once(conn)
    st = conn.execute("SELECT status, attempts FROM notification_outbox WHERE id=1;").fetchone()
    check("status still pending", st["status"], "pending")
    check("no attempt burned", st["attempts"], 0)
    conn.close()


def test_worker_sends_when_configured():
    print("== configured + working adapter -> row goes 'sent' ==")
    _configure_email()
    conn = fresh_conn()
    add_recipient(conn, "R", email="r@co", notify_email=1)
    ev = delay(conn, 7, "in")
    notify.enqueue_notifications(conn, ev)

    sent = []
    orig = notify.send_email_postmark
    notify.send_email_postmark = lambda to, subj, body: sent.append(to)
    try:
        notify.process_outbox_once(conn)
    finally:
        notify.send_email_postmark = orig
    row = conn.execute("SELECT status, sent_at FROM notification_outbox WHERE id=1;").fetchone()
    check("status sent", row["status"], "sent")
    check("sent_at recorded", bool(row["sent_at"]), True)
    check("adapter called once with address", sent, ["r@co"])
    conn.close()


def test_failed_send_retries_then_fails():
    print("== a failing send retries with backoff, then 'failed' at the cap ==")
    _configure_email()
    conn = fresh_conn()
    add_recipient(conn, "R", email="r@co", notify_email=1)
    ev = delay(conn, 7, "in")
    notify.enqueue_notifications(conn, ev)

    def boom(to, subj, body):
        raise RuntimeError("smtp blew up")

    orig = notify.send_email_postmark
    notify.send_email_postmark = boom
    try:
        # First failure -> back to pending, attempts=1, next_attempt_at in the future.
        notify.process_outbox_once(conn)
        r1 = conn.execute("SELECT status, attempts, last_error FROM notification_outbox WHERE id=1;").fetchone()
        check("after 1st failure: pending", r1["status"], "pending")
        check("attempts incremented", r1["attempts"], 1)
        check("last_error captured", "smtp blew up" in (r1["last_error"] or ""), True)

        # The backoff put next_attempt_at in the future, so an immediate pass is a no-op.
        notify.process_outbox_once(conn)
        check("not retried before backoff elapses",
              conn.execute("SELECT attempts FROM notification_outbox WHERE id=1;").fetchone()["attempts"], 1)

        # Force every attempt due and exhaust the cap; the row must end 'failed'.
        for _ in range(notify.MAX_ATTEMPTS + 1):
            conn.execute("UPDATE notification_outbox SET next_attempt_at=datetime('now','localtime','-1 hour') "
                         "WHERE id=1 AND status='pending';")
            conn.commit()
            notify.process_outbox_once(conn)
        final = conn.execute("SELECT status, attempts FROM notification_outbox WHERE id=1;").fetchone()
        check("ends failed at the cap", final["status"], "failed")
        check("attempts capped at MAX_ATTEMPTS", final["attempts"], notify.MAX_ATTEMPTS)
        check("recent_failures surfaces it", len(notify.recent_failures(conn)), 1)
    finally:
        notify.send_email_postmark = orig
    conn.close()


def test_destination_snapshot_is_stable():
    print("== destination is snapshotted; editing the recipient doesn't rewrite it ==")
    conn = fresh_conn()
    add_recipient(conn, "R", email="old@co", notify_email=1)
    ev = delay(conn, 7, "in")
    notify.enqueue_notifications(conn, ev)
    conn.execute("UPDATE recipients SET email='new@co' WHERE id=1;")
    conn.commit()
    dest = conn.execute("SELECT destination FROM notification_outbox WHERE id=1;").fetchone()["destination"]
    check("outbox keeps the enqueue-time address", dest, "old@co")
    conn.close()


def main():
    try:
        test_scope_routing()
        test_message_content_from_event()
        test_worker_waits_when_unconfigured()
        test_worker_sends_when_configured()
        test_failed_send_retries_then_fails()
        test_destination_snapshot_is_stable()
    finally:
        _reset_config()
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("ALL NOTIFY TESTS PASSED" if not _FAILS else f"FAILURES: {_FAILS}"))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
