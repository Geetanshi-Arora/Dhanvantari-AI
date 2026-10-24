-- Runtime never owns tables; clinical write grants arrive with tested workflows later.
DO $$ DECLARE t record; BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'app' LOOP
    EXECUTE format('ALTER TABLE app.%I OWNER TO app_owner', t.tablename);
    EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t.tablename);
    EXECUTE format('REVOKE ALL ON TABLE app.%I FROM PUBLIC, authenticated, dhanvantari_api', t.tablename);
  END LOOP;
END $$;

-- Helpers use fixed search_path. Definer is a NOLOGIN role; callers cannot set that role.
CREATE FUNCTION app.actor_id() RETURNS uuid LANGUAGE sql STABLE
SET search_path = pg_catalog AS $$
  SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
ALTER FUNCTION app.actor_id() OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.actor_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.actor_id() TO authenticated;

CREATE FUNCTION app.is_active_staff() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT EXISTS (SELECT 1 FROM app.staff_profiles WHERE id = app.actor_id() AND active)
$$;
ALTER FUNCTION app.is_active_staff() OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.is_active_staff() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.is_active_staff() TO authenticated;

CREATE FUNCTION app.can_access_patient(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT app.is_active_staff() AND EXISTS (
    SELECT 1 FROM app.patient_assignments
    WHERE patient_id = target AND staff_id = app.actor_id() AND revoked_at IS NULL
  )
$$;
ALTER FUNCTION app.can_access_patient(uuid) OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.can_access_patient(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.can_access_patient(uuid) TO authenticated;

CREATE FUNCTION app.has_care_consent(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT app.can_access_patient(target) AND EXISTS (
    SELECT 1 FROM app.consents WHERE patient_id = target AND purpose = 'care'
      AND granted AND revoked_at IS NULL
  )
$$;
ALTER FUNCTION app.has_care_consent(uuid) OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.has_care_consent(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.has_care_consent(uuid) TO authenticated;

CREATE FUNCTION app.can_access_visit(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT EXISTS (SELECT 1 FROM app.visits WHERE id = target AND app.has_care_consent(patient_id))
$$;
ALTER FUNCTION app.can_access_visit(uuid) OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.can_access_visit(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.can_access_visit(uuid) TO authenticated;

GRANT SELECT ON app.staff_profiles, app.nurses, app.doctors TO authenticated;
CREATE POLICY staff_self ON app.staff_profiles FOR SELECT TO authenticated
  USING (id = app.actor_id());
CREATE POLICY nurse_self ON app.nurses FOR SELECT TO authenticated
  USING (id = app.actor_id() AND app.is_active_staff());
CREATE POLICY doctor_self ON app.doctors FOR SELECT TO authenticated
  USING (id = app.actor_id() AND app.is_active_staff());

GRANT SELECT, INSERT, DELETE ON app.auth_sessions TO dhanvantari_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.login_throttles TO dhanvantari_api;
GRANT INSERT ON app.audit_logs TO dhanvantari_api;
CREATE POLICY backend_sessions ON app.auth_sessions TO dhanvantari_api USING (true) WITH CHECK (true);
CREATE POLICY backend_throttle ON app.login_throttles TO dhanvantari_api USING (true) WITH CHECK (true);
CREATE POLICY backend_audit ON app.audit_logs FOR INSERT TO dhanvantari_api WITH CHECK (true);

-- Audit history cannot be changed by the web runtime, even if grants are broadened accidentally.
CREATE FUNCTION app.audit_immutable() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog AS $$ BEGIN
  RAISE EXCEPTION 'Audit history is append-only';
END $$;
REVOKE ALL ON FUNCTION app.audit_immutable() FROM PUBLIC;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON app.audit_logs
  FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();

GRANT SELECT ON app.patients, app.patient_assignments, app.consents, app.visits,
  app.symptoms, app.vital_signs, app.ai_assessments, app.clinician_decisions,
  app.prescriptions, app.prescription_items, app.urgent_escalations TO authenticated;
CREATE POLICY patient_read ON app.patients FOR SELECT TO authenticated
  USING (app.can_access_patient(id));
CREATE POLICY assignment_read ON app.patient_assignments FOR SELECT TO authenticated
  USING (staff_id = app.actor_id() AND app.is_active_staff());
CREATE POLICY consent_read ON app.consents FOR SELECT TO authenticated
  USING (app.can_access_patient(patient_id));
CREATE POLICY visit_read ON app.visits FOR SELECT TO authenticated
  USING (app.has_care_consent(patient_id));
CREATE POLICY symptom_read ON app.symptoms FOR SELECT TO authenticated
  USING (app.can_access_visit(visit_id));
CREATE POLICY vitals_read ON app.vital_signs FOR SELECT TO authenticated
  USING (app.can_access_visit(visit_id));
CREATE POLICY assessment_read ON app.ai_assessments FOR SELECT TO authenticated
  USING (app.can_access_visit(visit_id));
CREATE POLICY decision_read ON app.clinician_decisions FOR SELECT TO authenticated
  USING (app.can_access_visit(visit_id));
CREATE POLICY prescription_read ON app.prescriptions FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM app.clinician_decisions d
                WHERE d.id = decision_id AND d.status = 'approved'));
CREATE POLICY prescription_item_read ON app.prescription_items FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM app.prescriptions p WHERE p.id = prescription_id));
CREATE POLICY escalation_read ON app.urgent_escalations FOR SELECT TO authenticated
  USING (app.can_access_visit(visit_id));
-- Upload metadata has no grants or policies yet: quarantined files are inaccessible.

-- Registration profiles must match their server-controlled role.
CREATE FUNCTION app.check_profession() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path = pg_catalog AS $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM app.staff_profiles
                 WHERE id = NEW.id AND role = TG_ARGV[0]) THEN
    RAISE EXCEPTION 'Professional profile role mismatch';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION app.check_profession() OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.check_profession() FROM PUBLIC;
CREATE TRIGGER nurse_role BEFORE INSERT OR UPDATE ON app.nurses
  FOR EACH ROW EXECUTE FUNCTION app.check_profession('nurse');
CREATE TRIGGER doctor_role BEFORE INSERT OR UPDATE ON app.doctors
  FOR EACH ROW EXECUTE FUNCTION app.check_profession('doctor');

CREATE FUNCTION app.check_prescription() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path = pg_catalog AS $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM app.clinician_decisions d
                 JOIN app.staff_profiles s ON s.id = d.doctor_id
                 WHERE d.id = NEW.decision_id AND d.status = 'approved'
                   AND d.doctor_id = NEW.doctor_id AND s.active AND s.role = 'doctor') THEN
    RAISE EXCEPTION 'Prescription requires an approved decision by an active doctor';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION app.check_prescription() OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.check_prescription() FROM PUBLIC;
CREATE TRIGGER prescription_approval BEFORE INSERT OR UPDATE ON app.prescriptions
  FOR EACH ROW EXECUTE FUNCTION app.check_prescription();
