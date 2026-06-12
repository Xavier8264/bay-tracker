# Bay Tracking & Logging System

A self-contained web app that tracks work in production **bays**, shows live status
on TVs around the plant, lets technicians log activity from one central PC, and keeps
a complete, exportable history for analysis.

- **Backend:** Python + Flask, served by **waitress**. **Database:** a single SQLite file.
- **Live updates:** Server-Sent Events (with polling fallback + auto-reconnect).
- **Frontend:** plain HTML/CSS/JS (renders on cheap TV / streaming-stick browsers).
- **No internet required.** Everything runs on the local network.

The source of truth is an **append-only event log**. All current state (what's in each
bay, elapsed times, delays) is *derived* by replaying events, so a reboot or crash never
loses anything. All timestamps are generated **server-side**.

---

## The one rule that protects your data

**Code and data live in separate folders.** The database is stored in the folder named by
the `BAYTRACKER_DATA` environment variable (default `C:\BayTrackerData`), which is **outside
this repository**. Git operations (clone/pull/checkout) only ever touch files inside the repo,
so updating the code physically cannot touch the accumulated log.

> ⚠️ **Do not put `BAYTRACKER_DATA` on OneDrive/Dropbox/Google Drive.** Cloud sync corrupts
> live SQLite files. Use a plain local path and back it up on a schedule (see *Backups*).

---

## Get it running (clone → install → launch on the network)

Do these steps on the **one PC that will be the server**. They put the app on a LAN
port that every other machine (TVs, the logging PC, your laptop) can reach in a browser.

**Prerequisites:** install **Python 3** (from [python.org](https://www.python.org/) — tick
**"Add Python to PATH"**) and **git**. Commands below are PowerShell on Windows.

### 1. Clone the repository

```powershell
# Clone to a plain local path (a OneDrive/Dropbox-synced folder is fine for the CODE,
# but the DATABASE must not live in one -- see step 2).
git clone <your-repo-url> C:\BayTracking
cd C:\BayTracking
```

### 2. Install the requirements

**Option A — one command (recommended).** `setup.ps1` creates a repo-local `venv`, installs
the exact pinned dependencies from `requirements.txt`, sets `BAYTRACKER_DATA`, and creates +
seeds the database. Safe to re-run.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

**Option B — do it by hand** (same result, explicit steps):

```powershell
python -m venv venv                                            # create the virtual environment
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt     # install all dependencies
$env:BAYTRACKER_DATA = "C:\BayTrackerData"                     # where the database lives (NOT on OneDrive)
venv\Scripts\python.exe init_db.py                             # create + seed the database
```

> The database is kept in `BAYTRACKER_DATA` (default `C:\BayTrackerData`), **outside the repo**,
> so updating the code can never touch your data. Keep it on a plain local disk — cloud-synced
> folders corrupt live SQLite files.

### 3. Launch it on a network-reachable port

```powershell
$env:BAYTRACKER_DATA = "C:\BayTrackerData"
venv\Scripts\python.exe -m waitress --listen=0.0.0.0:5000 --threads=32 app:app
```

- **`0.0.0.0` is the part that makes it reachable from other machines** — it binds every
  network interface. (`127.0.0.1` / `localhost` would only work on this PC.)
- `5000` is the port. Change it here (and everywhere below) if 5000 is taken.
- `--threads=32` leaves headroom for one long-lived connection per TV + the console; set it
  comfortably above your number of screens.

Leave that window open — the server is now running. (For a quick local-only smoke test you can
instead run `venv\Scripts\python.exe app.py`; use **waitress** for the real deployment.)

**Open the firewall** so other machines aren't blocked — run this once in an **elevated**
PowerShell (or use `setup.ps1 -OpenFirewall`):

```powershell
netsh advfirewall firewall add rule name="BayTracker" dir=in action=allow protocol=TCP localport=5000
```

### 4. Point another machine at it

1. On the **server PC**, find its LAN address: run `ipconfig` and note the **IPv4 Address**
   (e.g. `192.168.1.50`).
2. From **any other machine on the same network**, open a browser to that IP and port:

   | For | URL (substitute your server's IP) |
   | --- | --- |
   | TV dashboard | `http://192.168.1.50:5000/dashboard` |
   | Logging console | `http://192.168.1.50:5000/console` |
   | Stats / Admin | `http://192.168.1.50:5000/stats` and `/admin` |

   Add `?division=<name>` to a dashboard URL (e.g. `/dashboard?division=Assembly`) to make that
   TV take over full-screen only for its own division's delays.

3. **Pin the address:** set a **DHCP reservation** on your router for the server PC so its IPv4
   never changes — otherwise every display breaks when the address shifts.

> **Reachable from the server but not from other machines?** It's almost always one of:
> the server bound `127.0.0.1` instead of `0.0.0.0` (step 3), the firewall rule is missing
> (step 3), or the two machines are on different networks/VLANs. Test from the server first
> with `http://localhost:5000/healthz`, then from the other machine with the server's IP.

For a full production install that also opens the firewall **and** installs an auto-start,
auto-restart Windows service, run (elevated): `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -OpenFirewall -InstallService`.
See *Run it as a service* below to keep it alive across reboots.

---

## First-run configuration (important)

The system starts with **no operational data and no example values** — not even shift or
break times (this is intentional; see *Data integrity* below). On first run it creates only
the 12 empty bay slots and the mandatory **"Other"** delay reason.

Open **`/admin`** and enter your real values:

- **Divisions** and **Delay reasons** (each reason maps to a division + an in/out-of-control tag).
- **Product numbers** (the known short list) and the **Initials roster**.
- **Bays & layout** (rename bays; enable extra top-row bays for a 4×4 grid).
- **Break schedule**, **Shifts** (attribution cutoffs), and the **Operating calendar**.
  - Until you enter operating hours, the system counts **all** elapsed time (nothing freezes
    for off-hours). Once entered, breaks and off-hours become non-counting time.
- **Behaviour/cost settings**: delay-takeover duration, stale-item thresholds, labor cost rate.
- **PINs** for Stats and Admin (until set, those pages are open — set them first).

---

## The four pages

| URL | Who | What |
|-----|-----|------|
| `/dashboard` | TVs (kiosk) | Read-only live grid. `?division=<name>` makes it take over full-screen only for that division's delays. |
| `/console` | Central PC | Click a bay to Start / Move / Complete / Mate / Flag-or-Clear delay / Unit-complete / Scrap. Barcode-scanner friendly. |
| `/stats` | Management (PIN) | Date-range filters, Pareto/utilization/cycle/throughput charts, cost estimates, the corrections workflow, and CSV/XLSX export. |
| `/admin` | Management (PIN) | All configuration above. |

Health check for monitoring/updates: **`GET /healthz`** → `200 {"status":"ok"}`.

---

## Pin the address + point the displays

1. **DHCP reservation:** reserve this PC's IP on your router so the address never changes.
   If the IP shifts, every display breaks.
2. **Hard-wire** the server and the logging PC (never on wifi). Wire displays where you can.
3. **Displays:** a Fire TV Stick (or similar) with the **Silk** browser pointed at
   `http://<PC-IP>:5000/dashboard` is the most reliable cheap option. Many Roku-based smart
   TVs have **no browser** and can't load the page — use a stick or a Google/Android TV.
   Disable sleep/screensaver on the device. For true set-and-forget kiosks, a mini-PC / Pi
   running Chrome in kiosk mode is the most robust.

The dashboard auto-fullscreens on first tap, tries to keep the screen awake, auto-reconnects,
and shows an **"OFFLINE / last updated X ago"** banner if the link drops — so a frozen TV is
obvious. Because elapsed time is recomputed from stored timestamps, a wifi blip never desyncs
or loses data; the device self-corrects on reconnect.

---

## Run it as a service

**Recommended — NSSM** (auto-start on boot, auto-restart on crash): place `tools\nssm.exe`
in the repo (see `tools\README.txt`), then run `setup.ps1 -InstallService`.

**Alternative — Task Scheduler:** create an "At startup" task running
`venv\Scripts\python.exe -m waitress --listen=0.0.0.0:5000 --threads=32 app:app` with the repo
as the working directory and `BAYTRACKER_DATA` set for that account.

---

## Backups

The database file is the only irreplaceable asset — protect it.

```powershell
# One-off / scheduled consistent backup (writes to BAYTRACKER_DATA\backups):
powershell -ExecutionPolicy Bypass -File .\backup.ps1

# Also copy off-machine to a network share:
powershell -ExecutionPolicy Bypass -File .\backup.ps1 -Dest \\server\share\baytracker
```

Schedule it daily (Task Scheduler). You can also set a **backup network path** in `/admin`,
which `backup.ps1` will copy to automatically. `update.ps1` always backs up before updating.
The CSV/XLSX exports are a secondary safety net, **not** a substitute for copying the DB file.

---

## Updating safely

Releases are **git tags** (e.g. `v1.1.0`). The floor PC always runs a known tag, never bare
`main`, and **never auto-pulls**. To update deliberately:

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1 -Tag v1.1.0
```

`update.ps1` (1) backs up the DB, (2) records the current version, (3) checks out the tag,
(4) installs pinned deps, (5) runs `migrate.py` (additive/idempotent), (6) restarts, (7) health-checks,
and (8) **automatically rolls back** to the previous version if the health check fails.

---

## Exports

The Stats page produces four purpose-shaped tables (one row = one real thing), respecting the
current filters (or "export everything"):

1. **Delays** — one row per delay episode.
2. **Bay Runs** — one row per bay occupancy.
3. **Unit Journeys** — one row per work order.
4. **Events (raw)** — the full append-only log.

**CSV** export is a `.zip` of the four files plus `README_data_dictionary.txt`. **XLSX** is one
workbook with those four tabs plus a **Data Dictionary** tab. Timestamps are ISO 8601 (seconds
kept for precision); durations are decimal minutes (+ an H:MM twin); `work_order` and
`product_number` are forced to **text** so Excel can't eat leading zeros. Open runs/delays show
blank end times and blank durations — never a fabricated value.

---

## Data integrity (no fabricated data)

The database starts empty of all operational records. The system never invents, estimates,
randomizes, interpolates, or default-fills a value. Open records stay open. Stats show only real
recorded data ("no data" where there is none). Cost is shown only if you enter a labor rate. The
only auto-created rows are the empty structural bay slots and the mandatory "Other" reason.

---

## Repository layout

```
app.py            WSGI app (app:app): routes, SSE, JSON API, PIN gate, /healthz
init_db.py        Create + seed the DB (non-destructive)
migrate.py        Forward-only, idempotent schema migrations
backup_db.py      Consistent online backup of the DB
setup.ps1         One-time install     update.ps1   Safe, reversible update
backup.ps1        Scheduled backup wrapper
requirements.txt  Exactly pinned dependencies
baytracker/       Application package:
  config.py  db.py  bootstrap.py  schedule.py  events.py  state.py
  actions.py  exports.py  metrics.py  sse.py  auth.py  app_db.py
templates/        dashboard / console / stats / admin / unlock / base
static/           css/  js/  vendor/chart.umd.min.js (vendored, offline)
tools/            nssm.exe goes here (see tools/README.txt)
```

The `.gitignore` excludes all data and generated files (`*.db`, backups, exports, `venv/`).
**The database is never committed.**

---

## Developer notes

```powershell
# Run locally (Flask dev server, foreground):
$env:BAYTRACKER_DATA = "C:\BayTrackerData_dev"
venv\Scripts\python.exe init_db.py
venv\Scripts\python.exe app.py        # http://localhost:5000
```

Keep the code simple and heavily commented — it's meant to be maintained by an engineering
intern, not a dedicated dev team. The time-accounting rules (active/delay/queue/cycle, the union
of parallel work, and non-counting break/off-hours time) live in `baytracker/schedule.py` and
`baytracker/state.py`.
