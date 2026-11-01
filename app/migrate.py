"""Apply versioned PostgreSQL migrations in order (run as a release step)."""
from __future__ import annotations
import hashlib
from pathlib import Path
from app.database import Database, DatabaseSettings

def migrate(directory: Path | None = None) -> list[str]:
    root = directory or Path(__file__).resolve().parents[1] / "db" / "migrations"
    db = Database(DatabaseSettings.from_env())
    applied=[]
    db.open()
    try:
        with db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations (version text PRIMARY KEY, checksum char(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())")
        for path in sorted(root.glob("*.sql")):
            content=path.read_bytes(); checksum=hashlib.sha256(content).hexdigest()
            with db.transaction() as conn:
                prev=conn.execute("SELECT checksum FROM public.schema_migrations WHERE version=%s",(path.name,)).fetchone()
                if prev:
                    if prev["checksum"].strip()!=checksum: raise RuntimeError(f"Applied migration changed: {path.name}")
                    continue
                conn.execute(content.decode("utf-8"))
                conn.execute("INSERT INTO public.schema_migrations(version,checksum) VALUES (%s,%s)",(path.name,checksum))
            applied.append(path.name)
    finally: db.close()
    return applied

if __name__ == "__main__":
    versions=migrate()
    print("Applied: " + (", ".join(versions) if versions else "no new migrations"))
