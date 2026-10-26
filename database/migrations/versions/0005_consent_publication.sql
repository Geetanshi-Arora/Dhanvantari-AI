-- Foundation ONLY. No publisher login/membership, public entry point, notice,
-- approval, activation, or retirement operation is created or performed here.
-- Apply only in Alembic's existing outer transaction after separate approval.
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname='dhanvantari_notice_publisher')
    OR NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_owner'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
      AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication)
    OR (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='app')
      IS DISTINCT FROM 'app_owner' THEN
    RAISE EXCEPTION USING MESSAGE='publication.invalid_configuration', ERRCODE='P0001';
  END IF;
  IF EXISTS (SELECT FROM app.consent_notice_versions WHERE accepts_new) THEN
    -- Never silently disable an existing registry or fabricate historical evidence.
    RAISE EXCEPTION USING MESSAGE='publication.active_registry_requires_review', ERRCODE='P0001';
  END IF;
END $$;

CREATE ROLE dhanvantari_notice_publisher NOLOGIN NOINHERIT NOSUPERUSER
  NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
-- Usage alone grants no table, sequence or function access. No membership grant.
GRANT USAGE ON SCHEMA app TO dhanvantari_notice_publisher;

CREATE TABLE app.consent_publication_releases (
  id uuid PRIMARY KEY,
  release_id varchar(80) NOT NULL UNIQUE
    CHECK (release_id ~ '^[A-Za-z0-9_-]{1,80}$'),
  manifest_digest bytea NOT NULL UNIQUE CHECK (octet_length(manifest_digest)=32),
  previous_manifest_digest bytea
    REFERENCES app.consent_publication_releases(manifest_digest) ON DELETE RESTRICT,
  canonical_manifest bytea NOT NULL CHECK (octet_length(canonical_manifest) BETWEEN 1 AND 2000000),
  canonical_evidence bytea NOT NULL CHECK (octet_length(canonical_evidence) BETWEEN 1 AND 2000000),
  evidence_bundle_digest bytea NOT NULL CHECK (octet_length(evidence_bundle_digest)=32),
  canonical_trust_policy bytea NOT NULL CHECK (octet_length(canonical_trust_policy) BETWEEN 1 AND 2000000),
  trust_policy_digest bytea NOT NULL CHECK (octet_length(trust_policy_digest)=32),
  canonical_reviewer_registry bytea NOT NULL
    CHECK (octet_length(canonical_reviewer_registry) BETWEEN 1 AND 2000000),
  reviewer_registry_digest bytea NOT NULL CHECK (octet_length(reviewer_registry_digest)=32),
  -- Temporary hard gate: a future reviewed migration must explicitly replace it.
  publication_enabled boolean NOT NULL DEFAULT false
    CONSTRAINT publication_0005_disabled CHECK (NOT publication_enabled),
  operator_session_user name NOT NULL,
  operator_role_oid oid NOT NULL,
  request_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL,
  CHECK (previous_manifest_digest IS NULL OR previous_manifest_digest<>manifest_digest),
  CHECK (manifest_digest=sha256(convert_to('dhanvantari.notice-manifest.v1','UTF8')
    ||decode('00','hex')||canonical_manifest)),
  -- These bundle digests are SHA-256 of the exact supplied byte bundles. Individual
  -- evidence signatures/digests retain the offline validator's separate domains.
  CHECK (evidence_bundle_digest=sha256(canonical_evidence)),
  CHECK (trust_policy_digest=sha256(canonical_trust_policy)),
  CHECK (reviewer_registry_digest=sha256(canonical_reviewer_registry))
);
-- One genesis and at most one successor: no silent release-chain forks.
CREATE UNIQUE INDEX consent_publication_one_genesis
  ON app.consent_publication_releases ((true)) WHERE previous_manifest_digest IS NULL;
CREATE UNIQUE INDEX consent_publication_one_successor
  ON app.consent_publication_releases(previous_manifest_digest)
  WHERE previous_manifest_digest IS NOT NULL;

CREATE TABLE app.consent_notice_bindings (
  purpose varchar(20) NOT NULL CHECK (purpose IN ('care','uploads','future_ai')),
  notice_version varchar(40) NOT NULL CHECK (length(notice_version) BETWEEN 1 AND 40),
  language varchar(16) NOT NULL CHECK (language IN ('en','hi','bn','ta','te','mr','gu','pa')),
  notice_text text NOT NULL
    CHECK (length(notice_text) BETWEEN 1 AND 65536 AND length(btrim(notice_text))>0),
  content_hash bytea NOT NULL CHECK (octet_length(content_hash)=32),
  canonical_artifact bytea NOT NULL CHECK (octet_length(canonical_artifact) BETWEEN 1 AND 2000000),
  artifact_digest bytea NOT NULL CHECK (octet_length(artifact_digest)=32),
  canonical_original_approval_evidence bytea NOT NULL
    CHECK (octet_length(canonical_original_approval_evidence) BETWEEN 1 AND 2000000),
  original_approval_evidence_digest bytea NOT NULL
    CHECK (octet_length(original_approval_evidence_digest)=32),
  first_release_id uuid NOT NULL
    REFERENCES app.consent_publication_releases(id) ON DELETE RESTRICT,
  PRIMARY KEY (purpose,notice_version,language),
  CHECK (content_hash=sha256(convert_to(notice_text,'UTF8'))),
  -- canonical_artifact is the existing offline artifact_digest() payload, which
  -- excludes lifecycle fields. Store bytes; never hash PostgreSQL jsonb::text.
  CHECK (artifact_digest=sha256(convert_to('dhanvantari.notice-artifact.v1','UTF8')
    ||decode('00','hex')||canonical_artifact)),
  CHECK (original_approval_evidence_digest=sha256(canonical_original_approval_evidence))
);

CREATE TABLE app.consent_publication_events (
  id uuid PRIMARY KEY,
  release_id uuid NOT NULL REFERENCES app.consent_publication_releases(id) ON DELETE RESTRICT,
  purpose varchar(20) NOT NULL,
  notice_version varchar(40) NOT NULL,
  language varchar(16) NOT NULL,
  event_type varchar(20) NOT NULL CHECK (event_type IN ('publication','retirement')),
  previous_event_id uuid,
  canonical_event bytea NOT NULL CHECK (octet_length(canonical_event) BETWEEN 1 AND 2000000),
  event_digest bytea NOT NULL CHECK (octet_length(event_digest)=32),
  operator_session_user name NOT NULL,
  operator_role_oid oid NOT NULL,
  request_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL,
  FOREIGN KEY (purpose,notice_version,language)
    REFERENCES app.consent_notice_bindings(purpose,notice_version,language) ON DELETE RESTRICT,
  UNIQUE (id,purpose,notice_version,language),
  FOREIGN KEY (previous_event_id,purpose,notice_version,language)
    REFERENCES app.consent_publication_events(id,purpose,notice_version,language) ON DELETE RESTRICT,
  UNIQUE (release_id,purpose,notice_version,language),
  UNIQUE (previous_event_id),
  CHECK (previous_event_id IS NULL OR previous_event_id<>id),
  CHECK (event_digest=sha256(canonical_event))
);
CREATE UNIQUE INDEX consent_publication_one_initial_event
  ON app.consent_publication_events(purpose,notice_version,language)
  WHERE previous_event_id IS NULL;

ALTER TABLE app.consent_publication_releases OWNER TO app_owner;
ALTER TABLE app.consent_notice_bindings OWNER TO app_owner;
ALTER TABLE app.consent_publication_events OWNER TO app_owner;
ALTER TABLE app.consent_publication_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.consent_notice_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.consent_publication_events ENABLE ROW LEVEL SECURITY;
-- No RLS policies: ordinary roles have no direct access, including the publisher.

-- Trigger only: this is NOT a publication or activation entry point. Capture the
-- authenticated DB login, not current_user (which may be a definer), nor a caller
-- supplied clinical actor claim. A shared DB login does not identify a human;
-- future operator provisioning must address that separately.
CREATE FUNCTION app.capture_consent_publication_operator() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
  NEW.operator_session_user := session_user;
  SELECT oid INTO STRICT NEW.operator_role_oid FROM pg_catalog.pg_roles WHERE rolname=session_user;
  NEW.recorded_at := clock_timestamp();
  RETURN NEW;
END $$;
ALTER FUNCTION app.capture_consent_publication_operator() OWNER TO app_owner;

CREATE TRIGGER capture_consent_publication_operator BEFORE INSERT ON app.consent_publication_releases
  FOR EACH ROW EXECUTE FUNCTION app.capture_consent_publication_operator();
CREATE TRIGGER capture_consent_publication_operator BEFORE INSERT ON app.consent_publication_events
  FOR EACH ROW EXECUTE FUNCTION app.capture_consent_publication_operator();

-- Reuse the existing fixed-error trigger; reject mutations even if an ordinary
-- write grant is accidentally broadened later. Owners/admins remain a trust boundary.
CREATE TRIGGER publication_release_immutable BEFORE UPDATE OR DELETE ON app.consent_publication_releases
  FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER publication_release_no_truncate BEFORE TRUNCATE ON app.consent_publication_releases
  FOR EACH STATEMENT EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER notice_binding_immutable BEFORE UPDATE OR DELETE ON app.consent_notice_bindings
  FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER notice_binding_no_truncate BEFORE TRUNCATE ON app.consent_notice_bindings
  FOR EACH STATEMENT EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER publication_event_immutable BEFORE UPDATE OR DELETE ON app.consent_publication_events
  FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER publication_event_no_truncate BEFORE TRUNCATE ON app.consent_publication_events
  FOR EACH STATEMENT EXECUTE FUNCTION app.audit_immutable();

-- Strip actual ACL recipients, including unexpected creator-default grants, from
-- NEW objects only. Supabase-managed defaults, roles and existing objects are untouched.
DO $$
DECLARE target text; recipient record;
BEGIN
  FOREACH target IN ARRAY ARRAY['consent_publication_releases','consent_notice_bindings',
    'consent_publication_events'] LOOP
    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE app.%I FROM PUBLIC RESTRICT',target);
    FOR recipient IN SELECT DISTINCT r.rolname FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace
      CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
      JOIN pg_roles r ON r.oid=a.grantee
      WHERE n.nspname='app' AND c.relname=target AND a.grantee<>c.relowner LOOP
      EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE app.%I FROM %I RESTRICT',target,recipient.rolname);
    END LOOP;
    IF EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
      WHERE n.nspname='app' AND c.relname=target AND a.grantee<>c.relowner) THEN
      RAISE EXCEPTION USING MESSAGE='publication.unexpected_privileges', ERRCODE='P0001';
    END IF;
  END LOOP;
  REVOKE ALL PRIVILEGES ON FUNCTION app.capture_consent_publication_operator() FROM PUBLIC RESTRICT;
  FOR recipient IN SELECT DISTINCT r.rolname FROM pg_proc p
    CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    JOIN pg_roles r ON r.oid=a.grantee
    WHERE p.oid='app.capture_consent_publication_operator()'::regprocedure AND a.grantee<>p.proowner LOOP
    EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION app.capture_consent_publication_operator() FROM %I RESTRICT',recipient.rolname);
  END LOOP;
  IF EXISTS (SELECT FROM pg_proc p
    CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid='app.capture_consent_publication_operator()'::regprocedure AND a.grantee<>p.proowner) THEN
    RAISE EXCEPTION USING MESSAGE='publication.unexpected_privileges', ERRCODE='P0001';
  END IF;
END $$;

-- Fail rather than accept pre-existing PUBLIC access to clinical/application
-- objects. Respect object types: privilege probes for sequences and relations
-- must never be interchanged (SQLSTATE 42809).
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_auth_members
      WHERE member='dhanvantari_notice_publisher'::regrole)
    OR EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='app' AND c.relkind IN ('r','p','v','m','f') AND (
        has_table_privilege('dhanvantari_notice_publisher',c.oid,
          'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_any_column_privilege('dhanvantari_notice_publisher',c.oid,
          'SELECT,INSERT,UPDATE,REFERENCES')))
    OR EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='app' AND c.relkind='S' AND has_sequence_privilege(
        'dhanvantari_notice_publisher',c.oid,'USAGE,SELECT,UPDATE'))
    OR EXISTS (SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='app' AND has_function_privilege(
        'dhanvantari_notice_publisher',p.oid,'EXECUTE'))
    OR has_schema_privilege('dhanvantari_notice_publisher','app','CREATE') THEN
    RAISE EXCEPTION USING MESSAGE='publication.unexpected_publisher_access', ERRCODE='P0001';
  END IF;
END $$;
-- Digest checks prove byte integrity only, not canonical JSON semantics, signature
-- validity, reviewer authority, release freshness or content availability. Future
-- separately reviewed functions must validate those bindings and audit atomically.
-- Existing notice acceptance, clinical RLS, revocation and replay ordering are
-- intentionally unchanged; no new structure can enable notice acceptance here.
