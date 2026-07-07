"""
test_incidents.py -- regression tests for the EHS incident report (incidents.py).

The incident path shares the reliability core with delay notifications: reporting
an accident must file the record AND enqueue leadership alerts to the outbox
(never over the network), and the same worker sends them. Run it after touching
incidents.py / the /api/incident routes / the incidents+outbox schema:

    python tests/test_incidents.py

Throwaway temp database. Asserts:
  * a preliminary alert files a minimal incident row (prelim_sent_at set) and
    queues a PRELIMINARY alert to every leadership recipient with a channel,
  * finalize() fills that SAME row (no duplicate) and stamps finalized_at,
  * record_full() inserts a complete row in one go (the no-preliminary path),
  * incident outbox rows carry incident_id + kind and delay_event_id=0 (the
    sentinel), and are picked up by the shared worker,
  * required-field validation rejects a submit missing initials or "what",
  * the existing worker sends an incident row 'sent' when a channel is configured.

No pytest required. Exits non-zero on any failure so it can gate an update.
"""

import os
import shutil
import sys
import tempfile
import uuid as _uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="bt_incd_")
os.environ["BAYTRACKER_DATA"] = _TMP

from baytracker import db, incidents, notify                     # noqa: E402
from baytracker import notify_config as cfg                      # noqa: E402

_FAILS = []


def check(name, got, want):
    ok = (got == want)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, want {want!r}")
    if not ok:
        _FAILS.append(name)


def fresh_conn():
    conn = db.connect(os.path.join(_TMP, f"t_{_uuid.uuid4().hex}.db"))
    db.create_schema(conn)
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


def _reset_config():
    cfg.POSTMARK_TOKEN = None
    cfg.POSTMARK_FROM = None
    cfg.TWILIO_SID = cfg.TWILIO_AUTH_TOKEN = cfg.TWILIO_FROM = None


# ---------------------------------------------------------------------------


def test_preliminary_then_finalize_one_row():
    print("== preliminary files a row + queues PRELIMINARY; finalize fills the SAME row ==")
    conn = fresh_conn()
    add_recipient(conn, "Boss", email="boss@co", phone="+1414", notify_email=1, notify_sms=1)
    add_recipient(conn, "EHS", email="ehs@co", notify_email=1)          # email only
    add_recipient(conn, "Retired", email="x@co", notify_email=1, active=0)   # skipped
    add_recipient(conn, "NoChannel", email="n@co")                     # channel off -> skipped

    inc = incidents.start_preliminary(conn, type="ACCIDENT", location="Bay 3", reported_by="JP")
    check("prelim_sent_at set", bool(inc["prelim_sent_at"]), True)
    echo = incidents.enqueue_incident(conn, inc, "PRELIMINARY")
    # Boss (email+sms) + EHS (email) => 2 email, 1 sms.
    check("prelim email recipients", echo["recipients"]["email"], 2)
    check("prelim sms recipients", echo["recipients"]["sms"], 1)

    rows = conn.execute("SELECT delay_event_id, incident_id, kind FROM notification_outbox;").fetchall()
    check("all rows use the sentinel delay_event_id=0", all(r["delay_event_id"] == 0 for r in rows), True)
    check("all rows tagged with the incident id", all(r["incident_id"] == inc["id"] for r in rows), True)
    check("all rows kind=PRELIMINARY", {r["kind"] for r in rows}, {"PRELIMINARY"})

    inc2 = incidents.finalize(conn, inc["id"], occurred_at="2026-07-07T09:15",
            location="Bay 3", reported_by="JP", severity="Recordable",
            what_happened="Slipped on coolant.", immediate_action="Area secured.")
    check("finalized_at set", bool(inc2["finalized_at"]), True)
    check("exactly one incident row (finalize did not duplicate)",
          conn.execute("SELECT COUNT(*) n FROM incidents;").fetchone()["n"], 1)
    incidents.enqueue_incident(conn, inc2, "DETAILED")
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM notification_outbox;").fetchall()}
    check("both alert kinds now queued", kinds, {"PRELIMINARY", "DETAILED"})
    conn.close()


def test_record_full_no_preliminary():
    print("== record_full inserts a complete near-miss in one go ==")
    conn = fresh_conn()
    add_recipient(conn, "Boss", email="boss@co", notify_email=1)
    nm = incidents.record_full(conn, type="NEAR_MISS", occurred_at="2026-07-07T10:00",
            location="Yard", reported_by="RM", potential="High",
            what_happened="Forklift near-miss.")
    check("type recorded", nm["type"], "NEAR_MISS")
    check("finalized immediately", bool(nm["finalized_at"]), True)
    echo = incidents.enqueue_incident(conn, nm, "DETAILED")
    body_ok = ("NEAR MISS" in echo["body"] and "Potential severity: High" in echo["body"]
               and "Forklift near-miss." in echo["body"])
    check("detailed body has near-miss fields", body_ok, True)
    conn.close()


def test_validation_requires_initials_and_what():
    print("== submit missing initials or 'what' is rejected ==")
    conn = fresh_conn()
    try:
        incidents.record_full(conn, type="ACCIDENT", reported_by="ZZ")   # no 'what'
        check("missing what raises", False, True)
    except ValueError:
        check("missing what raises", True, True)
    try:
        incidents.record_full(conn, type="ACCIDENT", what_happened="x")  # no initials
        check("missing initials raises", False, True)
    except ValueError:
        check("missing initials raises", True, True)
    try:
        incidents.record_full(conn, type="BOGUS", reported_by="ZZ", what_happened="x")
        check("bad type raises", False, True)
    except ValueError:
        check("bad type raises", True, True)
    conn.close()


def test_shared_worker_sends_incident_when_configured():
    print("== the existing outbox worker sends an incident row when a channel is set ==")
    _reset_config()
    conn = fresh_conn()
    add_recipient(conn, "Boss", email="boss@co", notify_email=1)
    inc = incidents.record_full(conn, type="ACCIDENT", reported_by="JP",
            what_happened="Test.", location="Bay 1")
    incidents.enqueue_incident(conn, inc, "DETAILED")

    # Unconfigured -> row stays pending (no attempt burned).
    notify.process_outbox_once(conn)
    st = conn.execute("SELECT status, attempts FROM notification_outbox WHERE id=1;").fetchone()
    check("pending while unconfigured", (st["status"], st["attempts"]), ("pending", 0))

    # Configure email + stub the adapter -> row goes 'sent'.
    cfg.POSTMARK_TOKEN, cfg.POSTMARK_FROM = "tok", "ehs@co"
    sent = []
    orig = notify.send_email_postmark
    notify.send_email_postmark = lambda to, subj, body: sent.append((to, subj))
    try:
        notify.process_outbox_once(conn)
    finally:
        notify.send_email_postmark = orig
    row = conn.execute("SELECT status FROM notification_outbox WHERE id=1;").fetchone()
    check("row goes sent", row["status"], "sent")
    check("adapter called with the incident subject",
          sent and sent[0][0] == "boss@co" and "incident report" in sent[0][1], True)
    conn.close()


def main():
    try:
        test_preliminary_then_finalize_one_row()
        test_record_full_no_preliminary()
        test_validation_requires_initials_and_what()
        test_shared_worker_sends_incident_when_configured()
    finally:
        _reset_config()
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("ALL INCIDENT TESTS PASSED" if not _FAILS else f"FAILURES: {_FAILS}"))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
