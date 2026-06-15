# BayTracker — Delay Notifications (Email + SMS) Build Spec

This adds automatic email and text alerts when a bay enters the **DELAYED** state. It slots into the existing Flask + SQLite app and follows the patterns this codebase actually uses: an append-only `events` log as the single source of truth, config tables edited live in `/admin`, server-side timestamps, and never inventing data.

The reliability design rests on one principle: **the web request that logs a delay never sends anything over the network.** It only writes "intent to send" rows to a local outbox table. A background worker (the same daemon-thread pattern as the existing heartbeat) does the actual sending and retries until it succeeds. A wifi drop at the moment a delay is logged can't slow the technician down or lose an alert.

> **How this differs from the first draft.** The original spec was written against an imagined `delays` + `reasons` schema. This app is **event-sourced**: a delay is a `DELAY_START` row in the [`events`](baytracker/db.py) table, the reason config table is `delay_reasons`, the control tag is `in_out_of_control` with values `'in'|'out'|NULL`, and bays are `id` + `name` (e.g. "Bay 1") rather than a bare integer. Everything below is written against the real code. The reliability architecture from the draft survives unchanged — only the bindings to the schema change.

---

## 0. How a delay actually happens here (the integration surface)

Read this first; everything keys off it.

- A technician flags a delay via [`actions.flag_delay`](baytracker/actions.py). That function snapshots the reason's `label`, `division`, and `in_out_of_control` tag **onto** the event and appends a `DELAY_START` row via `events.append(...)`. It returns the full event row.
- The route [`app.py` `api_action`](app.py) already special-cases that row:

  ```python
  _publish_state(conn)
  if row is not None and row["type"] == "DELAY_START":
      _publish_delay_takeover(conn, row)
  ```

  This is our hook. We add **one line** right after the takeover.
- A `DELAY_START` event row carries everything a message needs, already snapshotted (history-stable):
  `id`, `ts`, `bay_id`, `work_order`, `product_number`, `reason_label`, `division`, `in_out_of_control` (`'in'|'out'|NULL`), `note`, `initials`.
- The **only** thing not on the event is the human bay name; resolve `bay_id → bays.name` at enqueue time (display-only, snapshotted into the message body so it stays accurate forever).

Because the event already has the snapshot, **we never re-query `delay_reasons`** — doing so would reintroduce exactly the history-rewriting bug the snapshot design exists to prevent.

---

## 1. Recipients live in *your* database, not the vendor

A recipient is a person with a name, an email and/or phone, channel preferences, and a scope of what they want to hear about. Same idea as `delay_reasons`: an admin page edits it live, no code changes.

```sql
CREATE TABLE IF NOT EXISTS recipients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT,
    phone         TEXT,                          -- E.164 only, e.g. +14145551234
    notify_email  INTEGER NOT NULL DEFAULT 0,    -- 0/1
    notify_sms    INTEGER NOT NULL DEFAULT 0,    -- 0/1
    bay_scope     TEXT    NOT NULL DEFAULT 'all', -- 'all' or CSV of bays.id, e.g. '7,8,9'
    control_scope TEXT    NOT NULL DEFAULT 'all', -- 'all' or 'out' (out-of-control only)
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
```

Two deliberate changes from the draft, both to match reality:

- **`bay_scope` stores bay `id`s, not "bay numbers."** Bays are renameable and soft-retired, and their displayed name is free-form. The admin form shows a checklist of bay **names**; we store the selected `bays.id` values as a CSV (or the literal `'all'`). This is robust against renames and never needs an integer "number" the schema doesn't have. (If you'd rather keep the draft's free-text shortcut, you can — but then matching has to parse the numeric part of each bay's name, which breaks the moment a bay is renamed "Weld Cell A". The id-CSV is the safer default.)
- **`control_scope`** replaces the draft's `reason_scope`. The real control tag is `in_out_of_control IN ('in','out',NULL)`. `'all'` hears everything; `'out'` hears only delays the technician couldn't control (`in_out_of_control = 'out'`). A maintenance lead gets `'out'`.

`datetime('now','localtime')` matches this project's convention that all stored timestamps are plant wall-clock (see [`config.TS_FORMAT`](baytracker/config.py)).

---

## 2. The outbox — the reliability core

Every intended send is a row here. This table *is* the queue and the audit log.

```sql
CREATE TABLE IF NOT EXISTS notification_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delay_event_id  INTEGER NOT NULL,              -- FK events.id of the DELAY_START row
    recipient_id    INTEGER,                       -- FK recipients.id (NULL if recipient later deleted)
    channel         TEXT    NOT NULL,              -- 'email' | 'sms'
    destination     TEXT    NOT NULL,              -- snapshot of email/phone at enqueue time
    subject         TEXT,                          -- email only
    body            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending|sending|sent|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON notification_outbox(status, next_attempt_at);
```

Note `delay_event_id` (not `delay_id`) — it references `events.id` of the `DELAY_START` row, the only "delay record" this app has. `destination` is snapshotted at enqueue time so editing a recipient's phone later never rewrites what was actually sent. Foreign keys are declared for documentation only — this project deliberately runs with `PRAGMA foreign_keys` **off** (see [`db.connect`](baytracker/db.py)) so soft-retired/snapshotted rows never fight enforcement.

**Where these go:** add both `CREATE TABLE` statements (and the index) to `SCHEMA_SQL` in [`baytracker/db.py`](baytracker/db.py) — that is the single source for the schema, run idempotently by `init_db.py` *and* at app startup. For PCs whose database already exists, also add a migration to [`migrate.py`](migrate.py) so an in-place update creates the tables:

```python
# migrate.py
def _add_notifications(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recipients ( ... );
        CREATE TABLE IF NOT EXISTS notification_outbox ( ... );
        CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON notification_outbox(status, next_attempt_at);
    """)
    conn.commit()

MIGRATIONS = [_add_notifications]
```

`CREATE TABLE IF NOT EXISTS` makes this safe to run repeatedly and harmless on a fresh DB where `db.create_schema` already made the tables. Bump `SCHEMA_VERSION` to 2 in `db.py` (informational, per the migrate.py contract).

---

## 3. The trigger — hook into the DELAYED transition

Add a `notify` module (`baytracker/notify.py`). Its `enqueue_notifications` takes the **DELAY_START event row** the action already returns — no query against `delays`/`reasons`, because neither exists and the event is already a complete snapshot.

```python
# baytracker/notify.py
from . import notify_config as cfg

_CONTROL_TEXT = {"in": "in control", "out": "out of control"}

def enqueue_notifications(conn, event):
    """Write outbox rows for one DELAY_START event. Network-free; instant.
    `event` is the sqlite3.Row returned by actions.flag_delay / events.append."""
    bay = conn.execute("SELECT name FROM bays WHERE id = ?;",
                       (event["bay_id"],)).fetchone()
    bay_name = bay["name"] if bay else f"Bay {event['bay_id']}"
    control = _CONTROL_TEXT.get(event["in_out_of_control"])  # may be None
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


def _recipient_matches(r, event):
    if r["bay_scope"] != "all":
        bay_ids = {int(b) for b in r["bay_scope"].split(",") if b.strip()}
        if event["bay_id"] not in bay_ids:
            return False
    if r["control_scope"] == "out" and event["in_out_of_control"] != "out":
        return False
    return True
```

**Wiring the hook** — one line in [`app.py` `api_action`](app.py), guarded so a notification failure can never break the action that logged the delay:

```python
_publish_state(conn)
if row is not None and row["type"] == "DELAY_START":
    _publish_delay_takeover(conn, row)
    try:
        notify.enqueue_notifications(conn, row)   # writes outbox rows only — no network
    except Exception:
        pass  # logging the delay must succeed even if enqueue hiccups
```

Every field in the message comes straight off the event row — no placeholders, no estimates — consistent with the no-fake-data rule. (`work_order` is included because it's already on the event and genuinely useful in the alert; drop it if you prefer the draft's shorter body.)

---

## 4. The send adapters

Two small functions, one per vendor. Each raises on any failure so the worker treats it as retryable. `requests` is a new dependency (see §6).

```python
# baytracker/notify.py (continued)
import requests
from . import notify_config as cfg

def send_email_postmark(to, subject, body):
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

def send_sms_twilio(to, body):
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{cfg.TWILIO_SID}/Messages.json",
        auth=(cfg.TWILIO_SID, cfg.TWILIO_AUTH_TOKEN),
        data={"From": cfg.TWILIO_FROM, "To": to, "Body": body},
        timeout=10,
    )
    resp.raise_for_status()
```

---

## 5. The background worker

A single daemon thread — the **exact pattern already proven** by [`_start_heartbeat`](app.py): a `while True` loop that opens its **own** `db.connect()`, does its work, closes, sleeps, and never lets an exception kill the thread. WAL mode is already on, so the worker's writes and the request threads' writes coexist cleanly.

```python
# baytracker/notify.py (continued)
import time, threading
from . import db

BACKOFF_SECONDS = [30, 120, 300, 900, 3600]   # waits between attempts
MAX_ATTEMPTS = len(BACKOFF_SECONDS)

def process_outbox_once(conn):
    rows = conn.execute("""
        SELECT * FROM notification_outbox
        WHERE status = 'pending' AND next_attempt_at <= datetime('now','localtime')
        ORDER BY id LIMIT 20
    """).fetchall()

    for row in rows:
        # claim the row so it can't be picked up twice
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

def start_outbox_worker(interval=20):
    def loop():
        while True:
            try:
                conn = db.connect()         # worker opens its OWN connection (per-thread)
                try:
                    process_outbox_once(conn)
                finally:
                    conn.close()
            except Exception:
                pass                         # never let the worker thread die
            time.sleep(interval)
    threading.Thread(target=loop, name="bt-outbox", daemon=True).start()
```

Two things that matter and are already true here:

- **The worker opens its own SQLite connection** via `db.connect()` (SQLite connections aren't shareable across threads). This mirrors the heartbeat exactly, including WAL + `busy_timeout=30000`.
- **Run exactly one app process.** The `status='sending'` claim guards against double-sends within a process; the clean rule on the single floor PC is one waitress process, one worker thread. Don't scale to multiple worker processes without revisiting the claim.

**Start it where the heartbeat starts** — inside `create_app()`, next to `_start_heartbeat()`:

```python
_start_heartbeat()
notify.start_outbox_worker()    # add this
return app
```

Use the same one-shot guard idiom as `_heartbeat_started` if you want belt-and-suspenders against double-start under the reloader (not needed under waitress).

---

## 6. Config and secrets

This project's hard rule is that **secrets and data live OUTSIDE the repo** — the Flask secret key sits in `DATA_DIR`, never committed (see [`config.get_secret_key`](baytracker/config.py)), and `BAYTRACKER_DATA` is passed via the service environment. Notification secrets follow the same philosophy rather than a repo-root `.env`:

1. **Primary: environment variables**, set in the NSSM service definition right next to `BAYTRACKER_DATA` (see `setup.ps1`). Nothing secret touches git or the backed-up DB.
2. **Dev convenience: an optional `notify.env` in `DATA_DIR`** (outside the repo, already un-synced/un-committed), loaded with `python-dotenv` pointed explicitly at that path.

```python
# baytracker/notify_config.py
import os
from . import config

# Optional: load DATA_DIR/notify.env if python-dotenv is installed and the file exists.
try:
    from dotenv import load_dotenv
    _envfile = config.DATA_DIR / "notify.env"
    if _envfile.exists():
        load_dotenv(_envfile)
except ImportError:
    pass

POSTMARK_TOKEN    = os.environ.get("POSTMARK_TOKEN")
POSTMARK_FROM     = os.environ.get("POSTMARK_FROM")        # a verified sender, e.g. baytracker@yourco.com
DASHBOARD_URL     = os.environ.get("DASHBOARD_URL", "http://localhost:5000/dashboard")
TWILIO_SID        = os.environ.get("TWILIO_SID")           # blank until Phase 2
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM       = os.environ.get("TWILIO_FROM")          # +1XXXXXXXXXX

def email_configured() -> bool:
    return bool(POSTMARK_TOKEN and POSTMARK_FROM)

def sms_configured() -> bool:
    return bool(TWILIO_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM)
```

Use `os.environ.get` (not `os.environ[...]`) so a PC with notifications not yet configured still boots — the admin failure view and `email_configured()`/`sms_configured()` flags make the "not set up yet" state visible instead of crashing the app at import.

`DATA_DIR/notify.env` example (never committed; lives outside the repo):

```
POSTMARK_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
POSTMARK_FROM=baytracker@yourcompany.com
DASHBOARD_URL=http://10.0.0.5:5000/dashboard
# Phase 2 (leave commented until 10DLC is registered):
# TWILIO_SID=ACxxxxxxxx
# TWILIO_AUTH_TOKEN=xxxxxxxx
# TWILIO_FROM=+1XXXXXXXXXX
```

Add to [`requirements.txt`](requirements.txt), exact-pinned to match the existing house style (resolve the real current pins at install time):

```
requests==2.32.x
python-dotenv==1.0.x
```

`*.env` is **not** currently in [`.gitignore`](.gitignore); since the recommended file lives in `DATA_DIR` (outside the repo) it can't be committed anyway, but add `*.env` and `notify.env` to `.gitignore` as belt-and-suspenders in case someone drops one in the repo during testing.

---

## 7. Admin page — slot into the existing `/admin`, don't add `/recipients`

There is **no per-feature page pattern** in this app. Admin is a single [`templates/admin.html`](templates/admin.html) driven by [`static/js/admin.js`](static/js/admin.js), hydrated by one `GET /api/admin/data` endpoint, with each editable table mutated through an op-dispatch `POST /api/admin/<thing>` (`op = add|update|delete`), all gated by `@auth.require_area("admin")`. Recipients follow that pattern exactly:

1. **Extend `/api/admin/data`** ([`app.py` `admin_data`](app.py)) to include `recipients` and a `notify` block (config flags + recent failures):

   ```python
   recipients = [dict(r) for r in conn.execute(
       "SELECT * FROM recipients ORDER BY active DESC, name;").fetchall()]
   failures = [dict(r) for r in conn.execute(
       "SELECT * FROM notification_outbox WHERE status='failed' "
       "ORDER BY created_at DESC LIMIT 50;").fetchall()]
   # ... add to the returned dict:
   #   "recipients": recipients,
   #   "notify": {"email_configured": notify_config.email_configured(),
   #              "sms_configured":   notify_config.sms_configured(),
   #              "failures": failures},
   ```

2. **Add `POST /api/admin/recipient`** mirroring [`admin_reason`](app.py) — `op` in `add|update|delete`, same validation/`conn.commit()` shape. Normalize the phone to E.164 on add/update (see §10). For `bay_scope`, accept `'all'` or join the checked bay ids into a CSV string.

3. **Add the recipients UI section** to `admin.html` + `admin.js`: a table with add/edit/retire and per-person fields — name, email, phone (hint "E.164, e.g. +14145551234"), email on/off, SMS on/off, bay scope (checklist of bay names → ids, or "All"), control scope ("All" / "Out-of-control only"), active. Reuse the existing admin table styling and the save-confirmation toast/banner you already built.

4. **Per-channel "Send test"** that calls the adapter directly (bypassing the outbox) to confirm vendor config before waiting for a real delay:

   ```python
   @app.route("/api/admin/recipient_test", methods=["POST"])
   @auth.require_area("admin")
   def admin_recipient_test():
       conn = get_db()
       p = request.get_json(force=True, silent=True) or {}
       r = conn.execute("SELECT * FROM recipients WHERE id=?;", (p.get("id"),)).fetchone()
       if r is None:
           return jsonify({"ok": False, "error": "No such recipient."}), 404
       try:
           if r["notify_email"] and r["email"]:
               notify.send_email_postmark(r["email"], "BayTracker test", "Test alert — config OK.")
           if r["notify_sms"] and r["phone"]:
               notify.send_sms_twilio(r["phone"], "BayTracker test — config OK.")
       except Exception as e:
           return jsonify({"ok": False, "error": str(e)}), 502
       return jsonify({"ok": True})
   ```

   Note this returns JSON (the app's admin is a JSON-driven SPA-style page), not a `redirect` — the draft's `redirect(url_for("recipients_page"))` doesn't fit this app's admin, which never full-page-navigates.

---

## 8. Make failures visible

A permanently failed send must not be silent. The `notify.failures` list is already in `/api/admin/data` (§7.1). Render it as a small banner/section at the top of the admin recipients area:

```sql
SELECT * FROM notification_outbox
WHERE status = 'failed'
ORDER BY created_at DESC
LIMIT 50;
```

If anything shows up, `last_error` tells you why. Also surface the `email_configured` / `sms_configured` flags so "no alerts are going out because nothing is configured" is obvious rather than mysterious.

---

## 9. Rollout — email first, SMS when registration is real

**Phase 1 — Email only (do this now).** Postmark, the two tables (schema + migration), the `notify` module (enqueue + email adapter + worker), the one-line hook in `api_action`, the admin section, the test button, the failure view. Email has no compliance dependency, so this is self-contained and trustworthy. Get it running.

**Phase 2 — Add SMS (only when ready).** The outbox and adapters already support `channel='sms'`. Enabling texts is just: complete your company's **A2P 10DLC registration**, fill the Twilio values into `DATA_DIR/notify.env` (or the service env), and flip `notify_sms` on for the right people. Nothing structural changes — it's built for from day one but stays dark until the carrier registration is genuinely in place. `sms_configured()` keeps the worker from attempting Twilio sends before then (guard the SMS branch on it if you want enqueued SMS rows to wait rather than fail).

---

## Setup gotchas worth knowing up front

- **Postmark sender verification.** Postmark won't send from an address until you've verified the sender signature or, better, the sending domain (DKIM). Do this in the Postmark dashboard before testing.
- **Cisco Umbrella.** The floor PC reaches the internet through Umbrella's DNS filtering. `api.postmarkapp.com` and `api.twilio.com` are unlikely to be blocked, but a send that fails with a connection/DNS error points there first — IT can allowlist the domain.
- **Phone format.** Twilio requires E.164 (`+1` then 10 digits). Normalize on the admin form so `(414) 555-1234` doesn't silently fail later.
- **Timestamps.** Use `datetime('now','localtime')` in SQL defaults/updates to stay consistent with the app's plant-wall-clock convention (`config.TS_FORMAT`), not bare `datetime('now')` (UTC).
- **One process, one worker.** Same rule as the heartbeat. waitress runs a single process here; keep it that way or the `status='sending'` claim is your only guard.
- **Wifi drops are now harmless.** Logging a delay only writes local DB rows; the worker retries with backoff. A dropped connection means an alert goes out a few seconds late, never never.

---

## Build order (Phase 1 checklist)

1. `baytracker/db.py` — add `recipients` + `notification_outbox` + index to `SCHEMA_SQL`; bump `SCHEMA_VERSION` to 2.
2. `migrate.py` — add `_add_notifications` migration; set `MIGRATIONS = [_add_notifications]`.
3. `baytracker/notify_config.py` — new (env + optional `DATA_DIR/notify.env`).
4. `baytracker/notify.py` — new (`enqueue_notifications`, `_recipient_matches`, `send_email_postmark`, `send_sms_twilio`, `process_outbox_once`, `start_outbox_worker`).
5. `app.py` — import `notify`/`notify_config`; one-line enqueue hook in `api_action`; `notify.start_outbox_worker()` in `create_app`; extend `admin_data`; add `admin_recipient` + `admin_recipient_test` routes.
6. `templates/admin.html` + `static/js/admin.js` — recipients table, scopes, test button, failure banner.
7. `requirements.txt` — add `requests`, `python-dotenv` (exact-pinned).
8. `.gitignore` — add `*.env` / `notify.env` (belt-and-suspenders).
9. Verify: run locally, add a recipient with your email, flag a test delay, confirm an outbox row appears and the worker flips it to `sent`; use "Send test" to confirm Postmark before trusting a live delay.
