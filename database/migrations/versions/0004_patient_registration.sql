-- Installed only by a separately approved Alembic operation, in its outer transaction.
-- Runtime memberships, existing clinical policies and Supabase objects are unchanged.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_owner' AND NOT rolcanlogin
      AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole
      AND NOT rolreplication)
    OR NOT EXISTS (SELECT FROM pg_roles WHERE rolname='dhanvantari_clinical_actor'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
      AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_configuration', ERRCODE='P0001';
  END IF;
  IF EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='app' AND c.relkind IN ('r','p')
      AND (pg_get_userbyid(c.relowner)<>'app_owner' OR NOT c.relrowsecurity)) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_configuration', ERRCODE='P0001';
  END IF;
END $$;

-- Freeze consent DML until the migration commits: the history precheck and
-- trigger installation must inspect the same legacy state. Ordinary reads remain.
LOCK TABLE app.consents IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
  IF EXISTS (
    SELECT FROM (
      SELECT patient_id,purpose,dense_rank() OVER (
        PARTITION BY patient_id,purpose
        ORDER BY (revoked_at IS NULL) DESC,revoked_at DESC NULLS LAST,created_at DESC
      ) AS decision_rank
      FROM app.consents
    ) ranked WHERE decision_rank=1
    GROUP BY patient_id,purpose HAVING count(*)>1
  ) THEN
    -- A unique non-revoked row is authoritative even with tied creation times.
    -- Without one, indistinguishable most recently closed rows require operator
    -- review; do not pick a UUID or rewrite/delete any historical record.
    RAISE EXCEPTION USING MESSAGE='registration.ambiguous_history', ERRCODE='P0001';
  END IF;
END $$;

CREATE TABLE app.consent_notice_versions (
  purpose varchar(20) NOT NULL CHECK (purpose IN ('care','uploads','future_ai')),
  notice_version varchar(40) NOT NULL CHECK (length(notice_version) BETWEEN 1 AND 40),
  language varchar(16) NOT NULL CHECK (language IN ('en','hi','bn','ta','te','mr','gu','pa')),
  accepts_new boolean NOT NULL DEFAULT false,
  PRIMARY KEY (purpose,notice_version,language)
);
-- Deliberately no INSERTs: no production notice or translation is assumed approved.
CREATE TABLE app.patient_operation_keys (
  actor_id uuid NOT NULL REFERENCES app.nurses(id),
  operation varchar(20) NOT NULL CHECK (operation IN ('registration','consent','revocation')),
  key uuid NOT NULL,
  request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint)=32),
  result jsonb CHECK (result IS NULL OR jsonb_typeof(result)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (actor_id,operation,key)
);
ALTER TABLE app.consent_notice_versions OWNER TO app_owner;
ALTER TABLE app.patient_operation_keys OWNER TO app_owner;
ALTER TABLE app.consent_notice_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.patient_operation_keys ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.consent_notice_versions, app.patient_operation_keys
  FROM PUBLIC, anon, authenticated, service_role, dhanvantari_api, dhanvantari_clinical_actor;

-- Private helpers are not runtime entry points. Owner bypass of RLS is intentional
-- and is confined to these statically defined, explicitly authorizing functions.
CREATE FUNCTION app._registration_actor() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid;
BEGIN
  IF current_setting('transaction_isolation')<>'read committed' THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_state', ERRCODE='P0001';
  END IF;
  actor := app.actor_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='registration.forbidden', ERRCODE='P0001';
  END IF;
  PERFORM 1 FROM app.staff_profiles s JOIN app.nurses n ON n.id=s.id
    WHERE s.id=actor AND s.active AND s.role='nurse' FOR SHARE OF s,n;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING MESSAGE='registration.forbidden', ERRCODE='P0001';
  END IF;
  RETURN actor;
END $$;

CREATE FUNCTION app._registration_patient(target uuid, actor uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
  -- Reject unauthorized targets before waiting for a patient row lock. This
  -- initial snapshot is only a precheck; recheck and lock the assignment below.
  PERFORM 1 FROM app.patient_assignments
    WHERE patient_id=target AND staff_id=actor AND revoked_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING MESSAGE='registration.not_found', ERRCODE='P0001';
  END IF;
  -- Future API services must set bounded SET LOCAL lock_timeout and
  -- statement_timeout BEFORE invoking these functions, inside actor_session().
  -- Keep transactions short and READ COMMITTED. Future assignment/clinical writes
  -- must lock this patient first and then recheck consent/assignment.
  PERFORM 1 FROM app.patients WHERE id=target AND fictional AND age BETWEEN 18 AND 120
    FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING MESSAGE='registration.not_found', ERRCODE='P0001';
  END IF;
  PERFORM 1 FROM app.patient_assignments
    WHERE patient_id=target AND staff_id=actor AND revoked_at IS NULL FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING MESSAGE='registration.not_found', ERRCODE='P0001';
  END IF;
END $$;

CREATE FUNCTION app._registration_decision(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
  IF input IS NULL OR jsonb_typeof(input)<>'object'
    OR NOT input ?& ARRAY['purpose','granted','notice_version','language']
    OR (input - ARRAY['purpose','granted','notice_version','language']) <> '{}'::jsonb
    OR jsonb_typeof(input->'purpose')<>'string'
    OR jsonb_typeof(input->'granted')<>'boolean'
    OR jsonb_typeof(input->'notice_version')<>'string'
    OR jsonb_typeof(input->'language')<>'string'
    OR input->>'purpose' NOT IN ('care','uploads','future_ai')
    OR length(input->>'notice_version') NOT BETWEEN 1 AND 40
    OR input->>'language' NOT IN ('en','hi','bn','ta','te','mr','gu','pa') THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  -- Approval is checked separately after the idempotency lookup, so a completed
  -- retry remains available after notice retirement. New decisions always check.
  RETURN input;
END $$;

CREATE FUNCTION app._registration_notice(input jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
  PERFORM 1 FROM app.consent_notice_versions
    WHERE purpose=input->>'purpose' AND notice_version=input->>'notice_version'
      AND language=input->>'language' AND accepts_new FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING MESSAGE='registration.notice_not_approved', ERRCODE='P0001';
  END IF;
END $$;

-- Match Python str.strip() whitespace, without accepting whitespace-only names.
CREATE FUNCTION app._registration_trim(input text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
  SELECT btrim(input,U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')
$$;

CREATE FUNCTION app._registration_payload(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE field text; value text; decisions jsonb; care boolean; normalized jsonb;
BEGIN
  IF input IS NULL OR jsonb_typeof(input)<>'object'
    OR NOT input ?& ARRAY['name','age','preferred_language','consents']
    OR (input - ARRAY['name','age','preferred_language','consents','sex',
      'contact_reference','history','allergies','current_medicines']) <> '{}'::jsonb THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF jsonb_typeof(input->'name')<>'string' OR length(app._registration_trim(input->>'name')) NOT BETWEEN 1 AND 80
    OR jsonb_typeof(input->'age')<>'number' OR (input->>'age') !~ '^[0-9]+$'
    OR (input->>'age')::numeric NOT BETWEEN 18 AND 120
    OR jsonb_typeof(input->'preferred_language')<>'string'
    OR input->>'preferred_language' NOT IN ('en','hi','bn','ta','te','mr','gu','pa') THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  normalized := jsonb_build_object('name',app._registration_trim(input->>'name'),'age',(input->>'age')::integer,
    'preferred_language',input->>'preferred_language');
  FOREACH field IN ARRAY ARRAY['sex','contact_reference','history','allergies','current_medicines'] LOOP
    IF input ? field AND jsonb_typeof(input->field) NOT IN ('string','null') THEN
      RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
    END IF;
    value := nullif(app._registration_trim(input->>field),'');
    IF field='sex' AND jsonb_typeof(input->field)='string'
      AND input->>field NOT IN ('Female','Male','Intersex','Prefer not to say') THEN
      RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
    END IF;
    IF (field='sex' AND value IS NOT NULL
        AND value NOT IN ('Female','Male','Intersex','Prefer not to say'))
      OR length(value) > (CASE field WHEN 'sex' THEN 30 WHEN 'contact_reference' THEN 60
        WHEN 'history' THEN 1000 ELSE 500 END) THEN
      RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
    END IF;
    normalized := normalized || jsonb_build_object(field,value);
  END LOOP;
  IF jsonb_typeof(input->'consents')<>'array' THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  IF jsonb_array_length(input->'consents') NOT BETWEEN 1 AND 3 THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  SELECT jsonb_agg(app._registration_decision(d) ORDER BY d->>'purpose') INTO decisions
    FROM jsonb_array_elements(input->'consents') d;
  IF (SELECT count(DISTINCT d->>'purpose') FROM jsonb_array_elements(decisions) d)
      <> jsonb_array_length(decisions)
    OR NOT EXISTS (SELECT FROM jsonb_array_elements(decisions) d WHERE d->>'purpose'='care') THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  SELECT (d->>'granted')::boolean INTO care
    FROM jsonb_array_elements(decisions) d WHERE d->>'purpose'='care';
  IF NOT care AND (normalized->>'history' IS NOT NULL OR normalized->>'allergies' IS NOT NULL
    OR normalized->>'current_medicines' IS NOT NULL) THEN
    RAISE EXCEPTION USING MESSAGE='registration.care_required', ERRCODE='P0001';
  END IF;
  RETURN normalized || jsonb_build_object('consents',decisions);
END $$;

CREATE FUNCTION app._registration_claim(actor uuid, operation_name text, operation_key uuid,
  payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE fingerprint bytea; previous app.patient_operation_keys; inserted integer;
BEGIN
  IF operation_key IS NULL OR substring(operation_key::text,15,1)<>'4'
    OR substring(operation_key::text,20,1) NOT IN ('8','9','a','b') THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  fingerprint := sha256(convert_to(payload::text,'UTF8'));
  INSERT INTO app.patient_operation_keys(actor_id,operation,key,request_fingerprint)
    VALUES(actor,operation_name,operation_key,fingerprint) ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS inserted = ROW_COUNT;
  IF inserted=1 THEN RETURN NULL; END IF;
  SELECT * INTO STRICT previous FROM app.patient_operation_keys
    WHERE actor_id=actor AND operation=operation_name AND key=operation_key FOR UPDATE;
  IF previous.request_fingerprint<>fingerprint THEN
    RAISE EXCEPTION USING MESSAGE='registration.idempotency_conflict', ERRCODE='P0001';
  END IF;
  IF previous.result IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_state', ERRCODE='P0001';
  END IF;
  RETURN previous.result;
END $$;

CREATE FUNCTION app._registration_consent_json(consent_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT jsonb_build_object('id',c.id,'purpose',c.purpose,'granted',c.granted,
    'status',CASE WHEN c.revoked_at IS NOT NULL THEN 'revoked'
      WHEN c.granted THEN 'granted' ELSE 'declined' END,
    'notice_version',c.notice_version,'language',c.language,
    'created_at',c.created_at,'revoked_at',c.revoked_at)
    FROM app.consents c WHERE c.id=consent_id
$$;

-- Current decisions agree with has_care_consent(): a unique non-revoked
-- decision takes precedence over closed history, regardless of creation ties.
-- If none remains, order by closure then creation time, never by a random UUID.
CREATE FUNCTION app._registration_current_consent(target uuid, purpose_name text)
RETURNS app.consents
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE selected app.consents;
BEGIN
  SELECT c.* INTO STRICT selected FROM app.consents c
    WHERE c.patient_id=target AND c.purpose=purpose_name
    ORDER BY (c.revoked_at IS NULL) DESC,c.revoked_at DESC NULLS LAST,c.created_at DESC
    FETCH FIRST 1 ROW WITH TIES;
  RETURN selected;
EXCEPTION
  WHEN NO_DATA_FOUND THEN RETURN NULL;
  WHEN TOO_MANY_ROWS THEN
    RAISE EXCEPTION USING MESSAGE='registration.ambiguous_history', ERRCODE='P0001';
END $$;

CREATE FUNCTION app._registration_status(target uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT jsonb_build_object('id',p.id,'reference',p.reference,
    'preferred_language',p.preferred_language,'created_at',p.created_at,
    'care_access',CASE WHEN EXISTS (SELECT FROM app.consents c WHERE c.patient_id=p.id
      AND c.purpose='care' AND c.granted AND c.revoked_at IS NULL)
      THEN 'enabled' ELSE 'blocked' END,
    'consents',(SELECT jsonb_agg(app._registration_consent_json(latest.id) ORDER BY latest.purpose)
      FROM (SELECT DISTINCT c.purpose FROM app.consents c WHERE c.patient_id=p.id) purposes
      CROSS JOIN LATERAL app._registration_current_consent(p.id,purposes.purpose) latest))
    FROM app.patients p WHERE p.id=target
$$;

CREATE FUNCTION app._registration_audit(actor uuid, action_name text, target uuid,
  request uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
  INSERT INTO app.audit_logs(id,actor_id,action,outcome,resource_id,request_id)
    VALUES(gen_random_uuid(),actor,action_name,'success',target,request)
$$;

CREATE FUNCTION app.register_patient(input jsonb, operation_key uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; payload jsonb; replay jsonb; target uuid; decision jsonb; stamp timestamptz;
BEGIN
  IF request IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  payload := app._registration_payload(input);
  replay := app._registration_claim(actor,'registration',operation_key,payload);
  IF replay IS NOT NULL THEN
    target := (replay->>'patient_id')::uuid;
    PERFORM app._registration_patient(target,actor);
    RETURN app._registration_status(target);
  END IF;
  FOR decision IN SELECT * FROM jsonb_array_elements(payload->'consents') LOOP
    PERFORM app._registration_notice(decision);
  END LOOP;
  target := gen_random_uuid(); stamp := clock_timestamp();
  INSERT INTO app.patients(id,reference,name,age,age_recorded_on,sex,preferred_language,
    contact_reference,history,allergies,current_medicines,registered_by,fictional,created_at)
    VALUES(target,'DV-'||substr(replace(target::text,'-',''),1,27),payload->>'name',
      (payload->>'age')::integer,(stamp AT TIME ZONE 'UTC')::date,payload->>'sex',
      payload->>'preferred_language',payload->>'contact_reference',payload->>'history',
      payload->>'allergies',payload->>'current_medicines',actor,true,stamp);
  INSERT INTO app.patient_assignments(patient_id,staff_id,assigned_at) VALUES(target,actor,stamp);
  FOR decision IN SELECT * FROM jsonb_array_elements(payload->'consents') LOOP
    INSERT INTO app.consents(id,patient_id,purpose,notice_version,language,granted,
      recorded_by,representative,created_at)
      VALUES(gen_random_uuid(),target,decision->>'purpose',decision->>'notice_version',
        decision->>'language',(decision->>'granted')::boolean,actor,NULL,stamp);
    PERFORM app._registration_audit(actor,'consent.record.'||(decision->>'purpose'),target,request);
  END LOOP;
  PERFORM app._registration_audit(actor,'patient.register',target,request);
  UPDATE app.patient_operation_keys SET result=jsonb_build_object('patient_id',target)
    WHERE actor_id=actor AND operation='registration' AND key=operation_key;
  RETURN app._registration_status(target);
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE='P0001' AND SQLERRM IN (
    'registration.forbidden','registration.not_found','registration.invalid_input',
    'registration.notice_not_approved','registration.care_required',
    'registration.idempotency_conflict','registration.consent_conflict',
    'registration.invalid_state','registration.history_immutable',
    'registration.ambiguous_history') THEN
    RAISE;
  END IF;
  RAISE EXCEPTION USING MESSAGE='registration.operation_failed', ERRCODE='P0001';
END $$;

CREATE FUNCTION app.record_consent_decision(target uuid, input jsonb, expected_current_id uuid,
  operation_key uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; decision jsonb; replay jsonb; latest app.consents;
  result_row app.consents; stamp timestamptz;
BEGIN
  IF target IS NULL OR request IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor(); decision := app._registration_decision(input);
  replay := app._registration_claim(actor,'consent',operation_key,jsonb_build_object(
    'patient_id',target,'decision',decision,'expected_current_id',expected_current_id));
  PERFORM app._registration_patient(target,actor);
  IF replay IS NOT NULL THEN
    SELECT * INTO STRICT result_row
      FROM app._registration_current_consent(target,decision->>'purpose');
    IF result_row.id IS DISTINCT FROM (replay->>'consent_id')::uuid THEN
      RAISE EXCEPTION USING MESSAGE='registration.consent_conflict', ERRCODE='P0001';
    END IF;
  ELSE
    SELECT * INTO latest FROM app._registration_current_consent(target,decision->>'purpose');
    PERFORM 1 FROM app.consents WHERE id=latest.id FOR UPDATE;
    IF latest.id IS DISTINCT FROM expected_current_id THEN
      RAISE EXCEPTION USING MESSAGE='registration.consent_conflict', ERRCODE='P0001';
    END IF;
    PERFORM app._registration_notice(decision);
    -- Exceed all earlier timestamps, including legacy history and clock skew.
    SELECT greatest(clock_timestamp(),max(created_at)+interval '1 microsecond',
      max(revoked_at)+interval '1 microsecond') INTO stamp FROM app.consents
      WHERE patient_id=target AND purpose=decision->>'purpose';
    IF latest.id IS NOT NULL AND latest.revoked_at IS NULL THEN
      UPDATE app.consents SET revoked_at=stamp WHERE id=latest.id;
    END IF;
    INSERT INTO app.consents(id,patient_id,purpose,notice_version,language,granted,
      recorded_by,representative,created_at)
      VALUES(gen_random_uuid(),target,decision->>'purpose',decision->>'notice_version',
        decision->>'language',(decision->>'granted')::boolean,actor,NULL,stamp)
      RETURNING * INTO result_row;
    PERFORM app._registration_audit(actor,
      CASE WHEN latest.id IS NULL THEN 'consent.record.' ELSE 'consent.replace.' END
        ||(decision->>'purpose'),target,request);
    UPDATE app.patient_operation_keys SET result=jsonb_build_object('consent_id',result_row.id)
      WHERE actor_id=actor AND operation='consent' AND key=operation_key;
  END IF;
  RETURN jsonb_build_object('consent',app._registration_consent_json(result_row.id),
    'care_access',app._registration_status(target)->>'care_access');
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE='P0001' AND SQLERRM IN (
    'registration.forbidden','registration.not_found','registration.invalid_input',
    'registration.notice_not_approved','registration.care_required',
    'registration.idempotency_conflict','registration.consent_conflict',
    'registration.invalid_state','registration.history_immutable',
    'registration.ambiguous_history') THEN
    RAISE;
  END IF;
  RAISE EXCEPTION USING MESSAGE='registration.operation_failed', ERRCODE='P0001';
END $$;

CREATE FUNCTION app.revoke_consent(target uuid, consent_id uuid, purpose_name text,
  operation_key uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid; replay jsonb; latest app.consents; result_row app.consents; stamp timestamptz;
BEGIN
  IF target IS NULL OR consent_id IS NULL OR request IS NULL OR purpose_name IS NULL
    OR purpose_name NOT IN ('care','uploads','future_ai') THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  replay := app._registration_claim(actor,'revocation',operation_key,
    jsonb_build_object('patient_id',target,'consent_id',consent_id,'purpose',purpose_name));
  PERFORM app._registration_patient(target,actor);
  IF replay IS NOT NULL THEN
    SELECT * INTO STRICT result_row
      FROM app._registration_current_consent(target,purpose_name);
    IF result_row.id IS DISTINCT FROM (replay->>'consent_id')::uuid THEN
      RAISE EXCEPTION USING MESSAGE='registration.consent_conflict', ERRCODE='P0001';
    END IF;
  ELSE
    SELECT * INTO latest FROM app._registration_current_consent(target,purpose_name);
    PERFORM 1 FROM app.consents WHERE id=latest.id FOR UPDATE;
    IF latest.id IS DISTINCT FROM consent_id OR NOT latest.granted OR latest.revoked_at IS NOT NULL THEN
      RAISE EXCEPTION USING MESSAGE='registration.consent_conflict', ERRCODE='P0001';
    END IF;
    -- Make this closure unambiguously later than previously closed decisions.
    SELECT greatest(clock_timestamp(),max(created_at),max(revoked_at)+interval '1 microsecond')
      INTO stamp FROM app.consents WHERE patient_id=target AND purpose=purpose_name;
    UPDATE app.consents SET revoked_at=stamp WHERE id=consent_id RETURNING * INTO result_row;
    PERFORM app._registration_audit(actor,'consent.revoke.'||purpose_name,target,request);
    UPDATE app.patient_operation_keys SET result=jsonb_build_object('consent_id',result_row.id)
      WHERE actor_id=actor AND operation='revocation' AND key=operation_key;
  END IF;
  RETURN jsonb_build_object('consent',app._registration_consent_json(result_row.id),
    'care_access',app._registration_status(target)->>'care_access');
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE='P0001' AND SQLERRM IN (
    'registration.forbidden','registration.not_found','registration.invalid_input',
    'registration.notice_not_approved','registration.care_required',
    'registration.idempotency_conflict','registration.consent_conflict',
    'registration.invalid_state','registration.history_immutable',
    'registration.ambiguous_history') THEN
    RAISE;
  END IF;
  RAISE EXCEPTION USING MESSAGE='registration.operation_failed', ERRCODE='P0001';
END $$;

CREATE FUNCTION app.patient_registration_status(target uuid, request uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor uuid;
BEGIN
  IF target IS NULL OR request IS NULL THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_input', ERRCODE='P0001';
  END IF;
  actor := app._registration_actor();
  PERFORM app._registration_patient(target,actor);
  PERFORM app._registration_audit(actor,'patient.registration_status',target,request);
  RETURN app._registration_status(target);
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE='P0001' AND SQLERRM IN (
    'registration.forbidden','registration.not_found','registration.invalid_input',
    'registration.notice_not_approved','registration.care_required',
    'registration.idempotency_conflict','registration.consent_conflict',
    'registration.invalid_state','registration.history_immutable',
    'registration.ambiguous_history') THEN
    RAISE;
  END IF;
  RAISE EXCEPTION USING MESSAGE='registration.operation_failed', ERRCODE='P0001';
END $$;

CREATE FUNCTION app.protect_consent_history() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
  IF TG_OP='DELETE' THEN
    RAISE EXCEPTION USING MESSAGE='registration.history_immutable', ERRCODE='P0001';
  END IF;
  IF (to_jsonb(NEW)-'revoked_at') IS DISTINCT FROM (to_jsonb(OLD)-'revoked_at')
    OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
    OR NEW.revoked_at<OLD.created_at THEN
    RAISE EXCEPTION USING MESSAGE='registration.history_immutable', ERRCODE='P0001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER protect_consent_history BEFORE UPDATE OR DELETE ON app.consents
  FOR EACH ROW EXECUTE FUNCTION app.protect_consent_history();

-- Object-specific ACLs defeat any creator default grants without altering defaults.
-- Include the runtime LOGIN if present; it need not exist in disposable test DBs.
DO $$
DECLARE signature text; recipient text;
BEGIN
  FOREACH signature IN ARRAY ARRAY[
    'app._registration_actor()', 'app._registration_patient(uuid,uuid)',
    'app._registration_decision(jsonb)', 'app._registration_notice(jsonb)',
    'app._registration_trim(text)', 'app._registration_payload(jsonb)', 'app._registration_claim(uuid,text,uuid,jsonb)',
    'app._registration_consent_json(uuid)', 'app._registration_current_consent(uuid,text)',
    'app._registration_status(uuid)',
    'app._registration_audit(uuid,text,uuid,uuid)',
    'app.register_patient(jsonb,uuid,uuid)',
    'app.record_consent_decision(uuid,jsonb,uuid,uuid,uuid)',
    'app.revoke_consent(uuid,uuid,text,uuid,uuid)',
    'app.patient_registration_status(uuid,uuid)', 'app.protect_consent_history()'
  ] LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO app_owner',signature);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC RESTRICT',signature);
    FOR recipient IN SELECT rolname FROM pg_roles WHERE rolname IN (
      'anon','authenticated','service_role','dhanvantari_api',
      'dhanvantari_clinical_actor','dhanvantari_runtime') LOOP
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I RESTRICT',signature,recipient);
    END LOOP;
    IF EXISTS (SELECT FROM pg_proc p CROSS JOIN LATERAL
      aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
      WHERE p.oid=signature::regprocedure AND a.grantee<>p.proowner) THEN
      RAISE EXCEPTION USING MESSAGE='registration.invalid_configuration', ERRCODE='P0001';
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_roles WHERE rolname='dhanvantari_runtime') THEN
    REVOKE ALL ON app.consent_notice_versions, app.patient_operation_keys FROM dhanvantari_runtime;
  END IF;
  IF EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
    WHERE n.nspname='app' AND c.relname IN ('consent_notice_versions','patient_operation_keys')
      AND a.grantee<>c.relowner) THEN
    RAISE EXCEPTION USING MESSAGE='registration.invalid_configuration', ERRCODE='P0001';
  END IF;
END $$;
GRANT EXECUTE ON FUNCTION app.register_patient(jsonb,uuid,uuid),
  app.record_consent_decision(uuid,jsonb,uuid,uuid,uuid),
  app.revoke_consent(uuid,uuid,text,uuid,uuid), app.patient_registration_status(uuid,uuid)
  TO dhanvantari_clinical_actor;
