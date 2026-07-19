"""
check_invariants.py -- mechanically check the project's load-bearing rules.

AGENTS.md lists six rules that keep this system safe over years of unattended
running. Some of them can be verified by grepping the tree; this script does
that, prints PASS/FAIL lines in the same style as the scripts in tests/, and
exits non-zero on any violation. Run it after any code revision and before
every release. The full checklist -- including the rules a script cannot see
(durations through Schedule.counted_seconds, no fabricated data, server-side
timestamps) -- lives in .claude/skills/baytracker-invariants/SKILL.md.

    venv\\Scripts\\python.exe tools\\check_invariants.py

Scope notes (keep the checker green on a clean tree):
  * Only baytracker/*.py and app.py are scanned for Python patterns -- tests
    and demo tooling set env vars / write disposable data on purpose.
  * Lines whose first non-space character is '#' are skipped, but docstrings
    are NOT parsed: a docstring showing a forbidden pattern as an example will
    trip the checker. Reword the docstring rather than weakening the pattern
    (writing os.environ[...] with the literal ellipsis is ignored on purpose).
  * The migrate.py check matches UPPERCASE SQL only -- the codebase convention
    is uppercase SQL, and the migrate.py docstring discusses "drop"/"rename"
    in lowercase prose.
  * A line carrying an ``invariant-ok:`` trailing comment is skipped by the
    checks that opt in (only rule 5 today). The tag marks a reviewed,
    deliberate exception -- currently just the two PIN-gated admin
    "send a test message" endpoints, whose entire purpose is a synchronous
    send. Never add the tag without recording the justification in the diff.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY_FILES = sorted((ROOT / "baytracker").glob("*.py")) + [ROOT / "app.py"]

_failures = 0


def check(ok, label, detail=""):
    """Print one PASS/FAIL line (same style as tests/) and count failures."""
    global _failures
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
    if not ok:
        _failures += 1
        if detail:
            print(detail)


def code_lines(path):
    """Yield (lineno, text) for each line that isn't a pure comment."""
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        yield n, raw


def grep_py(pattern, label, flags=0, files=None, allow_marker=False):
    """Fail ``label`` if any scanned Python line matches ``pattern``.

    allow_marker: skip lines tagged with an ``invariant-ok:`` trailing
    comment -- a reviewed, deliberate exception (see module docstring).
    """
    rx = re.compile(pattern, flags)
    hits = []
    for path in (files if files is not None else APP_PY_FILES):
        for n, line in code_lines(path):
            if allow_marker and "invariant-ok:" in line:
                continue
            if rx.search(line):
                hits.append(f"    {path.relative_to(ROOT)}:{n}: {line.strip()}")
    check(not hits, label, "\n".join(hits))


# --- Rule 2: the events table is append-only --------------------------------
grep_py(r"\bUPDATE\s+events\b|\bDELETE\s+FROM\s+events\b",
        "events table is append-only (no UPDATE/DELETE on events)",
        flags=re.IGNORECASE)

# --- Secrets/config: missing config must boot cleanly -----------------------
grep_py(r"os\.environ\[(?!\.)",
        "no hard os.environ[...] reads (use os.environ.get)")

# --- Offline LAN: the frontend must not reference anything external ---------
_cdn_rx = re.compile(r"(?:src|href)\s*=\s*[\"']https?://", re.IGNORECASE)
_cdn_hits = []
for _path in sorted((ROOT / "templates").glob("*.html")):
    for _n, _line in code_lines(_path):
        if _cdn_rx.search(_line):
            _cdn_hits.append(f"    {_path.relative_to(ROOT)}:{_n}: {_line.strip()}")
check(not _cdn_hits, "templates reference no external/CDN URLs",
      "\n".join(_cdn_hits))

# --- Rule 5: the web request never sends notifications ----------------------
# Exception: the two PIN-gated admin "send a test message" endpoints call the
# adapters synchronously ON PURPOSE (their job is proving the config works
# right now, not queueing). Those call sites carry an ``invariant-ok:`` tag;
# any untagged reference to the senders in app.py still fails here.
grep_py(r"send_email_postmark|send_sms_twilio",
        "app.py calls notification senders only at tagged admin test endpoints",
        files=[ROOT / "app.py"],
        allow_marker=True)

# --- Dependencies are exactly pinned ----------------------------------------
_bad_pins = []
for _n, _raw in enumerate((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines(), 1):
    _line = _raw.split("#", 1)[0].strip()
    if _line and "==" not in _line:
        _bad_pins.append(f"    requirements.txt:{_n}: {_raw.strip()}")
check(not _bad_pins, "requirements.txt pins every dependency with ==",
      "\n".join(_bad_pins))

# --- Rule 6: migrations are additive-only -----------------------------------
grep_py(r"\bDROP\s+(?:TABLE|COLUMN)\b|\bRENAME\s+(?:TO|COLUMN)\b",
        "migrate.py is additive-only (no DROP/RENAME)",
        files=[ROOT / "migrate.py"])

print()
if _failures:
    print(f"{_failures} invariant check(s) FAILED -- see AGENTS.md, "
          "'The rules that must never be broken'.")
    sys.exit(1)
print("All invariant checks passed.")
