"""""
apply_schema.py — applies src/storage/schema.sql (existing) and
knowledge_base_schema.sql (new) against the TimescaleDB instance brought
up by docker-compose.yml.
 
Usage:
    python scripts/apply_schema.py
 
Reads DATABASE_URL from the environment (falls back to the default
docker-compose credentials if unset — check yours match).
 
Safe to re-run: every statement in both files should be idempotent
(CREATE TABLE IF NOT EXISTS, CREATE OR REPLACE VIEW, etc.) — if your
existing schema.sql isn't, add IF NOT EXISTS / OR REPLACE before running
this against a non-empty database.
"""
 
import os
import sys
from pathlib import Path
 
import psycopg
 
DEFAULT_DSN = "postgresql://ocp:change_me@127.0.0.1:5432/alarms"
 
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILES = [
    REPO_ROOT / "src" / "storage" / "schema.sql",
    REPO_ROOT / "src" / "storage" / "knowledge_base_schema.sql",
]
 
 
def apply_file(cur, path: Path):
    if not path.exists():
        print(f"  SKIP (not found): {path}")
        return
    sql = path.read_text(encoding="utf-8")
    print(f"  Applying {path} ({len(sql)} bytes)...")
    cur.execute(sql)
    print(f"  OK: {path.name}")
 
 
def main():
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    print(f"Connecting to {dsn.split('@')[-1]}...")  # don't print credentials
 
    try:
        conn = psycopg.connect(dsn)
    except psycopg.OperationalError as e:
        print(f"Could not connect: {e}")
        print("Check that `docker compose up -d` has been run and DATABASE_URL is set.")
        sys.exit(1)
 
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for f in SCHEMA_FILES:
                apply_file(cur, f)
        conn.commit()
        print("Schema applied successfully.")
    except Exception as e:
        conn.rollback()
        print(f"Failed, rolled back: {e}")
        sys.exit(1)
    finally:
        conn.close()
 
 
if __name__ == "__main__":
    main()