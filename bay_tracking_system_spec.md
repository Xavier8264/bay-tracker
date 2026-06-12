# Build Spec: Production Bay Tracking & Logging System

You are building a complete, self-contained web application that tracks work happening in a set of
production "bays" on a manufacturing floor, displays live status on TVs throughout the plant, lets
technicians log activity from a single central computer, and keeps a comprehensive, exportable log
for later analysis. Build the entire system from this spec. Prefer simple, maintainable, well-commented
code over clever abstractions. When something is configurable, default it to the placeholder values
noted here and make it editable in the Settings page — do not hard-code operational values.

---

## 1. Context & Goals

These bays are the final stage of production and are the bottleneck of the whole operation, so when a
bay has a problem it needs to be seen and addressed fast, plant-wide. Every step here is manual work
done by technicians across three shifts; there are no automated/unattended processes in this division.

The system must:
- Show a live, glanceable status grid on cheap TVs placed throughout the plant.
- Let technicians log what's running, move work between bays, and flag/clear delays from one central
  computer, with as little friction (clicks/typing) as possible.
- Keep a complete, tamper-evident history that can be analyzed later and exported to CSV and XLSX.
- Be intuitive and easy for technicians to adapt to.

---

## 2. Architecture

- **Backend:** Python + Flask. **Database:** SQLite (single file on disk).
- **Live updates:** Server-Sent Events (SSE) so dashboards update near-instantly. Fall back to polling
  every few seconds if SSE drops. Data volume is tiny (status of ~12–16 bays), so bandwidth is a
  non-issue; design for *stable reconnection*, not throughput.
- **Frontend:** plain HTML/CSS/JS. No heavy framework — must render reliably on weak TV browsers /
  cheap streaming-stick browsers.
- **Hosting:** runs on the central Windows PC (located between bays 10 and 11), served over the local
  network. TVs and the logging station are just browsers pointing at this server. No internet required.
- **Source of truth = an append-only event log.** All current state (what's in each bay, elapsed time,
  delays) is *derived* by replaying events. Never mutate history. On startup the server rebuilds current
  state by replaying the log, so a reboot or crash loses nothing.
- All event timestamps are generated **server-side** (never from client devices) for consistency.

---

## 3. Domain Model

**Bay** — a physical station. 12 standard bays, with the option to enable up to a full top row of extra
bays (see Layout). A bay is a normal entity regardless of position; only its on-screen placement differs.

**Work Order** — the unique identifier for one physical unit. **One work order = exactly one unit.**
A unit may occupy **up to 2 bays simultaneously** (two sub-assemblies built in parallel, later mated).
The system must warn/block assigning a third bay to the same work order.

**Product Number** — the *variation/type* of the unit; the grouping key for "similar jobs" analysis.
Maintained as an editable short list plus free-entry for oddballs (see Console entry rules).

**Component label** — optional free-text label (e.g., "Half A") used to distinguish the two streams when
one work order is in two bays.

**Event** — an append-only record. Event types include at least:
`START` (unit started in a bay), `MOVE` (convenience: completes in current bay + starts in target bay),
`COMPLETE_BAY` (unit leaves a bay into the WIP/queue pool), `MATE` (two streams of one work order join
into one), `DELAY_START`, `DELAY_CLEAR`, `UNIT_COMPLETE` (terminal — unit done/leaves the area),
 `CORRECTION` (supersedes an earlier event's time; original retained).

Every event stores: event id, server timestamp (to the second), event type, bay (if applicable),
work order, product number, component label, delay reason / division / in-or-out-of-control / note
(for delay events), initials, and a reference to the superseded event (for corrections).

---

## 4. Time Accounting (precise rules — get these exactly right)

Track two different clocks; both derive from the same event log:

- **Unit cycle time** = wall-clock from a unit's first START to its UNIT_COMPLETE, **counting only
  operating time** (see non-counting time below). Parallel work in two bays overlaps in real time, so
  for the unit you count the *union* of its active periods, never the sum.
- **Bay-hours / utilization** = sum of each bay's occupied time. Two bays running at once = two bay-hours.

Within a unit's cycle, break it into:
- **Active time** = time a bay is RUNNING, excluding delay time and excluding non-counting time.
- **Delay time** = time a bay is DELAYED, excluding non-counting time.
- **Queue time** = time a unit sits COMPLETE_BAY (in the WIP pool) before it's started in another bay or
  terminally completed, excluding non-counting time. (Queue may often be ~0 if techs use MOVE for direct
  handoffs; that's fine.)
- So: cycle = active + delay + queue.

**Non-counting time** — the system knows when work is *not* happening, and **no timer ticks** during it:
1. **Scheduled breaks** (lunch + short breaks), and 2. **Off-hours** (any time outside the operating
   calendar). During non-counting time, **both active AND delay timers pause** and resume afterward.

When a delay is flagged: **pause the bay's active timer and start its delay timer.** When cleared:
stop delay, resume active.

Each run and delay is **attributed to a shift** by its timestamp using clean shift cutoffs (approximate
real-world overlaps like a 2:00/2:30 handoff don't matter for attribution).

Store all times to the second for accuracy. **Never display seconds** — the UI shows minutes and hours
only (e.g., "1:23" or "47 min"). Elapsed time is always computed as (now − stored start), minus
non-counting time, so every screen agrees and a refresh never resets it.

---

## 5. Auto-Break Engine

Breaks are **automatic and clock-driven — there is no manual break button.** A schedule of break windows
(start time + duration) lives in Settings and is the same every day. At each window the system pauses all
running timers (active and delay) for all bays and automatically resumes them when the window ends. Bays
display a distinct **ON BREAK** state during the window (not red — must never be confused with a delay).

Because every process here is manual, the pause applies to **all** bays. Do **not** build any per-bay
"ignore breaks" exemption — it would add tracking burden for no benefit here.

Cleaning / shift-turnover time is **not** hard-coded or auto-handled (it's variable); leave it out.

**Placeholder schedule values to seed (EDIT IN SETTINGS — user will confirm exact times):**
- Shift 1 ≈ 06:00–14:00 (may end ~14:30); lunch 11:30 (30 min); breaks 07:00 (10 min) and a second TBD.
- Shift 2 ≈ 14:00–22:00; lunch + two 10-min breaks, times TBD.
- Shift 3 ≈ 22:00–06:00; lunch + two 10-min breaks, times TBD.
- Operating calendar ≈ 24 hours, 6 days; Saturday possibly 2 shifts only; Sunday occasional. Make the
  operating days/hours fully editable; off-hours are non-counting time.

---

## 6. Pages / Views

All four are the same app and visual language, different capabilities by route.

### 6a. Dashboard (`/dashboard`) — read-only, for TVs
- **Grid:** default **4×3** showing bays 1–12 in fixed positions (bay 1 top-left; never reorder by status).
- **Extra bays / 4×4:** when extra bays are enabled in Settings, render a **4×4** grid where bays 1–12
  occupy the bottom three rows and the extra bay(s) sit in the **top row** in admin-chosen columns
  (e.g., bay 13 directly above bay 2). Unused top-row cells render **greyed out / empty**.
- **Tile contents (keep uncluttered, ~3–4 items on the face):**
  - Running (green): bay name, work order #, live elapsed time (no seconds), small component label if present.
  - Delayed (red): bay name, work order #, short delay reason, small division tag, how long it's been delayed.
  - Idle (grey): bay name + IDLE.
  - On Break: distinct color + "ON BREAK"; timers frozen.
  - Tap/click to expand for notes, history, initials, mate/component detail.
- **Colorblind-safe:** never rely on color alone — include a text label/icon (e.g., "DELAYED").
- **Full-screen delay takeover:** when a delay is flagged, the screen goes full-screen on that delay
  (bay, work order, reason, division, who flagged it) for a **configurable duration (default 10–15s)**,
  then returns to the grid with that bay red until cleared. The takeover **follows the screen's filter**:
  an all-bays TV takes over for any delay; a division-filtered TV only for its own division's delays.
  If multiple delays land during a takeover, **queue them** so none are skipped.
- **Division filter:** `/dashboard?division=<name>` shows/takes-over only that division's delays.
- **Kiosk behavior:** auto-fullscreen, prevent sleep/screensaver, auto-reconnect on drop, and show an
  obvious "OFFLINE / last updated X min ago" indicator if the connection is lost (so a frozen TV is visible).
- **No cost/money information ever appears here.**
- **No countdown / target-time indicator** (see Future Hooks).

### 6b. Console (`/console`) — interactive, on the central PC
Mirrors the dashboard grid but tiles are clickable. Optimize the common path for minimum clicks/typing.
Live-refreshes so concurrent edits don't clobber. Barcode-scanner friendly (scanners act as keyboards).

Actions on a bay (validity depends on current state):
- **Start:** enter Work Order + Product Number + Initials.
  - *Product Number entry:* type-to-filter the known short list, matching **consecutive digits anywhere
    in the number** (substring, not just prefix — so typing the last three digits finds it). An **"Other"**
    option reveals a free-text field to enter an oddball product number.
- **Move to bay…:** dropdown to pick a target bay; closes the run here and opens it there (atomic handoff).
- **Complete at bay (to queue):** unit leaves this bay into the WIP/queue pool (accrues queue time until
  started elsewhere or completed).
- **Mate:** available when one work order occupies 2 bays; joins the two streams into one continuing unit
  (choose which bay it continues in; the other frees up). Logged as a MATE event.
- **Flag Delay:** choose a **reason** from the editable dropdown ("Other" pinned at the bottom); the
  reason carries its **division** and its **in/out-of-control** tag automatically; a **note is required
  (at least one character) on every delay**; Initials required. Flagging pauses active, starts delay,
  turns the bay red, and fires the takeover.
- **Clear Delay:** Initials required; stops delay, resumes active.
- **Unit Complete:** terminal; closes the unit's whole journey. Initials required.
- **Scrap:** terminal; Initials required.
- **Every logged action requires an Initials field.** Autocomplete from previously-seen initials but
  allow new ones (roster is editable in Settings).
- Confirmation prompts only on destructive/terminal actions (Scrap, Unit Complete, corrections).

### 6c. Stats (`/stats`) — PIN-protected (holds sensitive cost data)
- **Date-range filter:** presets (Year-to-date, last quarter, a specific quarter, today, this shift) plus
  custom range. Additional filters: by bay, delay reason, division, product number, shift.
- **Views/metrics (rendered in-browser with a lightweight chart lib):**
  - Delay Pareto: count and total delay time by reason and by division.
  - Bay utilization %.
  - Average cycle time by product number ("similar jobs" comparison across a range).
  - Throughput (units completed per day/week).
  - WIP (units currently in the area), queue time between bays, frequency of parallel (2-bay) work.
- **Cost estimation:** an input for **labor cost rate**; multiply against delay time to estimate cost
  **per reason and total** over the selected range. Cost lives only here, never on the dashboard.
- **Open & Recent / corrections:** list of everything currently open (runs and delays) plus recently
  closed, with **stale items flagged at top** (e.g., a delay open longer than a configurable threshold).
  Fix a forgotten clear by selecting the delay and setting when it **actually ended** ("ended now" or
  type a time); same pattern to adjust a run's start/complete time. Each correction requires initials,
  is logged as a CORRECTION event, and **leaves the original intact** (audit trail stays honest). Use
  simple per-entry forms with editable time fields — never raw database/spreadsheet editing.
- **Export buttons:** produce both CSV and XLSX (see Export Format). Exports respect the current filters,
  plus an "export everything" option.

### 6d. Settings / Admin (`/admin`) — PIN-protected
All operational values are edited here (nothing hard-coded). Editable:
- **Delay reasons:** add/rename/**soft-retire** (never hard-delete one referenced in history); each maps
  to a **division** and an **in/out-of-control** tag. "Other" is always present and pinned to the bottom.
- **Divisions** list.
- **Product numbers** (the known short list).
- **Initials roster.**
- **Bays & layout:** enable extra bays, set how many and which top-row column each occupies (default 4×3,
  no extras).
- **Break schedule** (windows: start time + duration) and **shift / operating calendar** (shift cutoffs,
  operating days/hours). Seed with the placeholder values in §5.
- **Delay takeover duration** (default ~12s).
- **Stale-item thresholds** (when an open run/delay gets flagged for review).
- **Labor cost rate.**
- **PIN(s)** for stats/admin.
- **Backup settings** (see Deployment).

---

## 7. States & Transitions

Bay states: **IDLE** (grey), **RUNNING** (green), **DELAYED** (red), **ON BREAK** (distinct, timers frozen).
Unit terminal outcomes: **UNIT_COMPLETE**, **SCRAP**. There is intentionally **no separate Hold state** —
if needed, a hold is represented as a delay reason.

Enforce: a work order may be active in at most 2 bays at once (warn/block a 3rd). Delays attach to a bay's
current run. (Reasons distinguish bay/equipment problems from unit/material problems via the in/out-of-control
tag and division.)

---

## 8. Export Format (CSV **and** XLSX)

Do **not** export the raw event log as the primary artifact — derive purpose-shaped tables where
**one row = one real thing.** Produce four tables. As CSV they are four files; as XLSX they are four tabs
in one workbook (plus a Data Dictionary tab). They share keys (work_order, bay, timestamps, row ids) so
they can be joined, but each reads on its own.

1. **Delays** — one row per delay: work_order, product_number, bay, reason, division, in_or_out_of_control,
   note, started_at, cleared_at, delay_minutes (break/off-hours excluded), flagged_by, cleared_by, shift,
   est_cost (only if a labor rate is set).
2. **Bay Runs** — one row per time a unit occupied a bay: work_order, product_number, bay, started_at,
   ended_at, active_minutes (delay + break excluded), delay_minutes, total_minutes, shift, started_by,
   completed_by.
3. **Unit Journeys** — one row per work order: product_number, first_started_at, completed_at,
   cycle_minutes, active_minutes, delay_minutes, queue_minutes, bays_visited (e.g., "3 → 7 → 5"),
   delay_count, outcome (complete/scrap).
4. **Events (raw)** — the full append-only log, one row per event; the source of truth.

**Formatting conventions:**
- Timestamps in **ISO 8601** ("2026-06-12 14:30:05") — keep seconds in the export for precision even
  though the UI hides them; sorts and parses cleanly, no MM/DD ambiguity.
- Durations as **plain decimal minutes** (e.g., 47.5) so they can be summed/averaged directly. (Optionally
  add a formatted "H:MM" twin column.)
- **work_order and product_number as text** — prevent Excel from eating leading zeros (0347 → 347) or
  using scientific notation. In XLSX, force these cell types to text; this is why XLSX is the safer
  human-readable artifact.
- **UTF-8 with BOM**, fields with commas/newlines (notes) properly quoted.
- Column order: **identifiers → times → durations → categories → notes/initials.**
- **Self-documenting filenames**: `delays_2026-Q1.csv`, `bay_runs_2026-01-01_to_03-31.csv`, etc.
- Include a **Data Dictionary** (a tab in XLSX, a README file with the CSVs) defining every column and
  exactly how active/delay/queue/cycle minutes are computed.

---

## 9. Deployment & Ops

- Package to run on the central **Windows** PC. Run the server so it **auto-starts on boot** (e.g., a
  Windows service / scheduled task using a production WSGI server such as waitress) and survives reboots.
- Server on a **static IP / stable hostname**; document the single port to open in the firewall.
- TVs: cheap mini-PC or streaming stick driving each display, browser set to **auto-launch the dashboard
  URL in kiosk/fullscreen on boot**, sleep disabled. Wire the server (and displays where cable runs are
  feasible); weak wifi is acceptable per display since data is tiny, as long as it stays connected.
- **Backups:** scheduled copy of the SQLite file to a network share so a dead PC doesn't erase history.
- Include clear setup/run instructions (install, initialize DB with seed config, start service, set TV URLs).

---

## 10. Future Hooks — design for, but DO NOT BUILD now

- **Countdown vs. target time:** include a **dormant `target_minutes` field per product number** in the
  schema, but **build no countdown UI and no indicator.** Reason: current targets are unrealistic, so a
  countdown would have every bay always overrun and train people to ignore it before it's meaningful. Keep
  it invisible and zero-burden until a later revision turns it on.
- **Notifications:** no notification system now (the visual full-screen takeover is the alert). Leave room
  to later text/message key people about delays, but build none of it now.
- **Second input device:** the Console is a web page, so a tablet could later share logging load off the
  central PC. No work needed now beyond it being browser-based.

---

## 11. Non-Goals (explicitly out of scope)

- No countdown/target indicator (dormant field only).
- No notifications/SMS/Slack/email.
- No per-bay break exemption (all bays are manual; blanket pause).
- No external work-order/ERP integration — units are created on first Start.
- No money/cost anywhere except the PIN-protected Stats page.

---

## 12. Build Guidance

Build it end to end: backend (event log + derived-state engine + time/break/operating logic + SSE +
export), then the four pages, then deployment scaffolding. Seed the database with the placeholder config
from §5 and sample divisions/reasons so it runs immediately. Keep the code simple, readable, and
heavily commented — this will be maintained by an engineering intern, not a dedicated dev team. Where this
spec leaves a value unspecified (exact shift/break times, weekend schedule), implement it as editable
settings with the placeholder defaults and a clear "confirm these" note in the UI.
