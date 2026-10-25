-- No login or Supabase-managed object is created or changed here.
DO $$
DECLARE target record;
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'dhanvantari_clinical_actor') THEN
    RAISE EXCEPTION 'Unexpected clinical actor role already exists';
  END IF;
  IF (SELECT count(*) FROM pg_roles WHERE rolname IN ('app_owner','dhanvantari_api')
      AND NOT (rolcanlogin OR NOT rolinherit OR rolsuper OR rolbypassrls OR rolcreatedb
               OR rolcreaterole OR rolreplication)) <> 2 THEN
    RAISE EXCEPTION 'Unexpected application role attributes';
  END IF;
  IF EXISTS (SELECT FROM pg_roles WHERE
      pg_has_role('dhanvantari_api', oid, 'MEMBER')
      AND rolname NOT IN ('dhanvantari_api','authenticated'))
    OR NOT pg_has_role('dhanvantari_api','authenticated','MEMBER')
    OR EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
      WHERE r.rolname='dhanvantari_api' AND m.admin_option) THEN
    RAISE EXCEPTION 'Unexpected backend memberships';
  END IF;
  IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='app')
      IS DISTINCT FROM 'app_owner'
    OR (SELECT count(*) FROM pg_tables WHERE schemaname='app') <> 20 THEN
    RAISE EXCEPTION 'Unexpected application schema';
  END IF;
  FOR target IN SELECT c.relowner,c.relrowsecurity FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind IN ('r','p') LOOP
    IF pg_get_userbyid(target.relowner) <> 'app_owner' OR NOT target.relrowsecurity THEN
      RAISE EXCEPTION 'Unexpected table ownership or RLS';
    END IF;
  END LOOP;
END $$;

CREATE ROLE dhanvantari_clinical_actor NOLOGIN INHERIT NOSUPERUSER
  NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT USAGE ON SCHEMA app TO dhanvantari_clinical_actor;
GRANT SELECT ON app.staff_profiles, app.nurses, app.doctors,
  app.patients, app.patient_assignments, app.consents, app.visits,
  app.symptoms, app.vital_signs, app.ai_assessments, app.clinician_decisions,
  app.prescriptions, app.prescription_items, app.urgent_escalations
  TO dhanvantari_clinical_actor;
GRANT EXECUTE ON FUNCTION app.actor_id(), app.is_active_staff(),
  app.can_access_patient(uuid), app.has_care_consent(uuid), app.can_access_visit(uuid)
  TO dhanvantari_clinical_actor;

-- ALTER POLICY changes only its target role, retaining reviewed expressions.
DO $$
DECLARE target record; old_actor oid;
BEGIN
  SELECT oid INTO old_actor FROM pg_roles WHERE rolname='authenticated';
  FOR target IN SELECT * FROM (VALUES
    ('staff_profiles','staff_self'), ('nurses','nurse_self'), ('doctors','doctor_self'),
    ('patients','patient_read'), ('patient_assignments','assignment_read'),
    ('consents','consent_read'), ('visits','visit_read'), ('symptoms','symptom_read'),
    ('vital_signs','vitals_read'), ('ai_assessments','assessment_read'),
    ('clinician_decisions','decision_read'), ('prescriptions','prescription_read'),
    ('prescription_items','prescription_item_read'), ('urgent_escalations','escalation_read')
  ) AS expected(table_name,policy_name) LOOP
    IF NOT EXISTS (SELECT FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='app'
      AND c.relname=target.table_name AND p.polname=target.policy_name
      AND p.polcmd='r' AND p.polroles=ARRAY[old_actor]) THEN
      RAISE EXCEPTION 'Unexpected clinician policy';
    END IF;
    EXECUTE format('ALTER POLICY %I ON app.%I TO dhanvantari_clinical_actor',
      target.policy_name,target.table_name);
  END LOOP;
  IF EXISTS (SELECT FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='app'
    AND old_actor=ANY(p.polroles)) THEN
    RAISE EXCEPTION 'Unexpected remaining shared-role policy';
  END IF;
END $$;

GRANT dhanvantari_clinical_actor TO dhanvantari_api;
REVOKE authenticated FROM dhanvantari_api RESTRICT;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA app FROM authenticated RESTRICT;
REVOKE EXECUTE ON FUNCTION app.actor_id(), app.is_active_staff(),
  app.can_access_patient(uuid), app.has_care_consent(uuid), app.can_access_visit(uuid)
  FROM authenticated RESTRICT;
REVOKE USAGE ON SCHEMA app FROM authenticated RESTRICT;
