"""Explicit operator command. No public registration or privilege-elevation endpoint."""

import argparse
import os
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def main():
    parser = argparse.ArgumentParser(
        description="Provision an existing Supabase clinician identity"
    )
    parser.add_argument("--id", required=True, type=UUID, help="UUID of an existing Auth user")
    parser.add_argument("--role", required=True, choices=["nurse", "doctor"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--registration", required=True)
    args = parser.parse_args()
    if not args.name.strip() or len(args.name) > 80 or not args.registration.strip():
        parser.error("Invalid display name or professional registration")
    load_dotenv()
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url or not url.startswith("postgresql+psycopg://"):
        parser.error("Set MIGRATION_DATABASE_URL for this administrative command only")
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as db:
        # Only provision identities already created/verified in Supabase Auth.
        exists = db.execute(
            text("SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=:id)"), {"id": args.id}
        ).scalar_one()
        if not exists:
            parser.error("Supabase Auth identity does not exist")
        db.execute(
            text("""INSERT INTO app.staff_profiles(id,display_name,role)
                           VALUES (:id,:name,:role)"""),
            {"id": args.id, "name": args.name.strip(), "role": args.role},
        )
        table = "nurses" if args.role == "nurse" else "doctors"
        db.execute(
            text(f"INSERT INTO app.{table}(id,registration_number) VALUES (:id,:reg)"),
            {"id": args.id, "reg": args.registration.strip()},
        )
        db.execute(
            text("""INSERT INTO app.audit_logs(id,actor_id,action,outcome,request_id)
                           VALUES (gen_random_uuid(),:id,'staff.provision','success',gen_random_uuid())"""),
            {"id": args.id},
        )
    engine.dispose()
    print("Clinician provisioned. No passwords or tokens were printed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Clinician provisioning failed; check private settings and database access"
        ) from None
