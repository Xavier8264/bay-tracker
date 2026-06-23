"""
Bay Tracking & Logging System -- application package.

This package contains all of the application logic, split into small, focused,
heavily-commented modules so an engineering intern can maintain it:

    config.py    -- where the data folder / database file live (BAYTRACKER_DATA)
    db.py        -- SQLite connection helpers + the full schema (CREATE IF NOT EXISTS)
    schedule.py  -- the time engine: breaks, off-hours, shifts, "non-counting" time
    events.py    -- the append-only event log (the single source of truth)
    state.py     -- replays the event log into the *current* state of every bay/unit
    actions.py   -- validates a requested action against current state, then logs it
    exports.py   -- builds the four purpose-shaped CSV/XLSX tables + data dictionary
    sse.py       -- tiny in-process publish/subscribe broker for Server-Sent Events

The Flask application object (`app`) is created in the top-level ``app.py`` so the
WSGI server can import it as ``app:app`` (see Appendix B1 of the spec).
"""

# Single source of truth for the version string. `update.ps1` deploys git tags
# like "v1.0.0"; this constant is what /healthz and the footer report.
__version__ = "1.4.0"
