"""
One-time script: backfill sequence_enrollment records from Smartlead.

Use this when prospects were enrolled in Smartlead but the DB records
were not committed (e.g. process killed mid-bulk-enroll). Fetches all
leads from the specified campaign, matches by email against prospects,
and inserts any missing sequence_enrollment rows.

Run from Railway console:
  python scripts/sync_smartlead_enrollments.py --campaign-id 3182419 --campaign-name "WCP - Cold Outbound v1"

Or with --dry-run to preview without writing:
  python scripts/sync_smartlead_enrollments.py --campaign-id 3182419 --dry-run
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.integrations import smartlead
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--campaign-name", default="")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    campaign_id = args.campaign_id
    campaign_name = args.campaign_name
    dry_run = args.dry_run

    print(f"Fetching all leads from Smartlead campaign {campaign_id}…")
    smartlead_emails = smartlead.get_all_campaign_lead_emails(campaign_id)
    print(f"  Found {len(smartlead_emails)} leads in Smartlead")

    if not smartlead_emails:
        print("Nothing to sync.")
        return

    db = SessionLocal()
    try:
        # Existing active enrollments in this campaign
        existing = {
            str(row.prospect_id)
            for row in db.query(SequenceEnrollment.prospect_id)
            .filter(
                SequenceEnrollment.smartlead_campaign_id == str(campaign_id),
                SequenceEnrollment.status == "active",
            )
            .all()
        }
        print(f"  DB already has {len(existing)} active enrollment records for this campaign")

        # Match Smartlead emails → prospect rows
        prospects_by_email = {
            p.email.lower(): p
            for p in db.query(Prospect).filter(
                Prospect.email.in_(list(smartlead_emails))
            ).all()
        }
        print(f"  Matched {len(prospects_by_email)} of {len(smartlead_emails)} Smartlead emails to DB prospects")

        inserted = 0
        not_found = 0
        already_exists = 0

        for email in smartlead_emails:
            prospect = prospects_by_email.get(email.lower())
            if not prospect:
                not_found += 1
                continue
            if str(prospect.id) in existing:
                already_exists += 1
                continue
            if not dry_run:
                db.add(SequenceEnrollment(
                    prospect_id=prospect.id,
                    smartlead_campaign_id=str(campaign_id),
                    campaign_name=campaign_name or None,
                    status="active",
                ))
            inserted += 1

        if not dry_run:
            db.commit()

        print()
        if dry_run:
            print("DRY RUN — no changes written")
        print(f"  Would insert / Inserted : {inserted}")
        print(f"  Already in DB (skipped) : {already_exists}")
        print(f"  Not found in prospects  : {not_found}")
        print("Done.")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
