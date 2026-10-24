-- Patients include clinical history. Deny the entire row without active care consent.
-- Future registration metadata access must use a separate narrowly scoped interface.
DROP POLICY patient_read ON app.patients;
CREATE POLICY patient_read ON app.patients FOR SELECT TO authenticated
  USING (app.has_care_consent(id));

CREATE FUNCTION app.validate_decision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE target_patient uuid; assigned_doctor uuid;
BEGIN
  SELECT patient_id, doctor_id INTO target_patient, assigned_doctor
    FROM app.visits WHERE id = NEW.visit_id FOR UPDATE;
  IF NOT FOUND OR assigned_doctor IS DISTINCT FROM NEW.doctor_id THEN
    RAISE EXCEPTION 'Decision requires the assigned visit doctor';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM app.staff_profiles s JOIN app.doctors d ON d.id=s.id
    WHERE s.id=NEW.doctor_id AND s.active AND s.role='doctor')
    OR NOT EXISTS (SELECT 1 FROM app.patient_assignments WHERE patient_id=target_patient
      AND staff_id=NEW.doctor_id AND revoked_at IS NULL)
    OR NOT EXISTS (SELECT 1 FROM app.consents WHERE patient_id=target_patient
      AND purpose='care' AND granted AND revoked_at IS NULL) THEN
    RAISE EXCEPTION 'Decision requires active doctor authorization and care consent';
  END IF;
  -- Application actors cannot write on behalf of another doctor. Administrative
  -- fixtures have no actor claim; no application write grant is introduced here.
  IF app.actor_id() IS NOT NULL AND app.actor_id() <> NEW.doctor_id THEN
    RAISE EXCEPTION 'Decision actor must be the assigned doctor';
  END IF;
  IF NEW.assessment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM app.ai_assessments WHERE id=NEW.assessment_id AND visit_id=NEW.visit_id
  ) THEN
    RAISE EXCEPTION 'Assessment must belong to the decision visit';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION app.validate_decision() OWNER TO app_owner;
REVOKE ALL ON FUNCTION app.validate_decision() FROM PUBLIC;
CREATE TRIGGER validate_decision BEFORE INSERT OR UPDATE ON app.clinician_decisions
  FOR EACH ROW EXECUTE FUNCTION app.validate_decision();

-- Prevent referenced evidence from being moved to a different visit after validation.
CREATE FUNCTION app.assessment_visit_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
  IF NEW.visit_id IS DISTINCT FROM OLD.visit_id THEN
    RAISE EXCEPTION 'Assessment visit is immutable';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION app.assessment_visit_immutable() FROM PUBLIC;
CREATE TRIGGER assessment_visit_immutable BEFORE UPDATE ON app.ai_assessments
  FOR EACH ROW EXECUTE FUNCTION app.assessment_visit_immutable();
