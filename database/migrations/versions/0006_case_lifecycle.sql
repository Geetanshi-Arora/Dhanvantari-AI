-- A visit is the durable case identifier. This migration adds only the case
-- lifecycle foundation; it does not grant doctor approval or clinical writes.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_owner' AND NOT rolcanlogin)
    OR NOT EXISTS (SELECT FROM pg_roles WHERE rolname='dhanvantari_clinical_actor'
      AND NOT rolcanlogin AND NOT rolbypassrls) THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_configuration', ERRCODE='P0001';
  END IF;
END $$;

ALTER TABLE app.visits ALTER COLUMN status TYPE varchar(40);
ALTER TABLE app.visits ALTER COLUMN status SET DEFAULT 'REGISTERED';
ALTER TABLE app.visits ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
DO $$ DECLARE item record; BEGIN
  FOR item IN SELECT conname FROM pg_constraint
    WHERE conrelid='app.visits'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) ILIKE '%status%' LOOP
    EXECUTE format('ALTER TABLE app.visits DROP CONSTRAINT %I',item.conname);
  END LOOP;
END $$;
UPDATE app.visits SET status=CASE status
  WHEN 'draft' THEN 'REGISTERED' WHEN 'pending' THEN 'READY_FOR_DOCTOR'
  WHEN 'approved' THEN 'APPROVED' WHEN 'rejected' THEN 'CLOSED'
  WHEN 'referred' THEN 'REFERRED' ELSE status END;
ALTER TABLE app.visits ADD CONSTRAINT ck_visits_case_status CHECK (status IN (
  'REGISTERED','INTAKE_IN_PROGRESS','TRANSCRIPT_PENDING_CONFIRMATION','SYMPTOM_REVIEW',
  'NURSE_CHECKS_PENDING','READY_FOR_DOCTOR','DOCTOR_REVIEW',
  'ADDITIONAL_INFORMATION_REQUIRED','PENDING_DOCTOR_APPROVAL','APPROVED','REFERRED',
  'FOLLOW_UP','CLOSED'));
CREATE OR REPLACE FUNCTION app._registration_status(target uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT jsonb_build_object('id',p.id,'reference',p.reference,
    'preferred_language',p.preferred_language,'created_at',p.created_at,
    'case_id',(SELECT v.id FROM app.visits v WHERE v.patient_id=p.id
      ORDER BY v.created_at DESC,v.id LIMIT 1),
    'care_access',CASE WHEN EXISTS (SELECT FROM app.consents c WHERE c.patient_id=p.id
      AND c.purpose='care' AND c.granted AND c.revoked_at IS NULL)
      THEN 'enabled' ELSE 'blocked' END,
    'consents',(SELECT jsonb_agg(app._registration_consent_json(latest.id) ORDER BY latest.purpose)
      FROM (SELECT DISTINCT c.purpose FROM app.consents c WHERE c.patient_id=p.id) purposes
      CROSS JOIN LATERAL app._registration_current_consent(p.id,purposes.purpose) latest))
    FROM app.patients p WHERE p.id=target
$$;
ALTER FUNCTION app._registration_status(uuid) OWNER TO app_owner;

CREATE TABLE app.case_status_history (
  id uuid PRIMARY KEY,
  visit_id uuid NOT NULL REFERENCES app.visits(id) ON DELETE RESTRICT,
  previous_status varchar(40),
  status varchar(40) NOT NULL CHECK (status IN (
    'REGISTERED','INTAKE_IN_PROGRESS','TRANSCRIPT_PENDING_CONFIRMATION','SYMPTOM_REVIEW',
    'NURSE_CHECKS_PENDING','READY_FOR_DOCTOR','DOCTOR_REVIEW',
    'ADDITIONAL_INFORMATION_REQUIRED','PENDING_DOCTOR_APPROVAL','APPROVED','REFERRED',
    'FOLLOW_UP','CLOSED')),
  changed_by uuid NOT NULL REFERENCES app.staff_profiles(id),
  request_id uuid NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (previous_status IS NULL OR previous_status IN (
    'REGISTERED','INTAKE_IN_PROGRESS','TRANSCRIPT_PENDING_CONFIRMATION','SYMPTOM_REVIEW',
    'NURSE_CHECKS_PENDING','READY_FOR_DOCTOR','DOCTOR_REVIEW',
    'ADDITIONAL_INFORMATION_REQUIRED','PENDING_DOCTOR_APPROVAL','APPROVED','REFERRED',
    'FOLLOW_UP','CLOSED'))
);
CREATE INDEX ix_case_status_history_visit_changed
  ON app.case_status_history(visit_id,changed_at,id);
ALTER TABLE app.case_status_history OWNER TO app_owner;
ALTER TABLE app.case_status_history ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.case_status_history FROM PUBLIC, anon, authenticated, service_role,
  dhanvantari_api, dhanvantari_clinical_actor;
GRANT SELECT ON app.case_status_history TO dhanvantari_clinical_actor;
CREATE POLICY case_status_history_read ON app.case_status_history
  FOR SELECT TO dhanvantari_clinical_actor USING (
    EXISTS (SELECT FROM app.visits v WHERE v.id=visit_id
      AND app.can_access_patient(v.patient_id)));

INSERT INTO app.visits(id,patient_id,nurse_id,doctor_id,visit_date,language,status,version,created_at)
  SELECT gen_random_uuid(),p.id,p.registered_by,NULL,(p.created_at AT TIME ZONE 'UTC')::date,
    p.preferred_language,'REGISTERED',1,p.created_at
  FROM app.patients p WHERE NOT EXISTS (
    SELECT FROM app.visits v WHERE v.patient_id=p.id);
CREATE TRIGGER case_status_history_immutable BEFORE UPDATE OR DELETE ON app.case_status_history
  FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER case_status_history_no_truncate BEFORE TRUNCATE ON app.case_status_history
  FOR EACH STATEMENT EXECUTE FUNCTION app.audit_immutable();

-- Preserve a history record for existing cases and ensure new registrations get
-- a case inside the same transaction as patient/consent creation.
INSERT INTO app.case_status_history(id,visit_id,previous_status,status,changed_by,request_id,changed_at)
  SELECT gen_random_uuid(),v.id,NULL,v.status,v.nurse_id,gen_random_uuid(),v.created_at
  FROM app.visits v WHERE NOT EXISTS (
    SELECT FROM app.case_status_history h WHERE h.visit_id=v.id);

-- Consent is always traceable to an immutable version/language identity. Draft
-- rows carry no invented content and are restricted to the existing synthetic
-- registration path. Published production notices continue using accepts_new.
ALTER TABLE app.consent_notice_versions ADD COLUMN development_only boolean NOT NULL DEFAULT false;
DO $$ DECLARE item record; BEGIN
  FOR item IN SELECT conname FROM pg_constraint
    WHERE conrelid='app.patient_operation_keys'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) ILIKE '%operation%' LOOP
    EXECUTE format('ALTER TABLE app.patient_operation_keys DROP CONSTRAINT %I',item.conname);
  END LOOP;
END $$;
ALTER TABLE app.patient_operation_keys ADD CONSTRAINT ck_patient_operation_name
  CHECK (operation IN ('registration','consent','revocation','case'));
CREATE TABLE app.consent_notice_drafts (
  purpose varchar(20) NOT NULL,
  notice_version varchar(40) NOT NULL,
  language varchar(16) NOT NULL,
  content text,
  content_state varchar(24) NOT NULL DEFAULT 'not_provided'
    CHECK (content_state IN ('not_provided','draft','published')),
  synthetic_only boolean NOT NULL DEFAULT true,
  PRIMARY KEY (purpose,notice_version,language),
  FOREIGN KEY (purpose,notice_version,language)
    REFERENCES app.consent_notice_versions(purpose,notice_version,language) ON DELETE RESTRICT,
  CHECK ((content_state='not_provided' AND content IS NULL) OR
         (content_state IN ('draft','published') AND content IS NOT NULL))
);
ALTER TABLE app.consent_notice_drafts OWNER TO app_owner;
ALTER TABLE app.consent_notice_drafts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.consent_notice_drafts FROM PUBLIC, anon, authenticated, service_role,
  dhanvantari_api, dhanvantari_clinical_actor;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_owner') THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_configuration', ERRCODE='P0001';
  END IF;
END $$;
INSERT INTO app.consent_notice_versions(purpose,notice_version,language,accepts_new,development_only)
  SELECT p.purpose,'DRAFT-SYNTHETIC-1',l.language,false,true
  FROM unnest(ARRAY['care','uploads','future_ai']::text[]) AS p(purpose)
  CROSS JOIN unnest(ARRAY['en','hi','bn','ta','te','mr','gu','pa']::text[]) AS l(language);
INSERT INTO app.consent_notice_drafts(purpose,notice_version,language,content,content_state,synthetic_only)
  SELECT p.purpose,'DRAFT-SYNTHETIC-1',l.language,NULL,'not_provided',true
  FROM unnest(ARRAY['care','uploads','future_ai']::text[]) AS p(purpose)
  CROSS JOIN unnest(ARRAY['en','hi','bn','ta','te','mr','gu','pa']::text[]) AS l(language);

CREATE OR REPLACE FUNCTION app._registration_notice(input jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE draft_only boolean;
BEGIN
  SELECT development_only INTO draft_only FROM app.consent_notice_versions
    WHERE purpose=input->>'purpose' AND notice_version=input->>'notice_version'
      AND language=input->>'language' AND (accepts_new OR development_only) FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING MESSAGE='registration.notice_not_approved', ERRCODE='P0001';
  END IF;
  -- Consent mutation also requires _registration_patient (fictional=true), and
  -- register_patient hard-codes fictional=true. Real-patient paths must use a
  -- separately reviewed gate that requires a published notice.
END $$;

CREATE FUNCTION app.initialize_registered_case() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE case_id uuid := gen_random_uuid();
BEGIN
  INSERT INTO app.visits(id,patient_id,nurse_id,doctor_id,visit_date,language,status,version,created_at)
    VALUES(case_id,NEW.id,NEW.registered_by,NULL,(NEW.created_at AT TIME ZONE 'UTC')::date,
      NEW.preferred_language,'REGISTERED',1,NEW.created_at);
  INSERT INTO app.case_status_history(id,visit_id,previous_status,status,changed_by,request_id,changed_at)
    VALUES(gen_random_uuid(),case_id,NULL,'REGISTERED',NEW.registered_by,
      COALESCE(NULLIF(current_setting('app.request_id',true),'')::uuid,gen_random_uuid()),NEW.created_at);
  RETURN NEW;
END $$;
ALTER FUNCTION app.initialize_registered_case() OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.initialize_registered_case() FROM PUBLIC, anon, authenticated,
  service_role, dhanvantari_api, dhanvantari_clinical_actor;
CREATE TRIGGER initialize_registered_case AFTER INSERT ON app.patients
  FOR EACH ROW EXECUTE FUNCTION app.initialize_registered_case();

CREATE FUNCTION app.transition_case(target uuid, next_status text, request uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; current_status text; patient uuid; worker uuid; stamp timestamptz;
BEGIN
  IF target IS NULL OR request IS NULL OR next_status IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  SELECT v.status,v.patient_id,v.nurse_id INTO current_status,patient,worker
    FROM app.visits v WHERE v.id=target FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION USING MESSAGE='case.not_found', ERRCODE='P0001'; END IF;
  PERFORM app._registration_patient(patient,actor);
  IF worker<>actor THEN RAISE EXCEPTION USING MESSAGE='case.forbidden', ERRCODE='P0001'; END IF;
  IF NOT ((current_status='REGISTERED' AND next_status='INTAKE_IN_PROGRESS') OR
    (current_status='INTAKE_IN_PROGRESS' AND next_status='TRANSCRIPT_PENDING_CONFIRMATION') OR
    (current_status='TRANSCRIPT_PENDING_CONFIRMATION' AND next_status='SYMPTOM_REVIEW') OR
    (current_status='SYMPTOM_REVIEW' AND next_status='NURSE_CHECKS_PENDING') OR
    (current_status='NURSE_CHECKS_PENDING' AND next_status='READY_FOR_DOCTOR') OR
    (current_status='ADDITIONAL_INFORMATION_REQUIRED' AND next_status='INTAKE_IN_PROGRESS')) THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_transition', ERRCODE='P0001';
  END IF;
  stamp := clock_timestamp();
  UPDATE app.visits SET status=next_status,version=version+1,updated_at=stamp WHERE id=target;
  INSERT INTO app.case_status_history(id,visit_id,previous_status,status,changed_by,request_id,changed_at)
    VALUES(gen_random_uuid(),target,current_status,next_status,actor,request,stamp);
  PERFORM app._registration_audit(actor,'case.status.'||lower(next_status),target,request);
END $$;
ALTER FUNCTION app.transition_case(uuid,text,uuid) OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.transition_case(uuid,text,uuid) FROM PUBLIC, anon, authenticated,
  service_role;
GRANT EXECUTE ON FUNCTION app.transition_case(uuid,text,uuid) TO dhanvantari_clinical_actor;

CREATE FUNCTION app.list_nurse_cases(search_text text, page_limit integer, after_id uuid,
  request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; result jsonb;
BEGIN
  IF request IS NULL OR page_limit NOT BETWEEN 1 AND 100 OR
     (search_text IS NOT NULL AND length(search_text)>80) THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  WITH selected AS (
    SELECT v.id,v.patient_id,p.reference AS patient_reference,p.name AS patient_name,
      p.preferred_language,v.nurse_id,v.doctor_id,v.status,v.created_at,v.updated_at
    FROM app.visits v JOIN app.patients p ON p.id=v.patient_id
    JOIN app.patient_assignments a ON a.patient_id=p.id AND a.staff_id=actor AND a.revoked_at IS NULL
    WHERE p.fictional AND p.age BETWEEN 18 AND 120
      AND (search_text IS NULL OR p.name ILIKE '%'||btrim(search_text)||'%'
        OR p.reference ILIKE '%'||btrim(search_text)||'%')
      AND (after_id IS NULL OR v.id>after_id)
    ORDER BY v.id LIMIT page_limit+1
  ), page AS (SELECT * FROM selected ORDER BY id LIMIT page_limit)
  SELECT jsonb_build_object('items',COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY id) FROM page),'[]'::jsonb),
    'next_cursor',CASE WHEN (SELECT count(*) FROM selected)>page_limit
      THEN (SELECT id::text FROM page ORDER BY id DESC LIMIT 1) ELSE NULL END)
    INTO result;
  PERFORM app._registration_audit(actor,'case.list',NULL,request);
  RETURN result;
END $$;
ALTER FUNCTION app.list_nurse_cases(text,integer,uuid,uuid) OWNER TO app_owner;

CREATE FUNCTION app.get_nurse_case(target uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; result jsonb;
BEGIN
  IF target IS NULL OR request IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  SELECT jsonb_build_object('id',v.id,'patient_id',v.patient_id,
    'patient_reference',p.reference,'patient_name',p.name,'preferred_language',p.preferred_language,
    'nurse_id',v.nurse_id,'doctor_id',v.doctor_id,'status',v.status,
    'created_at',v.created_at,'updated_at',v.updated_at)
    INTO result FROM app.visits v JOIN app.patients p ON p.id=v.patient_id
    JOIN app.patient_assignments a ON a.patient_id=p.id AND a.staff_id=actor AND a.revoked_at IS NULL
    WHERE v.id=target AND p.fictional AND p.age BETWEEN 18 AND 120;
  IF result IS NULL THEN RAISE EXCEPTION USING MESSAGE='case.not_found', ERRCODE='P0001'; END IF;
  PERFORM app._registration_audit(actor,'case.read',target,request);
  RETURN result;
END $$;
ALTER FUNCTION app.get_nurse_case(uuid,uuid) OWNER TO app_owner;

CREATE FUNCTION app.get_nurse_case_history(target uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; result jsonb;
BEGIN
  IF target IS NULL OR request IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  IF NOT EXISTS (SELECT FROM app.visits v JOIN app.patients p ON p.id=v.patient_id
      JOIN app.patient_assignments a ON a.patient_id=p.id AND a.staff_id=actor AND a.revoked_at IS NULL
      WHERE v.id=target AND p.fictional AND p.age BETWEEN 18 AND 120) THEN
    RAISE EXCEPTION USING MESSAGE='case.not_found', ERRCODE='P0001';
  END IF;
  SELECT jsonb_build_object('items',COALESCE(jsonb_agg(to_jsonb(history_row)
      ORDER BY history_row.changed_at,history_row.id),'[]'::jsonb)) INTO result
    FROM (SELECT h.id,h.visit_id AS case_id,h.previous_status,h.status,h.changed_by,h.changed_at
      FROM app.case_status_history h WHERE h.visit_id=target
      ORDER BY h.changed_at,h.id LIMIT 100) history_row;
  PERFORM app._registration_audit(actor,'case.history',target,request);
  RETURN result;
END $$;
ALTER FUNCTION app.get_nurse_case_history(uuid,uuid) OWNER TO app_owner;

REVOKE ALL ON FUNCTION app.list_nurse_cases(text,integer,uuid,uuid),
  app.get_nurse_case(uuid,uuid),app.get_nurse_case_history(uuid,uuid)
  FROM PUBLIC,anon,authenticated,service_role,dhanvantari_clinical_actor;
GRANT EXECUTE ON FUNCTION app.list_nurse_cases(text,integer,uuid,uuid),
  app.get_nurse_case(uuid,uuid),app.get_nurse_case_history(uuid,uuid)
  TO dhanvantari_clinical_actor;

CREATE FUNCTION app.create_patient_case(target uuid, requested_language text,
  operation_key uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; replay jsonb; selected_language text; case_id uuid; stamp timestamptz;
BEGIN
  IF target IS NULL OR operation_key IS NULL OR request IS NULL OR
     (requested_language IS NOT NULL AND requested_language NOT IN ('en','hi','bn','ta','te','mr','gu','pa')) THEN
    RAISE EXCEPTION USING MESSAGE='case.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  PERFORM app._registration_patient(target,actor);
  SELECT COALESCE(requested_language,p.preferred_language) INTO selected_language
    FROM app.patients p WHERE p.id=target;
  replay := app._registration_claim(actor,'case',operation_key,
    jsonb_build_object('patient_id',target,'language',selected_language));
  IF replay IS NOT NULL THEN
    case_id := (replay->>'case_id')::uuid;
    IF NOT EXISTS (SELECT FROM app.visits v WHERE v.id=case_id AND v.patient_id=target) THEN
      RAISE EXCEPTION USING MESSAGE='registration.idempotency_conflict', ERRCODE='P0001';
    END IF;
    RETURN jsonb_build_object('case_id',case_id,'patient_id',target,
      'status',(SELECT status FROM app.visits WHERE id=case_id),
      'created_at',(SELECT created_at FROM app.visits WHERE id=case_id));
  END IF;
  case_id := gen_random_uuid(); stamp := clock_timestamp();
  INSERT INTO app.visits(id,patient_id,nurse_id,doctor_id,visit_date,language,status,version,created_at,updated_at)
    VALUES(case_id,target,actor,NULL,(stamp AT TIME ZONE 'UTC')::date,selected_language,
      'REGISTERED',1,stamp,stamp);
  INSERT INTO app.case_status_history(id,visit_id,previous_status,status,changed_by,request_id,changed_at)
    VALUES(gen_random_uuid(),case_id,NULL,'REGISTERED',actor,request,stamp);
  PERFORM app._registration_audit(actor,'case.create',case_id,request);
  UPDATE app.patient_operation_keys SET result=jsonb_build_object('case_id',case_id)
    WHERE actor_id=actor AND operation='case' AND key=operation_key;
  RETURN jsonb_build_object('case_id',case_id,'patient_id',target,'status','REGISTERED','created_at',stamp);
END $$;
ALTER FUNCTION app.create_patient_case(uuid,text,uuid,uuid) OWNER TO app_owner;

CREATE FUNCTION app.update_patient_basic(target uuid, input jsonb, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; updated app.patients;
BEGIN
  IF target IS NULL OR request IS NULL OR input IS NULL OR jsonb_typeof(input)<>'object'
    OR input='{}'::jsonb OR (input-ARRAY['name','age','preferred_language','sex','contact_reference'])<>'{}'::jsonb THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF input ? 'name' AND (jsonb_typeof(input->'name')<>'string'
      OR length(app._registration_trim(input->>'name')) NOT BETWEEN 1 AND 80) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF input ? 'age' AND (jsonb_typeof(input->'age')<>'number'
      OR input->>'age' !~ '^(0|[1-9][0-9]*)$' OR length(input->>'age')>3
      OR (input->>'age')::integer NOT BETWEEN 18 AND 120) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF input ? 'preferred_language' AND (jsonb_typeof(input->'preferred_language')<>'string'
      OR input->>'preferred_language' NOT IN ('en','hi','bn','ta','te','mr','gu','pa')) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF input ? 'sex' AND input->'sex'<>'null'::jsonb AND (jsonb_typeof(input->'sex')<>'string'
      OR input->>'sex' NOT IN ('Female','Male','Intersex','Prefer not to say')) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF input ? 'contact_reference' AND input->'contact_reference'<>'null'::jsonb AND
      (jsonb_typeof(input->'contact_reference')<>'string'
       OR length(input->>'contact_reference')>60) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  PERFORM app._registration_patient(target,actor);
  UPDATE app.patients SET
    name=CASE WHEN input ? 'name' THEN app._registration_trim(input->>'name') ELSE name END,
    age=CASE WHEN input ? 'age' THEN (input->>'age')::integer ELSE age END,
    age_recorded_on=CASE WHEN input ? 'age' THEN (clock_timestamp() AT TIME ZONE 'UTC')::date ELSE age_recorded_on END,
    preferred_language=CASE WHEN input ? 'preferred_language' THEN input->>'preferred_language' ELSE preferred_language END,
    sex=CASE WHEN input ? 'sex' THEN input->>'sex' ELSE sex END,
    contact_reference=CASE WHEN input ? 'contact_reference' THEN NULLIF(app._registration_trim(input->>'contact_reference'),'') ELSE contact_reference END
    WHERE id=target RETURNING * INTO updated;
  PERFORM app._registration_audit(actor,'patient.update',target,request);
  RETURN jsonb_build_object('id',updated.id,'reference',updated.reference,'name',updated.name,
    'age',updated.age,'age_recorded_on',updated.age_recorded_on,'preferred_language',updated.preferred_language,
    'sex',updated.sex,'contact_reference',updated.contact_reference,'created_at',updated.created_at);
END $$;
ALTER FUNCTION app.update_patient_basic(uuid,jsonb,uuid) OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.create_patient_case(uuid,text,uuid,uuid),
  app.update_patient_basic(uuid,jsonb,uuid) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION app.create_patient_case(uuid,text,uuid,uuid),
  app.update_patient_basic(uuid,jsonb,uuid) TO dhanvantari_clinical_actor;

DO $$ BEGIN
  IF has_table_privilege('dhanvantari_clinical_actor','app.case_status_history',
       'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
    RAISE EXCEPTION USING MESSAGE='case.unexpected_privileges', ERRCODE='P0001';
  END IF;
END $$;
