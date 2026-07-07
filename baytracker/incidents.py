"""
incidents.py -- the EHS accident / near-miss report and its leadership alerts.

This is the server side of the "Report an incident" page (templates/incident.html
+ static/js/incident.js). It follows the same reliability principle as delay
notifications (see notify.py):

    The web request that reports an incident NEVER sends anything over the
    network. It writes the incident to the local `incidents` log and writes
    'pending' rows to notification_outbox. The SAME background worker that sends
    delay alerts (notify.start_outbox_worker) then delivers them and retries
    with backoff. A wifi drop at the moment of an accident can't lose the alert.

The hybrid notification flow (matches the design):

    1. Accident in progress -> an OPTIONAL immediate *preliminary* alert texts
       leadership "an accident occurred at <location>" before the form is filled
       (start_preliminary + enqueue_incident(kind="PRELIMINARY")). This also
       files the incident row straight away, so an alert fired then abandoned
       still leaves a record that something happened.
    2. The full form is submitted -> the *detailed* report is filed and texted
       (finalize / record_full + enqueue_incident(kind="DETAILED")).

Incident alerts go to every ACTIVE recipient that has a usable channel -- an
accident is plant-wide, so it deliberately ignores the per-recipient bay /
control scopes that delay alerts honour. Recipients are the same list the admin
already manages in /admin; no separate address book to keep in sync.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import config
from . import notify_config as cfg

# The two report kinds and their display labels.
INCIDENT_TYPES = {"ACCIDENT", "NEAR_MISS"}
_TYPE_LABEL = {"ACCIDENT": "Accident", "NEAR_MISS": "Near miss"}
_TYPE_TAG = {"ACCIDENT": "⚠ ACCIDENT", "NEAR_MISS": "◇ NEAR MISS"}

# The EHS-standard vocabularies the form offers. These are domain constants, not
# company config, so they live here rather than in an admin-editable table.
SEVERITIES = ["First aid only", "Recordable", "Lost-time", "Serious — emergency"]
MEDICALS = ["None needed", "First aid on site", "Sent to clinic", "911 called"]
POTENTIALS = ["Low", "Medium", "High"]
# Non-bay plant areas appended to the bay list on the location dropdown.
EXTRA_LOCATIONS = ["Weld cell", "Paint line", "Receiving dock", "Yard", "Other"]

# The incident columns a full report may carry, in a stable order (mirrors the
# events.py EVENT_COLUMNS idea so INSERT/UPDATE never drift from the schema).
_DETAIL_FIELDS = [
    "occurred_at", "location", "reported_by", "severity", "potential",
    "person", "injury", "medical", "what_happened", "immediate_action", "equipment",
]


def _now() -> str:
    """Server-side plant wall-clock timestamp, same format as everything else."""
    return datetime.now().strftime(config.TS_FORMAT)


def _clean_type(raw) -> str:
    t = (raw or "").strip().upper()
    if t not in INCIDENT_TYPES:
        raise ValueError(f"Unknown incident type: {raw!r}")
    return t


# ---------------------------------------------------------------------------
# Writing the incident record
# ---------------------------------------------------------------------------

def start_preliminary(conn: sqlite3.Connection, *, type: str, location,
                      reported_by) -> sqlite3.Row:
    """File a minimal incident row the instant a preliminary alert is fired.

    Only what's known at that point (type, location, initials) is stored; the
    detailed fields are filled in later by finalize(). prelim_sent_at is stamped
    so the record shows an alert went out even if the form is never completed.
    Returns the new row (its id is what the browser sends back to finalize())."""
    t = _clean_type(type)
    now = _now()
    cur = conn.execute(
        "INSERT INTO incidents (ts, type, location, reported_by, prelim_sent_at) "
        "VALUES (?, ?, ?, ?, ?);",
        (now, t, (location or None), (reported_by or None), now))
    conn.commit()
    return conn.execute("SELECT * FROM incidents WHERE id = ?;",
                        (cur.lastrowid,)).fetchone()


def finalize(conn: sqlite3.Connection, incident_id: int, **fields) -> sqlite3.Row:
    """Fill in the full details on an incident row started by a preliminary alert.

    This is the one place `incidents` is UPDATEd rather than appended to. It is
    safe here (and NOT a violation of the append-only rule the `events` table
    lives by) because `incidents` does not drive any replayed state -- it is a
    plain log. We only ever fill blanks + stamp finalized_at; we never rewrite an
    already-finalized row."""
    row = conn.execute("SELECT * FROM incidents WHERE id = ?;", (incident_id,)).fetchone()
    if row is None:
        raise ValueError(f"No incident #{incident_id} to finalize.")
    vals = _validated_details(fields)
    sets = ", ".join(f"{k} = ?" for k in _DETAIL_FIELDS) + ", finalized_at = ?"
    conn.execute(
        f"UPDATE incidents SET {sets} WHERE id = ?;",
        [vals[k] for k in _DETAIL_FIELDS] + [_now(), incident_id])
    conn.commit()
    return conn.execute("SELECT * FROM incidents WHERE id = ?;",
                        (incident_id,)).fetchone()


def record_full(conn: sqlite3.Connection, *, type: str, **fields) -> sqlite3.Row:
    """Insert a complete incident in one go (the no-preliminary-alert path)."""
    t = _clean_type(type)
    vals = _validated_details(fields)
    now = _now()
    cols = ["ts", "type"] + _DETAIL_FIELDS + ["finalized_at"]
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO incidents ({', '.join(cols)}) VALUES ({placeholders});",
        [now, t] + [vals[k] for k in _DETAIL_FIELDS] + [now])
    conn.commit()
    return conn.execute("SELECT * FROM incidents WHERE id = ?;",
                        (cur.lastrowid,)).fetchone()


def _validated_details(fields: dict) -> dict:
    """Normalise the detail fields and enforce the two required at submit."""
    vals = {k: (str(fields.get(k)).strip() if fields.get(k) not in (None, "") else None)
            for k in _DETAIL_FIELDS}
    if not vals["reported_by"]:
        raise ValueError("Reported-by (initials) is required.")
    if not vals["what_happened"]:
        raise ValueError("A 'what happened' description is required.")
    return vals


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------

def _short_time(ts: str) -> str:
    """'2:07 PM' from a stored 'YYYY-MM-DD HH:MM:SS' timestamp (best-effort)."""
    try:
        return datetime.strptime(ts, config.TS_FORMAT).strftime("%-I:%M %p")
    except (ValueError, TypeError):
        try:  # Windows strftime has no %-I; fall back to %I with a stripped zero
            return datetime.strptime(ts, config.TS_FORMAT).strftime("%I:%M %p").lstrip("0")
        except (ValueError, TypeError):
            return ts or "—"


def _preliminary_body(incident: sqlite3.Row) -> str:
    tag = "⚠ ACCIDENT" if incident["type"] == "ACCIDENT" else "◇ NEAR MISS"
    loc = incident["location"] or "—"
    by = incident["reported_by"] or "—"
    return (f"{tag} reported — {loc}. Reported by {by} at "
            f"{_short_time(incident['prelim_sent_at'] or incident['ts'])}. "
            f"Details to follow. {cfg.DASHBOARD_URL}")


def _detailed_body(incident: sqlite3.Row) -> str:
    is_accident = incident["type"] == "ACCIDENT"
    lines = [f"{_TYPE_TAG[incident['type']]} — incident report"]
    lines.append("Location: " + (incident["location"] or "—"))
    when = (incident["occurred_at"] or "").replace("T", " ") or "—"
    lines.append("When: " + when)
    lines.append("Reported by: " + (incident["reported_by"] or "—"))
    if is_accident:
        lines.append("Severity: " + (incident["severity"] or "—"))
        if incident["person"]:
            lines.append("Person: " + incident["person"])
        if incident["injury"]:
            lines.append("Injury: " + incident["injury"])
        lines.append("Medical: " + (incident["medical"] or "—"))
    else:
        lines.append("Potential severity: " + (incident["potential"] or "—"))
    lines.append("What happened: " + (incident["what_happened"] or "—"))
    if incident["immediate_action"]:
        lines.append("Immediate action: " + incident["immediate_action"])
    if incident["equipment"]:
        lines.append("Equipment/product: " + incident["equipment"])
    lines.append("")
    lines.append(cfg.DASHBOARD_URL)
    return "\n".join(lines)


def _subject(incident: sqlite3.Row, kind: str) -> str:
    label = _TYPE_LABEL[incident["type"]]
    loc = incident["location"] or "—"
    if kind == "PRELIMINARY":
        return f"⚠ {label} reported — {loc} (preliminary)"
    return f"{label} incident report — {loc}"


# ---------------------------------------------------------------------------
# Enqueue -- write outbox rows only (network-free; the worker sends)
# ---------------------------------------------------------------------------

def _leadership_recipients(conn: sqlite3.Connection):
    """Active EHS recipients with at least one usable channel.

    This reads the dedicated `ehs_recipients` list -- SEPARATE from the delay
    `recipients` list (edited independently in /admin), because the people who
    need to hear about an injury usually differ from the bay-delay list. An
    accident is plant-wide, so there is no bay/control scope here."""
    return conn.execute(
        "SELECT * FROM ehs_recipients WHERE active = 1 "
        "AND ((notify_email = 1 AND email IS NOT NULL AND email <> '') "
        "  OR (notify_sms = 1 AND phone IS NOT NULL AND phone <> ''));").fetchall()


def enqueue_incident(conn: sqlite3.Connection, incident: sqlite3.Row, kind: str) -> dict:
    """Write outbox rows for one incident notification. Network-free; instant.

    kind is 'PRELIMINARY' (the immediate accident-in-progress heads-up) or
    'DETAILED' (the full report). Returns a small summary the page can echo back
    to the operator: the message body actually queued and how many people it
    went to, per channel.
    """
    kind = kind.upper()
    if kind not in ("PRELIMINARY", "DETAILED"):
        raise ValueError(f"Unknown incident alert kind: {kind!r}")

    body = _preliminary_body(incident) if kind == "PRELIMINARY" else _detailed_body(incident)
    subject = _subject(incident, kind)

    email_count = sms_count = 0
    for r in _leadership_recipients(conn):
        if r["notify_email"] and r["email"]:
            conn.execute(
                """INSERT INTO notification_outbox
                   (delay_event_id, incident_id, kind, recipient_id, channel,
                    destination, subject, body)
                   VALUES (0, ?, ?, ?, 'email', ?, ?, ?)""",
                (incident["id"], kind, r["id"], r["email"], subject, body))
            email_count += 1
        if r["notify_sms"] and r["phone"]:
            conn.execute(
                """INSERT INTO notification_outbox
                   (delay_event_id, incident_id, kind, recipient_id, channel,
                    destination, body)
                   VALUES (0, ?, ?, ?, 'sms', ?, ?)""",
                (incident["id"], kind, r["id"], r["phone"], body))
            sms_count += 1
    conn.commit()

    return {
        "kind": kind,
        "time": _short_time(_now()),
        "body": body,
        "recipients": {"email": email_count, "sms": sms_count},
    }
