"""Bounded retention cleanup. Defaults to a dry run; deletion needs --delete."""
from __future__ import annotations
import argparse, os
from datetime import datetime, timedelta, timezone
from app.database import Database, DatabaseSettings, JobRepository

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete",action="store_true",help="actually remove expired terminal jobs")
    parser.add_argument("--limit",type=int,default=100)
    parser.add_argument("--audit-retention-days",type=int,default=90)
    args=parser.parse_args()
    if not 1<=args.limit<=1000 or not 1<=args.audit_retention_days<=3650: parser.error("limits are out of range")
    db=Database(DatabaseSettings.from_env()); db.open()
    try:
        repo=JobRepository(db); ids=repo.delete_expired_jobs(limit=args.limit,dry_run=not args.delete)
        old=datetime.now(timezone.utc)-timedelta(days=args.audit_retention_days)
        audit=repo.delete_old_audit_events(older_than=old,limit=min(args.limit*10,10000),dry_run=not args.delete)
        action="deleted" if args.delete else "would delete"
        print(f"{action} {len(ids)} expired terminal jobs; {action} {audit} old audit events")
    finally: db.close()
    return 0

if __name__=="__main__": raise SystemExit(main())
