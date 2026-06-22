"""
app.py -- the Flask application, exposed to the WSGI server as ``app:app``.

This is the glue layer. It:
  * serves the four pages (dashboard / console / stats / admin),
  * exposes a small JSON API the pages call,
  * streams live updates over Server-Sent Events (with a heartbeat so dashboards
    stay fresh and can detect a dropped connection),
  * gates /stats and /admin behind a PIN,
  * and answers /healthz so update.ps1 can confirm a deploy and roll back.

All the real logic lives in the baytracker package; the routes are deliberately
thin. Keep it that way.
"""

import queue
import threading
import time

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from io import BytesIO

from baytracker import (__version__, actions, auth, bootstrap, config, db,
                        events, exports, metrics, notify, notify_config, state)
from baytracker.app_db import close_db, get_db
from baytracker.actions import ActionError
from baytracker.sse import broker

# How often the server pushes a fresh snapshot to every connected browser. Small
# enough that elapsed times stay accurate and a frozen TV is noticed quickly;
# the data is tiny so this costs almost nothing.
HEARTBEAT_SECONDS = 5


def create_app() -> Flask:
    # Make startup self-sufficient: create the data folder, the database, the
    # schema and the structural seed rows if any of them are missing. Every
    # step is idempotent and non-destructive (same code path as init_db.py),
    # so a populated database is never touched -- but a fresh PC, a deleted
    # data folder, or a skipped init_db.py no longer produce a server that
    # answers 500 on every page.
    config.ensure_data_dirs()
    _conn = db.connect()
    try:
        db.create_schema(_conn)
        bootstrap.seed(_conn)
    finally:
        _conn.close()
    # Say where the data lives, loudly, so a wrong BAYTRACKER_DATA is obvious
    # in the console / service log instead of silently using the wrong folder.
    print(f"[baytracker] v{__version__}  data folder: {config.DATA_DIR}  "
          f"database: {config.DB_PATH}", flush=True)

    app = Flask(__name__)
    app.secret_key = config.get_secret_key()
    app.config["JSON_SORT_KEYS"] = False
    # Keep the PIN "unlock" in the session for a working day.
    app.permanent_session_lifetime = 60 * 60 * 12

    app.teardown_appcontext(close_db)

    # ---------------------------------------------------------------
    # Pages
    # ---------------------------------------------------------------
    @app.route("/")
    def index():
        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    def dashboard():
        # ?division=<name> turns a TV into a division-filtered screen (it only
        # takes over full-screen for its own division's delays).
        return render_template("dashboard.html",
                               division=request.args.get("division", ""),
                               version=__version__)

    @app.route("/console")
    def console():
        return render_template("console.html", version=__version__)

    @app.route("/stats")
    def stats():
        conn = get_db()
        if not auth.is_unlocked(conn, "stats"):
            return render_template("unlock.html", area="stats", version=__version__)
        return render_template("stats.html",
                               pin_set=bool(db.get_setting(conn, "pin_stats_hash", None)),
                               version=__version__)

    @app.route("/admin")
    def admin():
        conn = get_db()
        if not auth.is_unlocked(conn, "admin"):
            return render_template("unlock.html", area="admin", version=__version__)
        return render_template("admin.html",
                               pin_set=bool(db.get_setting(conn, "pin_admin_hash", None)),
                               version=__version__)

    # ---------------------------------------------------------------
    # Health check (Appendix B5)
    # ---------------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        # Touch the DB so the check fails if the data folder is unreachable.
        try:
            get_db().execute("SELECT 1;").fetchone()
        except Exception as exc:  # pragma: no cover - defensive
            return jsonify({"status": "error", "error": str(exc)}), 500
        return jsonify({"status": "ok", "version": __version__})

    # ---------------------------------------------------------------
    # Live state: SSE stream + polling fallback
    # ---------------------------------------------------------------
    @app.route("/events")
    def sse_events():
        def stream():
            q = broker.subscribe()
            try:
                # Paint immediately with a current snapshot, then follow updates.
                conn = db.connect()
                try:
                    yield broker.format_sse({"type": "state",
                                             "data": state.live_snapshot(conn)})
                finally:
                    conn.close()
                while True:
                    try:
                        payload = q.get(timeout=20)
                        yield broker.format_sse(payload)
                    except queue.Empty:
                        yield ": keep-alive\n\n"  # comment line keeps the socket open
            finally:
                broker.unsubscribe(q)

        resp = Response(stream(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"   # don't let any proxy buffer SSE
        # NOTE: do NOT set a "Connection" header here. It is hop-by-hop, which
        # PEP 3333 forbids a WSGI app from setting -- waitress aborts the whole
        # response with an AssertionError, killing the SSE stream. HTTP/1.1
        # connections are keep-alive by default anyway.
        return resp

    @app.route("/api/state")
    def api_state():
        return jsonify(state.live_snapshot(get_db()))

    @app.route("/api/config")
    def api_config():
        return jsonify(_public_config(get_db()))

    # ---------------------------------------------------------------
    # Actions (console). One endpoint dispatches to actions.py.
    # ---------------------------------------------------------------
    @app.route("/api/action", methods=["POST"])
    def api_action():
        conn = get_db()
        p = request.get_json(force=True, silent=True) or {}
        name = p.get("action")
        try:
            row = _dispatch_action(conn, name, p)
        except ActionError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except (KeyError, ValueError, TypeError):
            return jsonify({"ok": False, "error": "Missing or invalid fields."}), 400

        _publish_state(conn)
        if row is not None and row["type"] == "DELAY_START":
            _publish_delay_takeover(conn, row)
            # Write delay-alert intent to the local outbox (no network here -- a
            # background worker sends and retries). Guarded so a notification
            # hiccup can never fail the action that already logged the delay.
            try:
                notify.enqueue_notifications(conn, row)
            except Exception:
                pass
        return jsonify({"ok": True})

    # ---------------------------------------------------------------
    # Corrections (stats, PIN-gated)
    # ---------------------------------------------------------------
    @app.route("/api/correct", methods=["POST"])
    @auth.require_area("stats")
    def api_correct():
        conn = get_db()
        p = request.get_json(force=True, silent=True) or {}
        kind = p.get("kind")
        try:
            if kind == "event_time":
                actions.correct_event_time(conn, int(p["event_id"]), p["new_ts"],
                                           p.get("initials"), p.get("note"))
            elif kind == "close_delay":
                actions.close_open_delay(conn, int(p["bay_id"]), p["ended_at"],
                                         p.get("initials"), p.get("note"))
            elif kind == "close_run":
                actions.close_open_run(conn, int(p["bay_id"]), p["ended_at"],
                                       p.get("initials"), bool(p.get("terminal")),
                                       p.get("note"))
            else:
                return jsonify({"ok": False, "error": "Unknown correction."}), 400
        except ActionError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _publish_state(conn)
        return jsonify({"ok": True})

    # ---------------------------------------------------------------
    # Stats data + exports (PIN-gated)
    # ---------------------------------------------------------------
    @app.route("/api/stats")
    @auth.require_area("stats")
    def api_stats():
        return jsonify(metrics.compute(get_db(), _filters_from_query()))

    @app.route("/api/open_recent")
    @auth.require_area("stats")
    def api_open_recent():
        return jsonify(metrics.open_and_recent(get_db()))

    @app.route("/export.xlsx")
    @auth.require_area("stats")
    def export_xlsx():
        conn = get_db()
        filters = _filters_from_query()
        data = exports.to_xlsx(conn, filters)
        fname = f"bay_tracking_{exports.filename_suffix(filters)}.xlsx"
        return send_file(BytesIO(data), as_attachment=True, download_name=fname,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/export.zip")
    @auth.require_area("stats")
    def export_zip():
        conn = get_db()
        filters = _filters_from_query()
        data = exports.to_csv_zip(conn, filters)
        fname = f"bay_tracking_csv_{exports.filename_suffix(filters)}.zip"
        return send_file(BytesIO(data), as_attachment=True, download_name=fname,
                         mimetype="application/zip")

    # ---------------------------------------------------------------
    # Unlock (PIN entry)
    # ---------------------------------------------------------------
    @app.route("/unlock", methods=["POST"])
    def unlock():
        conn = get_db()
        p = request.get_json(force=True, silent=True) or {}
        area = p.get("area")
        if area not in ("stats", "admin"):
            return jsonify({"ok": False, "error": "Unknown area."}), 400
        if auth.verify_pin(conn, area, p.get("pin", "")):
            auth.unlock(area)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Incorrect PIN."}), 403

    @app.route("/lock", methods=["POST"])
    def lock():
        session.clear()
        return jsonify({"ok": True})

    # ---------------------------------------------------------------
    # Admin API (PIN-gated). Config is entered here; nothing is fabricated.
    # ---------------------------------------------------------------
    _register_admin_routes(app)

    # ---------------------------------------------------------------
    # Background heartbeat: push a fresh snapshot to all clients on a timer.
    # ---------------------------------------------------------------
    _start_heartbeat()

    # Background outbox worker: send queued delay notifications and retry
    # failures with backoff (same daemon-thread pattern as the heartbeat).
    notify.start_outbox_worker()

    return app


# ===================================================================
# Helpers (module-level so the heartbeat thread can use them too)
# ===================================================================

def _dispatch_action(conn, name, p):
    """Route an action name to the right actions.py function. Returns the event row."""
    if name == "start":
        return actions.start(conn, int(p["bay_id"]), p.get("work_order"),
                             p.get("product_number"), p.get("initials"),
                             p.get("component_label"))
    if name == "move":
        return actions.move(conn, int(p["bay_id"]), int(p["target_bay_id"]),
                            p.get("initials"))
    if name == "complete_bay":
        return actions.complete_bay(conn, int(p["bay_id"]), p.get("initials"))
    if name == "mate":
        return actions.mate(conn, int(p["keep_bay_id"]), int(p["release_bay_id"]),
                            p.get("initials"))
    if name == "flag_delay":
        return actions.flag_delay(conn, int(p["bay_id"]), int(p["reason_id"]),
                                  p.get("note"), p.get("initials"))
    if name == "clear_delay":
        return actions.clear_delay(conn, int(p["bay_id"]), p.get("initials"))
    if name == "pause_bay":
        return actions.pause_bay(conn, int(p["bay_id"]), p.get("initials"))
    if name == "resume_bay":
        return actions.resume_bay(conn, int(p["bay_id"]), p.get("initials"))
    if name == "shift_changeover":
        return actions.shift_changeover(conn, p.get("pause"), p.get("resume"),
                                        p.get("initials"))
    if name == "unit_complete":
        return actions.unit_complete(conn, p.get("work_order"), p.get("initials"))
    raise ActionError("Unknown action.")


def _public_config(conn) -> dict:
    """Config the console/dashboard need (no cost data, no PIN hashes)."""
    reasons = conn.execute(
        "SELECT r.id, r.label, r.in_out_of_control, r.is_other, r.sort_order, "
        "       d.name AS division "
        "FROM delay_reasons r LEFT JOIN divisions d ON r.division_id = d.id "
        "WHERE r.active = 1 "
        "ORDER BY r.is_other ASC, r.sort_order ASC, r.label ASC;"
    ).fetchall()
    products = conn.execute(
        "SELECT number, description FROM product_numbers WHERE active = 1 ORDER BY number;"
    ).fetchall()
    roster = conn.execute(
        "SELECT initials FROM initials_roster WHERE active = 1 ORDER BY initials;"
    ).fetchall()
    bays = conn.execute(
        "SELECT id, name, sort_order, is_extra, grid_col FROM bays "
        "WHERE active = 1 ORDER BY is_extra ASC, sort_order ASC;"
    ).fetchall()
    divisions = conn.execute(
        "SELECT name FROM divisions WHERE active = 1 ORDER BY name;").fetchall()
    return {
        "reasons": [dict(r) for r in reasons],
        "products": [dict(r) for r in products],
        "initials": [r["initials"] for r in roster],
        "bays": [dict(b) for b in bays],
        "divisions": [d["name"] for d in divisions],
        # Shift cutoffs (names + start times) are not sensitive; the Stats page
        # uses them for the shift filter and the "this shift" date preset.
        "shifts": db.get_setting(conn, "shifts", []),
        "layout": {
            "grid_cols": db.get_setting(conn, "grid_cols", 4),
            "standard_rows": db.get_setting(conn, "standard_rows", 3),
            "extras_enabled": db.get_setting(conn, "extras_enabled", False),
        },
        "takeover_seconds": db.get_setting(conn, "takeover_seconds", 12),
    }


def _filters_from_query() -> dict:
    """Build an export/stats filter dict from query-string params."""
    if request.args.get("everything") in ("1", "true", "yes"):
        return {}
    keys = ["start", "end", "bay_id", "reason", "division", "product_number", "shift"]
    return {k: request.args.get(k) for k in keys if request.args.get(k)}


def _normalize_phone(raw):
    """Coerce a typed phone to E.164 ('+1' + 10 digits) so Twilio doesn't fail
    silently later. Returns None for blank input. Best-effort: a '+'-prefixed
    international number is kept as-is (digits only); a bare US 10-digit number
    gets '+1'; '1XXXXXXXXXX' gets a leading '+'."""
    s = (raw or "").strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def _clean_bay_scope(value):
    """Normalize a recipient bay scope to 'all' or a CSV of integer bay ids.
    Accepts 'all', a list of ids (from the admin checklist), or a CSV string."""
    if value in (None, "", "all"):
        return "all"
    if isinstance(value, (list, tuple)):
        ids = [str(int(x)) for x in value if str(x).strip()]
    else:
        ids = [s.strip() for s in str(value).split(",") if s.strip().isdigit()]
    return ",".join(ids) if ids else "all"


def _publish_state(conn):
    """Build a fresh snapshot and push it to every connected browser."""
    broker.publish("state", state.live_snapshot(conn))


def _publish_delay_takeover(conn, row):
    """Emit the full-screen takeover payload for a freshly-flagged delay."""
    bay = conn.execute("SELECT name FROM bays WHERE id = ?;", (row["bay_id"],)).fetchone()
    broker.publish("delay", {
        "bay_id": row["bay_id"],
        "bay": bay["name"] if bay else f"Bay {row['bay_id']}",
        "work_order": row["work_order"],
        "product_number": row["product_number"],
        "reason": row["reason_label"],
        "division": row["division"],
        "in_out_of_control": row["in_out_of_control"],
        "note": row["note"],
        "flagged_by": row["initials"],
        "seconds": db.get_setting(conn, "takeover_seconds", 12),
    })


# ---- background heartbeat -------------------------------------------------
_heartbeat_started = False
_heartbeat_lock = threading.Lock()


def _start_heartbeat():
    global _heartbeat_started
    with _heartbeat_lock:
        if _heartbeat_started:
            return
        _heartbeat_started = True

    def loop():
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            try:
                conn = db.connect()
                try:
                    broker.publish("state", state.live_snapshot(conn))
                finally:
                    conn.close()
            except Exception:
                # Never let a transient error kill the heartbeat thread.
                pass

    t = threading.Thread(target=loop, name="bt-heartbeat", daemon=True)
    t.start()


# ===================================================================
# Admin routes (kept in their own function to keep create_app readable)
# ===================================================================

def _register_admin_routes(app):

    @app.route("/api/admin/data")
    @auth.require_area("admin")
    def admin_data():
        conn = get_db()
        divisions = [dict(r) for r in conn.execute(
            "SELECT * FROM divisions ORDER BY active DESC, name;").fetchall()]
        reasons = [dict(r) for r in conn.execute(
            "SELECT r.*, d.name AS division_name FROM delay_reasons r "
            "LEFT JOIN divisions d ON r.division_id = d.id "
            "ORDER BY r.is_other ASC, r.active DESC, r.sort_order, r.label;").fetchall()]
        products = [dict(r) for r in conn.execute(
            "SELECT * FROM product_numbers ORDER BY active DESC, number;").fetchall()]
        roster = [dict(r) for r in conn.execute(
            "SELECT * FROM initials_roster ORDER BY active DESC, initials;").fetchall()]
        bays = [dict(r) for r in conn.execute(
            "SELECT * FROM bays ORDER BY is_extra, sort_order;").fetchall()]
        recipients = [dict(r) for r in conn.execute(
            "SELECT * FROM recipients ORDER BY active DESC, name;").fetchall()]
        return jsonify({
            "divisions": divisions,
            "reasons": reasons,
            "products": products,
            "initials": roster,
            "bays": bays,
            "recipients": recipients,
            "notify": {
                "email_configured": notify_config.email_configured(),
                "sms_configured": notify_config.sms_configured(),
                "failures": notify.recent_failures(conn),
            },
            "settings": {
                "takeover_seconds": db.get_setting(conn, "takeover_seconds", 12),
                "stale_delay_minutes": db.get_setting(conn, "stale_delay_minutes", 120),
                "stale_run_minutes": db.get_setting(conn, "stale_run_minutes", 720),
                "labor_rate": db.get_setting(conn, "labor_rate", None),
                "backup_network_path": db.get_setting(conn, "backup_network_path", None),
                "grid_cols": db.get_setting(conn, "grid_cols", 4),
                "standard_rows": db.get_setting(conn, "standard_rows", 3),
                "extras_enabled": db.get_setting(conn, "extras_enabled", False),
            },
            "schedule": {
                "break_schedule": db.get_setting(conn, "break_schedule", []),
                "shifts": db.get_setting(conn, "shifts", []),
                "operating_calendar": db.get_setting(conn, "operating_calendar", None),
            },
            "pins": {
                "stats_set": bool(db.get_setting(conn, "pin_stats_hash", None)),
                "admin_set": bool(db.get_setting(conn, "pin_admin_hash", None)),
            },
        })

    def _body():
        return request.get_json(force=True, silent=True) or {}

    @app.route("/api/admin/division", methods=["POST"])
    @auth.require_area("admin")
    def admin_division():
        conn = get_db()
        p = _body()
        op = p.get("op")
        if op == "add":
            name = (p.get("name") or "").strip()
            if not name:
                return jsonify({"ok": False, "error": "Name required."}), 400
            conn.execute("INSERT OR IGNORE INTO divisions (name, active) VALUES (?, 1);", (name,))
        elif op == "update":
            conn.execute("UPDATE divisions SET name = ? WHERE id = ?;",
                         ((p.get("name") or "").strip(), p["id"]))
        elif op == "delete":
            # Hard delete. Safe for history: delay events snapshot the division
            # NAME onto themselves, so removing the config row never rewrites a
            # past record. Guard only against orphaning a *reason* that still
            # points here, which would silently blank that reason's division on
            # future delays.
            used = conn.execute("SELECT COUNT(*) AS n FROM delay_reasons WHERE division_id = ?;",
                                (p["id"],)).fetchone()["n"]
            if used:
                return jsonify({"ok": False,
                                "error": f"{used} delay reason(s) still use this division — "
                                         "delete or reassign them first."}), 400
            conn.execute("DELETE FROM divisions WHERE id = ?;", (p["id"],))
        conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/admin/reason", methods=["POST"])
    @auth.require_area("admin")
    def admin_reason():
        conn = get_db()
        p = _body()
        op = p.get("op")
        if op == "add":
            label = (p.get("label") or "").strip()
            if not label:
                return jsonify({"ok": False, "error": "Label required."}), 400
            nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n "
                               "FROM delay_reasons WHERE is_other = 0;").fetchone()["n"]
            conn.execute(
                "INSERT INTO delay_reasons (label, division_id, in_out_of_control, "
                "active, is_other, sort_order) VALUES (?, ?, ?, 1, 0, ?);",
                (label, p.get("division_id") or None,
                 p.get("in_out_of_control") or None, nxt))
        elif op == "update":
            # The mandatory "Other" row's label/flags are fixed; guard it.
            row = conn.execute("SELECT is_other FROM delay_reasons WHERE id = ?;",
                               (p["id"],)).fetchone()
            if row and row["is_other"]:
                return jsonify({"ok": False, "error": "The 'Other' reason can't be edited."}), 400
            conn.execute(
                "UPDATE delay_reasons SET label = ?, division_id = ?, "
                "in_out_of_control = ? WHERE id = ?;",
                ((p.get("label") or "").strip(), p.get("division_id") or None,
                 p.get("in_out_of_control") or None, p["id"]))
        elif op == "delete":
            # Hard delete. Safe for history: delay events snapshot the reason
            # label/division/control tag, so deleting the config row leaves
            # every past delay intact. The mandatory pinned "Other" stays.
            row = conn.execute("SELECT is_other FROM delay_reasons WHERE id = ?;",
                               (p["id"],)).fetchone()
            if row and row["is_other"]:
                return jsonify({"ok": False, "error": "The 'Other' reason can't be deleted."}), 400
            conn.execute("DELETE FROM delay_reasons WHERE id = ?;", (p["id"],))
        conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/admin/product", methods=["POST"])
    @auth.require_area("admin")
    def admin_product():
        conn = get_db()
        p = _body()
        op = p.get("op")
        if op == "add":
            number = (p.get("number") or "").strip()
            if not number:
                return jsonify({"ok": False, "error": "Product number required."}), 400
            conn.execute(
                "INSERT OR IGNORE INTO product_numbers (number, description, target_minutes, active) "
                "VALUES (?, ?, ?, 1);",
                (number, (p.get("description") or "").strip() or None,
                 p.get("target_minutes")))
        elif op == "update":
            conn.execute(
                "UPDATE product_numbers SET number = ?, description = ?, target_minutes = ? WHERE id = ?;",
                ((p.get("number") or "").strip(),
                 (p.get("description") or "").strip() or None,
                 p.get("target_minutes"), p["id"]))
        elif op == "delete":
            # Hard delete. Safe for history: events store the product number as
            # text, not a reference to this list, so removing it changes nothing
            # already logged.
            conn.execute("DELETE FROM product_numbers WHERE id = ?;", (p["id"],))
        conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/admin/initials", methods=["POST"])
    @auth.require_area("admin")
    def admin_initials():
        conn = get_db()
        p = _body()
        op = p.get("op")
        if op == "add":
            ini = (p.get("initials") or "").strip()
            if not ini:
                return jsonify({"ok": False, "error": "Initials required."}), 400
            conn.execute("INSERT OR IGNORE INTO initials_roster (initials, name, active) "
                         "VALUES (?, ?, 1);", (ini, (p.get("name") or "").strip() or None))
        elif op == "update":
            conn.execute("UPDATE initials_roster SET initials = ?, name = ? WHERE id = ?;",
                         ((p.get("initials") or "").strip(),
                          (p.get("name") or "").strip() or None, p["id"]))
        elif op == "delete":
            # Hard delete, by design: the roster is just an autocomplete list,
            # and events snapshot the initials TEXT, so removing a roster row
            # can never orphan or rewrite history. (Everything else in admin is
            # soft-retired because reasons/divisions are referenced by id.)
            conn.execute("DELETE FROM initials_roster WHERE id = ?;", (p["id"],))
        conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/admin/bay", methods=["POST"])
    @auth.require_area("admin")
    def admin_bay():
        conn = get_db()
        p = _body()
        op = p.get("op")
        if op == "add_extra":
            nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM bays;").fetchone()["n"]
            name = (p.get("name") or f"Bay {nxt}").strip()
            conn.execute("INSERT INTO bays (name, sort_order, is_extra, grid_col, active) "
                         "VALUES (?, ?, 1, ?, 1);", (name, nxt, p.get("grid_col")))
        elif op == "rename":
            conn.execute("UPDATE bays SET name = ? WHERE id = ?;",
                         ((p.get("name") or "").strip(), p["id"]))
        elif op == "set_col":
            conn.execute("UPDATE bays SET grid_col = ? WHERE id = ?;",
                         (p.get("grid_col"), p["id"]))
        elif op in ("retire", "activate"):
            if op == "retire":
                # Don't hide a bay that is currently occupied.
                r = state.replay(conn)
                if p["id"] in r.bay_current:
                    return jsonify({"ok": False, "error": "That bay is in use right now."}), 400
            conn.execute("UPDATE bays SET active = ? WHERE id = ?;",
                         (0 if op == "retire" else 1, p["id"]))
        conn.commit()
        state.invalidate_cache()
        _publish_state(conn)
        return jsonify({"ok": True})

    @app.route("/api/admin/layout", methods=["POST"])
    @auth.require_area("admin")
    def admin_layout():
        conn = get_db()
        p = _body()
        for key in ("grid_cols", "standard_rows", "extras_enabled"):
            if key in p:
                db.set_setting(conn, key, p[key])
        _publish_state(conn)
        return jsonify({"ok": True})

    @app.route("/api/admin/schedule", methods=["POST"])
    @auth.require_area("admin")
    def admin_schedule():
        conn = get_db()
        p = _body()
        for key in ("break_schedule", "shifts", "operating_calendar"):
            if key in p:
                db.set_setting(conn, key, p[key])
        state.invalidate_cache()  # schedule affects every computed duration
        _publish_state(conn)
        return jsonify({"ok": True})

    @app.route("/api/admin/settings", methods=["POST"])
    @auth.require_area("admin")
    def admin_settings():
        conn = get_db()
        p = _body()
        for key in ("takeover_seconds", "stale_delay_minutes", "stale_run_minutes",
                    "labor_rate", "backup_network_path"):
            if key in p:
                db.set_setting(conn, key, p[key])
        return jsonify({"ok": True})

    @app.route("/api/admin/pin", methods=["POST"])
    @auth.require_area("admin")
    def admin_pin():
        conn = get_db()
        p = _body()
        area = p.get("area")
        if area not in ("stats", "admin"):
            return jsonify({"ok": False, "error": "Unknown area."}), 400
        auth.set_pin(conn, area, (p.get("pin") or "").strip())
        return jsonify({"ok": True})

    # --- Delay-notification recipients -------------------------------------
    @app.route("/api/admin/recipient", methods=["POST"])
    @auth.require_area("admin")
    def admin_recipient():
        conn = get_db()
        p = _body()
        op = p.get("op")
        if op == "add":
            name = (p.get("name") or "").strip()
            if not name:
                return jsonify({"ok": False, "error": "Name required."}), 400
            conn.execute(
                "INSERT INTO recipients (name, email, phone, notify_email, notify_sms, "
                "bay_scope, control_scope, active) VALUES (?, ?, ?, ?, ?, ?, ?, 1);",
                (name, (p.get("email") or "").strip() or None,
                 _normalize_phone(p.get("phone")),
                 1 if p.get("notify_email") else 0,
                 1 if p.get("notify_sms") else 0,
                 _clean_bay_scope(p.get("bay_scope")),
                 "out" if p.get("control_scope") == "out" else "all"))
        elif op == "update":
            conn.execute(
                "UPDATE recipients SET name = ?, email = ?, phone = ?, notify_email = ?, "
                "notify_sms = ?, bay_scope = ?, control_scope = ? WHERE id = ?;",
                ((p.get("name") or "").strip(),
                 (p.get("email") or "").strip() or None,
                 _normalize_phone(p.get("phone")),
                 1 if p.get("notify_email") else 0,
                 1 if p.get("notify_sms") else 0,
                 _clean_bay_scope(p.get("bay_scope")),
                 "out" if p.get("control_scope") == "out" else "all",
                 p["id"]))
        elif op in ("retire", "activate"):
            # Soft-retire (active=0), never hard-delete, so the outbox audit trail
            # can always be traced back to a recipient row.
            conn.execute("UPDATE recipients SET active = ? WHERE id = ?;",
                         (1 if op == "activate" else 0, p["id"]))
        else:
            return jsonify({"ok": False, "error": "Unknown op."}), 400
        conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/admin/recipient_test", methods=["POST"])
    @auth.require_area("admin")
    def admin_recipient_test():
        # Send directly through the adapters (bypassing the outbox) so config can
        # be confirmed before waiting for a real delay. Returns JSON (this admin
        # is a JSON-driven page; it never full-page-navigates).
        conn = get_db()
        p = _body()
        r = conn.execute("SELECT * FROM recipients WHERE id = ?;", (p.get("id"),)).fetchone()
        if r is None:
            return jsonify({"ok": False, "error": "No such recipient."}), 404
        sent = []
        try:
            if r["notify_email"] and r["email"]:
                notify.send_email_postmark(r["email"], "BayTracker test",
                                           "Test alert — config OK.")
                sent.append("email")
            if r["notify_sms"] and r["phone"]:
                notify.send_sms_twilio(r["phone"], "BayTracker test — config OK.")
                sent.append("sms")
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        if not sent:
            return jsonify({"ok": False,
                            "error": "No enabled channel with an address to test."}), 400
        return jsonify({"ok": True, "sent": sent})


# The WSGI entry point: waitress-serve ... app:app
app = create_app()


if __name__ == "__main__":
    # Convenience for local development only. Production uses waitress (see
    # setup.ps1 / README). threaded=True so SSE streams don't block other requests.
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
