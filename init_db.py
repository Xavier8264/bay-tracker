"""
init_db.py -- create the database (if needed) and seed structural defaults.

Run once when first installing on a PC:

    python init_db.py

It is NON-DESTRUCTIVE and safe to run again at any time (Appendix B3):

  * Tables are created with CREATE TABLE IF NOT EXISTS -- existing tables and
    every row in them are left exactly as they are.
  * Seeding only fills gaps (12 bay slots if there are none yet, the mandatory
    "Other" reason, behavioural default settings if unset). It never drops,
    truncates, overwrites, or resets anything.

Where the database lives is controlled by the BAYTRACKER_DATA environment
variable (see baytracker/config.py). On a clean machine the folder is created
automatically.
"""

from baytracker import config, db, bootstrap


def main() -> None:
    # Make sure the data folder (outside the repo) exists.
    config.ensure_data_dirs()

    already_existed = config.DB_PATH.exists()

    conn = db.connect()
    try:
        db.create_schema(conn)   # CREATE TABLE IF NOT EXISTS ... (safe to repeat)
        bootstrap.seed(conn)     # fill only the gaps
    finally:
        conn.close()

    where = config.DB_PATH
    if already_existed:
        print(f"[init_db] Database already existed; verified schema + defaults (no data touched).")
    else:
        print(f"[init_db] Created new database.")
    print(f"[init_db] Database file: {where}")
    print(f"[init_db] Data folder:   {config.DATA_DIR}")
    print("[init_db] Done. Operational config (reasons, divisions, products, "
          "initials, shift/break/operating times) starts EMPTY -- enter it in /admin.")


if __name__ == "__main__":
    main()
