"""Fail-closed ledger ACL protection, inside Alembic's outer transaction."""

from sqlalchemy import text

LEDGER_ROLES = (
    "anon",
    "authenticated",
    "service_role",
    "dhanvantari_api",
    "dhanvantari_clinical_actor",
    "dhanvantari_runtime",
)


def protect_migration_ledger(connection):
    if not connection.in_transaction():
        raise RuntimeError("Ledger protection requires the migration transaction")
    identity = (
        connection.execute(
            text("""
        SELECT c.oid, c.relowner, c.relkind, pg_get_userbyid(c.relowner) AS owner,
               current_user AS administrator
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname='alembic_version'
    """)
        )
        .mappings()
        .one()
    )
    if identity["relkind"] != "r" or identity["owner"] != identity["administrator"]:
        raise RuntimeError("Unexpected migration ledger ownership")
    oid = identity["oid"]
    owner_acl = connection.execute(
        text("""
        SELECT x.grantor,x.grantee,x.privilege_type,x.is_grantable
        FROM pg_class c CROSS JOIN LATERAL
          aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) x
        WHERE c.oid=:oid ORDER BY x.grantee,x.privilege_type,x.grantor
    """),
        {"oid": oid},
    ).all()
    allowed = {*LEDGER_ROLES, "PUBLIC", identity["owner"]}
    recipients = (
        connection.execute(
            text("""
        SELECT CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END
        FROM pg_class c CROSS JOIN LATERAL
          aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) x WHERE c.oid=:oid
    """),
            {"oid": oid},
        )
        .scalars()
        .all()
    )
    columns = connection.execute(
        text("""
        SELECT 1 FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
        WHERE a.attrelid=:oid AND a.attnum>0 AND NOT a.attisdropped
    """),
        {"oid": oid},
    ).first()
    if columns or any(recipient not in allowed for recipient in recipients):
        raise RuntimeError("Unexpected ledger ACL; operator review required")
    roles = (
        connection.execute(
            text("""
        SELECT rolname FROM pg_roles WHERE rolname=ANY(:roles) ORDER BY rolname
    """),
            {"roles": list(LEDGER_ROLES)},
        )
        .scalars()
        .all()
    )
    if identity["owner"] in roles:
        raise RuntimeError("An application role must not own the ledger")
    # Identifiers come exclusively from the fixed allowlist, never user input.
    recipients_sql = ", ".join(["PUBLIC", *(f'"{role}"' for role in roles)])
    connection.execute(
        text(
            f"REVOKE ALL PRIVILEGES ON TABLE public.alembic_version FROM {recipients_sql} RESTRICT"
        )
    )
    privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
    if connection.dialect.server_version_info >= (17,):
        privileges.append("MAINTAIN")
    for role in roles:
        unsafe = connection.execute(
            text("""
            SELECT pg_has_role(:role, :owner, 'MEMBER')
              OR EXISTS (SELECT FROM unnest(CAST(:privileges AS text[])) p(priv)
                WHERE has_table_privilege(:role,:oid,p.priv))
              OR EXISTS (SELECT FROM unnest(ARRAY[
                'SELECT','INSERT','UPDATE','REFERENCES'
              ]) p(priv) WHERE has_any_column_privilege(:role,:oid,p.priv))
        """),
            {"role": role, "owner": identity["relowner"], "oid": oid, "privileges": privileges},
        ).scalar_one()
        if unsafe:
            raise RuntimeError("Application ledger access remains")
    after_acl = connection.execute(
        text("""
        SELECT x.grantor,x.grantee,x.privilege_type,x.is_grantable
        FROM pg_class c CROSS JOIN LATERAL
          aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) x
        WHERE c.oid=:oid ORDER BY x.grantee,x.privilege_type,x.grantor
    """),
        {"oid": oid},
    ).all()
    if after_acl != [row for row in owner_acl if row.grantee == identity["relowner"]]:
        raise RuntimeError("Unexpected remaining ledger ACL")
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        if not connection.execute(
            text("SELECT has_table_privilege(current_user,:oid,:p)"), {"oid": oid, "p": privilege}
        ).scalar_one():
            raise RuntimeError("Migration administrator lost ledger access")
