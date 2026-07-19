# AGENTS.md — Bay Tracking & Logging System

Guidance for AI coding agents working in this repository. Read this before making
any change; the project has a few hard rules that are load-bearing.

## Project overview

A self-contained web app ("BayTracker") that tracks work in production **bays** on a
manufacturing plant floor. It shows live status on TVs around the plant, lets
technicians log activity from one central PC, and keeps a complete, exportable
history for analysis. It runs entirely on a private plant LAN — **no internet
required at runtime** — on a single Windows PC.

- **Backend:** Python 3.14 + Flask, served in production by **waitress** (pure-Python
  WSGI server). **Database:** a single SQLite file in WAL mode.
- **Live updates:** Server-Sent Events (`/events`) with a 5-second server heartbeat,
  plus a client-side polling fallback and auto-reconnect.
- **Frontend:** plain HTML/CSS/JS — no framework, no build step. Jinja2 templates in
  `templates/`, scripts in `static/js/`, one vendored lib (`static/vendor/chart.umd.min.js`).
  It must render on cheap TV / streaming-stick browsers.
- **Notifications:** email (Postmark) and SMS (Twilio) alerts for bay delays and EHS
  incidents, sent by a background outbox worker with retry/backoff.

Current version is `__version__` in `baytracker/__init__.py` (single source of truth;
`/healthz` and page footers report it). Two design specs live in the repo root:
`bay_tracking_system_spec.md` and `baytracker-notifications-spec.md` — code comments
refer to them as "Appendix B2", "spec section 4", etc.

## The rules that must never be broken

1. **Code and data live in separate folders.** The database lives in the folder named
   by the `BAYTRACKER_DATA` environment variable (default `C:\BayTrackerData`),
   **outside this repository**. Git operations can therefore never touch the
   accumulated log. Never write code that stores data inside the repo, and never point
   `BAYTRACKER_DATA` at a cloud-synced folder (OneDrive/Dropbox corrupt live SQLite).
2. **The `events` table is append-only and is the single source of truth.** Never
   UPDATE or DELETE an event row. History is corrected by appending `CORRECTION`
   events and undone by appending `VOID` events. All current state is *derived* by
   replaying the log (`baytracker/state.py`), so a reboot never loses anything.
3. **No fabricated data (spec Appendix C2).** Never invent, estimate, randomize,
   interpolate, or default-fill a value. Open records stay open (blank end times /
   durations in exports). Stats show "no data" where there is none. Cost appears only
   when a real labor rate was entered. The only auto-created rows are structural bay
   slots and the mandatory "Other" delay reason (`baytracker/bootstrap.py`).
4. **All timestamps are generated server-side** (never trusted from a client) and
   stored as local plant wall-clock time, ISO 8601 to the second
   (`config.TS_FORMAT = "%Y-%m-%d %H:%M:%S"`).
5. **The web request never touches the network for notifications.** Logging a delay
   or incident only writes `pending` rows to `notification_outbox`; a daemon thread
   (`notify.start_outbox_worker`) sends and retries. Keep it that way.
6. **Migrations are additive-only and idempotent** (`migrate.py`): add tables/columns,
   never drop, rename, or rewrite existing data.

## Repository layout

```
app.py            WSGI app (app:app): thin routes, SSE, JSON API, PIN gate, /healthz.
                  All real logic lives in the baytracker package — keep routes thin.
baytracker/       Application package (all the logic):
  __init__.py     __version__ — the single version source of truth
  config.py       Data-dir resolution (BAYTRACKER_DATA), secret key, TS_FORMAT
  db.py           SQLite connection helpers + the COMPLETE schema (CREATE IF NOT EXISTS)
  bootstrap.py    The ONLY place rows are seeded on a fresh DB (bays, "Other" reason)
  schedule.py     The time engine: breaks, off-hours, shifts, non-counting time
  events.py       Append/read the event log; the event type vocabulary
  state.py        Replays the log into current state + live_snapshot() for the UI
  actions.py      The ONLY place a button-press becomes an event (validates first)
  exports.py      Derives the 4 export tables (Delays / Bay Runs / Unit Journeys / raw)
  metrics.py      Stats-page numbers, built from exports.derive_rows()
  incidents.py    EHS accident / near-miss log + leadership alerts
  notify.py       Notification outbox: enqueue, background send, retry with backoff
  notify_config.py Postmark/Twilio credentials from env or DATA_DIR/notify.env
  sse.py          Tiny in-process pub/sub broker for Server-Sent Events
  auth.py         PIN gate for /stats and /admin (session cookie + werkzeug hash)
  app_db.py       Per-request SQLite connection on Flask's g
templates/        dashboard / console / stats / admin / incident / unlock / base
static/           css/app.css, js/{common,dashboard,console,stats,admin,incident}.js,
                  vendor/chart.umd.min.js (vendored — offline; do not add CDN links)
tests/            Standalone test scripts (see Testing)
init_db.py        Create + seed the DB (non-destructive, idempotent)
migrate.py        Forward-only, additive, idempotent schema migrations
make_demo_data.py Builds the SEPARATE disposable demo DB (start.ps1 -Demo)
backup_db.py      Consistent online (WAL-aware) DB backup
setup.ps1         One-time install (venv, pinned deps, BAYTRACKER_DATA, DB seed,
                  shortcuts; -Offline installs from wheelhouse/, -InstallService,
                  -OpenFirewall, -ScheduleBackup)
start.ps1         THE way to launch by hand (right venv, right data dir, port checks)
update.ps1        Safe update by git tag: backup → checkout → deps → migrate →
                  restart → health-check → auto-rollback
update_latest.ps1 One-click update to the newest GitHub release tag
boot.ps1          Desktop-icon target: start service or foreground launch
backup.ps1        Scheduled-backup wrapper around backup_db.py
make_shortcut.ps1 Creates the labelled Desktop launchers (Start / Update)
requirements.txt  EXACTLY pinned dependencies (==), captured on Python 3.14
wheelhouse/       Vendored wheels for offline installs (Windows 64-bit / Py 3.14)
tools/            nssm.exe (service manager) goes here; see tools/README.txt
tools/check_invariants.py   mechanical check of the six load-bearing rules —
                  run it after any code revision and before every release
.claude/skills/   agent skills (procedures for AI agents and humans alike):
                  codebase-eval, baytracker-invariants, baytracker-health-audit,
                  baytracker-release, baytracker-restore-drill, longevity-review
.github/workflows/python-package.yml   CI (see Testing)
```

The four pages: `/dashboard` (read-only live grid for TVs; `?division=<name>` scopes
the full-screen delay takeover), `/console` (floor logging PC, public),
`/incident` (public EHS report page), `/stats` and `/admin` (PIN-gated). Health check:
`GET /healthz` → `200 {"status":"ok","version":...}`.

## Build, run, and test commands

All commands are PowerShell on Windows unless noted. Use the repo-local venv's Python
(`venv\Scripts\python.exe`) — never a global interpreter.

```powershell
# First-time install (creates venv, installs pinned deps, sets BAYTRACKER_DATA, seeds DB)
powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Offline / air-gapped install from vendored wheels:
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Offline

# Launch (production port 5000; -Force kills whatever holds the port first)
powershell -ExecutionPolicy Bypass -File .\start.ps1
# Manual equivalent (what start.ps1 runs). Threads note: every open dashboard/
# console tab holds ONE waitress thread for the life of its SSE stream -- keep
# the count comfortably above the number of screens that could be open at once.
venv\Scripts\python.exe -m waitress --listen=0.0.0.0:5000 --threads=64 app:app

# Local development — ALWAYS a dev port and a dev data folder, never 5000:
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Port 5001 -DataDir C:\BayTrackerData_dev

# Tests: each file in tests/ is a standalone script (one process per file):
venv\Scripts\python.exe tests\test_time_engine.py
venv\Scripts\python.exe tests\test_undo.py
venv\Scripts\python.exe tests\test_notify.py
venv\Scripts\python.exe tests\test_incidents.py
# `pytest tests` also works locally via tests/conftest.py, but the per-file
# scripts above are the canonical gate (CI runs them exactly this way).

# Lint (same selection as CI — hard errors only, style is not policed):
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

# Invariant checker (the six load-bearing rules; run before every release):
venv\Scripts\python.exe tools\check_invariants.py

# Demo mode (separate disposable demo database, DEMO DATA badge on every screen):
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Demo
```

## Testing strategy

- **No pytest dependency by design.** Each file in `tests/` is a self-contained script:
  it points `BAYTRACKER_DATA` at its own temp folder *before* importing the app, uses a
  unique throwaway SQLite file per test case, prints PASS/FAIL lines, and exits
  non-zero on any failure. Preserve this pattern when adding tests — do not convert
  them to pytest fixtures. (`tests/conftest.py` exists only so a bare `pytest tests`
  run also honors the scripts' `check()` failures.)
- **Why one process per file matters:** `baytracker.config.DATA_DIR` is resolved once
  at first import, so importing several test modules into one pytest process would make
  them share a database and trample each other. New test files must set
  `BAYTRACKER_DATA` and `sys.path` before any `baytracker` import (copy the header of
  an existing test).
- The time-accounting rules (breaks/off-hours are non-counting, union of parallel bay
  work, active/delay/queue/cycle partitioning) are the most subtle logic in the system.
  **Always run `tests/test_time_engine.py` after touching `schedule.py` or `state.py`.**
- CI (`.github/workflows/python-package.yml`) runs on push/PR to `main`: Python **3.14
  only**, on both ubuntu-latest and windows-latest; flake8 hard-error gate, then the
  four test scripts one process per file. Do not widen the Python matrix — the pinned
  requirements only install on 3.14.

## Code style and conventions

- **Keep it simple and heavily commented.** The README is explicit: this codebase is
  meant to be maintained by an engineering intern, not a dedicated dev team. Prefer
  plain, readable code over cleverness; explain *why* in comments (most modules open
  with a docstring describing the design — keep those docstrings accurate when you
  change behavior).
- **Thin routes.** `app.py` only dispatches; logic goes in the `baytracker` package.
- **Match the existing style:** 4-space indent, module docstrings, section-banner
  comments (`# ---...---`), double-quoted strings, snake_case. No formatter/linter
  config exists; CI only rejects syntax errors and undefined names.
- **Dependencies are exactly pinned** in `requirements.txt` and mirrored as wheels in
  `wheelhouse/` for offline installs. Do not float versions, and do not add a
  dependency without a strong reason — if you must, pin it exactly and refresh the
  wheelhouse (`py -3 -m pip download -r requirements.txt -d wheelhouse`).
- **Identifiers humans read are TEXT.** `work_order` and `product_number` are stored
  and exported as text so leading zeros survive (Excel exports force text cells).
- **Soft-retire, don't hard-delete** config rows that history may reference
  (`active=0` on delay reasons, recipients, bays). Delay events snapshot reason label /
  division / control tag onto themselves, so config edits never rewrite history.
- **Config edits vs. history:** admin endpoints for divisions/reasons/products/initials
  may hard-delete only because events snapshot everything they need; comments in
  `app.py` explain each case — follow the same reasoning before adding a delete.
- The console **Undo** reverses floor actions by appending `VOID` events grouped by
  `events.action_group`; only types in `state.UNDOABLE_TYPES` are undoable.
- Time durations everywhere flow through `Schedule.counted_seconds(t0, t1)` so every
  screen and export agrees. Never compute elapsed time with raw subtraction.
- Frontend is dependency-free vanilla JS that must run on old TV browsers — no
  transpile step, no npm, no modern-only syntax without a fallback.

## Security considerations

- The app runs on a **private LAN with no internet exposure**; the PIN gate
  (`baytracker/auth.py`) is intentionally modest — it keeps cost data off the shop
  floor and stops accidental edits, it is not a defense against a determined attacker.
  Do not present it as more than that.
- Two PINs exist (`stats` and `admin`; admin implies stats). PINs are stored hashed
  (werkzeug) in the `settings` table. An unset PIN means the area is **open** — pages
  show a "set a PIN" banner. Never seed a default PIN.
- **Secrets live outside the repo, always:** the Flask session key is generated into
  `DATA_DIR/secret_key`; Postmark/Twilio credentials come from environment variables
  or `DATA_DIR/notify.env`. Never commit credentials, and never read them with
  `os.environ[...]` (missing config must boot cleanly, just flagged as not configured).
- `/console` and `/incident` are deliberately public (the floor must reach them
  instantly). Only `/stats` and `/admin` routes use `@auth.require_area`.
- The database is never committed — `.gitignore` excludes `*.db`, WAL sidecars,
  backups, exports, and `venv/`. Keep it that way even when testing locally.
- SQL is parameterized throughout; keep it that way (the UI accepts free-text
  work orders, notes, and initials).

## Deployment and update process

- There is **one** GitHub remote and code only moves between machines through it.
  Never edit code directly on the floor PC and never hand-copy files between machines.
- **Releases are git tags** (e.g. `v1.6.1`). The floor PC runs a known tag, never bare
  `main`, and never auto-pulls. Bump `baytracker/__init__.py:__version__` with each
  release — `/healthz` and footers display it.
- Update on the floor PC with `update.ps1 -Tag vX.Y.Z` (or the "Update Bay Tracker"
  Desktop icon → `update_latest.ps1`): it backs up the DB, checks out the tag, installs
  pinned deps, runs `migrate.py`, restarts, health-checks `/healthz`, and **auto-rolls
  back** on failure.
- For unattended running, `setup.ps1 -InstallService` installs an NSSM Windows service
  (`tools/nssm.exe`); daily backups via `setup.ps1 -ScheduleBackup` → `backup.ps1`.
- Two operational foot-guns the tooling guards against — respect them in any change:
  dev servers must never use production port 5000 (a stale dev server silently keeps
  serving old code to every TV), and dev must use a separate data folder
  (`C:\BayTrackerData_dev`).
