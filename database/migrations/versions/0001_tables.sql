-- Frozen DDL for revision 0001. Do not regenerate after application.

CREATE TABLE app.staff_profiles (
	id UUID NOT NULL,
	display_name VARCHAR(80) NOT NULL,
	role VARCHAR(10) NOT NULL,
	active BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (role IN ('nurse','doctor'))
)

;

CREATE TABLE app.login_throttles (
	key VARCHAR(64) NOT NULL,
	count INTEGER NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (key)
)

;
CREATE INDEX ix_app_login_throttles_expires_at ON app.login_throttles (expires_at);

CREATE TABLE app.nurses (
	id UUID NOT NULL,
	registration_number VARCHAR(100) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(id) REFERENCES app.staff_profiles (id),
	UNIQUE (registration_number)
)

;

CREATE TABLE app.doctors (
	id UUID NOT NULL,
	registration_number VARCHAR(100) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(id) REFERENCES app.staff_profiles (id),
	UNIQUE (registration_number)
)

;

CREATE TABLE app.auth_sessions (
	token_hash VARCHAR(64) NOT NULL,
	staff_id UUID NOT NULL,
	encrypted_access_token TEXT NOT NULL,
	csrf_hash VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (token_hash),
	FOREIGN KEY(staff_id) REFERENCES app.staff_profiles (id)
)

;
CREATE INDEX ix_app_auth_sessions_staff_id ON app.auth_sessions (staff_id);
CREATE INDEX ix_app_auth_sessions_expires_at ON app.auth_sessions (expires_at);

CREATE TABLE app.audit_logs (
	id UUID NOT NULL,
	actor_id UUID,
	action VARCHAR(80) NOT NULL,
	outcome VARCHAR(20) NOT NULL,
	resource_id UUID,
	request_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(actor_id) REFERENCES app.staff_profiles (id)
)

;

CREATE TABLE app.patients (
	reference VARCHAR(30) NOT NULL,
	name VARCHAR(80) NOT NULL,
	age INTEGER NOT NULL,
	age_recorded_on DATE NOT NULL,
	sex VARCHAR(30),
	preferred_language VARCHAR(16) NOT NULL,
	contact_reference VARCHAR(60),
	history TEXT,
	allergies TEXT,
	current_medicines TEXT,
	registered_by UUID NOT NULL,
	fictional BOOLEAN DEFAULT 'true' NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (age BETWEEN 0 AND 120),
	UNIQUE (reference),
	FOREIGN KEY(registered_by) REFERENCES app.nurses (id)
)

;

CREATE TABLE app.patient_assignments (
	patient_id UUID NOT NULL,
	staff_id UUID NOT NULL,
	assigned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (patient_id, staff_id),
	FOREIGN KEY(patient_id) REFERENCES app.patients (id),
	FOREIGN KEY(staff_id) REFERENCES app.staff_profiles (id)
)

;

CREATE TABLE app.consents (
	patient_id UUID NOT NULL,
	purpose VARCHAR(20) NOT NULL,
	notice_version VARCHAR(40) NOT NULL,
	language VARCHAR(16) NOT NULL,
	granted BOOLEAN NOT NULL,
	recorded_by UUID NOT NULL,
	representative VARCHAR(80),
	revoked_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (purpose IN ('care','uploads','future_ai')),
	FOREIGN KEY(patient_id) REFERENCES app.patients (id),
	FOREIGN KEY(recorded_by) REFERENCES app.nurses (id)
)

;
CREATE INDEX ix_app_consents_patient_id ON app.consents (patient_id);
CREATE UNIQUE INDEX uq_active_consent ON app.consents (patient_id, purpose) WHERE revoked_at IS NULL;

CREATE TABLE app.visits (
	patient_id UUID NOT NULL,
	nurse_id UUID NOT NULL,
	doctor_id UUID,
	visit_date DATE NOT NULL,
	language VARCHAR(16) NOT NULL,
	status VARCHAR(16) DEFAULT 'draft' NOT NULL,
	version INTEGER DEFAULT '1' NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (status IN ('draft','pending','approved','rejected','referred')),
	FOREIGN KEY(patient_id) REFERENCES app.patients (id),
	FOREIGN KEY(nurse_id) REFERENCES app.nurses (id),
	FOREIGN KEY(doctor_id) REFERENCES app.doctors (id)
)

;
CREATE INDEX ix_app_visits_doctor_id ON app.visits (doctor_id);
CREATE INDEX ix_app_visits_patient_id ON app.visits (patient_id);

CREATE TABLE app.symptoms (
	visit_id UUID NOT NULL,
	original_text TEXT NOT NULL,
	original_language VARCHAR(16) NOT NULL,
	translated_text TEXT,
	recorded_by UUID NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(visit_id) REFERENCES app.visits (id),
	FOREIGN KEY(recorded_by) REFERENCES app.nurses (id)
)

;
CREATE INDEX ix_app_symptoms_visit_id ON app.symptoms (visit_id);

CREATE TABLE app.vital_signs (
	visit_id UUID NOT NULL,
	measured_at TIMESTAMP WITH TIME ZONE NOT NULL,
	recorded_by UUID NOT NULL,
	temperature_c NUMERIC(4, 1),
	pulse_bpm INTEGER,
	systolic_mmhg INTEGER,
	diastolic_mmhg INTEGER,
	spo2_percent NUMERIC(5, 2),
	respiratory_rate INTEGER,
	weight_kg NUMERIC(5, 2),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (temperature_c BETWEEN 25 AND 45),
	CHECK (pulse_bpm BETWEEN 1 AND 300),
	CHECK (systolic_mmhg BETWEEN 1 AND 350),
	CHECK (diastolic_mmhg BETWEEN 1 AND 250),
	CHECK (spo2_percent BETWEEN 0 AND 100),
	CHECK (respiratory_rate BETWEEN 1 AND 100),
	CHECK (weight_kg > 0 AND weight_kg <= 700),
	CHECK (num_nonnulls(temperature_c,pulse_bpm,systolic_mmhg,diastolic_mmhg,spo2_percent,respiratory_rate,weight_kg) > 0),
	FOREIGN KEY(visit_id) REFERENCES app.visits (id),
	FOREIGN KEY(recorded_by) REFERENCES app.nurses (id)
)

;
CREATE INDEX ix_app_vital_signs_visit_id ON app.vital_signs (visit_id);

CREATE TABLE app.uploaded_files (
	visit_id UUID NOT NULL,
	storage_key TEXT NOT NULL,
	mime_type VARCHAR(80) NOT NULL,
	size_bytes INTEGER NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	uploaded_by UUID NOT NULL,
	status VARCHAR(16) DEFAULT 'quarantined' NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (size_bytes > 0 AND size_bytes <= 10485760),
	CHECK (status IN ('quarantined','clean','rejected')),
	FOREIGN KEY(visit_id) REFERENCES app.visits (id),
	UNIQUE (storage_key),
	FOREIGN KEY(uploaded_by) REFERENCES app.staff_profiles (id)
)

;
CREATE INDEX ix_app_uploaded_files_visit_id ON app.uploaded_files (visit_id);

CREATE TABLE app.ai_assessments (
	visit_id UUID NOT NULL,
	provider VARCHAR(80) NOT NULL,
	model_version VARCHAR(80) NOT NULL,
	suggestions JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(visit_id) REFERENCES app.visits (id)
)

;
CREATE INDEX ix_app_ai_assessments_visit_id ON app.ai_assessments (visit_id);

CREATE TABLE app.urgent_escalations (
	visit_id UUID NOT NULL,
	flagged_by UUID NOT NULL,
	reason TEXT NOT NULL,
	status VARCHAR(20) DEFAULT 'open' NOT NULL,
	acknowledged_by UUID,
	acknowledged_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (status IN ('open','acknowledged','resolved')),
	CHECK ((status = 'open' AND acknowledged_by IS NULL AND acknowledged_at IS NULL) OR (status <> 'open' AND acknowledged_by IS NOT NULL AND acknowledged_at IS NOT NULL)),
	FOREIGN KEY(visit_id) REFERENCES app.visits (id),
	FOREIGN KEY(flagged_by) REFERENCES app.nurses (id),
	FOREIGN KEY(acknowledged_by) REFERENCES app.doctors (id)
)

;
CREATE INDEX ix_app_urgent_escalations_visit_id ON app.urgent_escalations (visit_id);
CREATE INDEX ix_app_urgent_escalations_status ON app.urgent_escalations (status);

CREATE TABLE app.medical_reports (
	file_id UUID NOT NULL,
	description VARCHAR(500),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (file_id),
	FOREIGN KEY(file_id) REFERENCES app.uploaded_files (id)
)

;

CREATE TABLE app.uploaded_images (
	file_id UUID NOT NULL,
	description VARCHAR(500),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (file_id),
	FOREIGN KEY(file_id) REFERENCES app.uploaded_files (id)
)

;

CREATE TABLE app.clinician_decisions (
	visit_id UUID NOT NULL,
	doctor_id UUID NOT NULL,
	assessment_id UUID,
	rationale TEXT NOT NULL,
	treatment_instructions TEXT,
	status VARCHAR(16) NOT NULL,
	version INTEGER NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (status IN ('pending','approved','rejected','referred')),
	UNIQUE (visit_id, version),
	FOREIGN KEY(visit_id) REFERENCES app.visits (id),
	FOREIGN KEY(doctor_id) REFERENCES app.doctors (id),
	FOREIGN KEY(assessment_id) REFERENCES app.ai_assessments (id)
)

;
CREATE INDEX ix_app_clinician_decisions_visit_id ON app.clinician_decisions (visit_id);

CREATE TABLE app.prescriptions (
	decision_id UUID NOT NULL,
	doctor_id UUID NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(decision_id) REFERENCES app.clinician_decisions (id),
	FOREIGN KEY(doctor_id) REFERENCES app.doctors (id)
)

;
CREATE INDEX ix_app_prescriptions_decision_id ON app.prescriptions (decision_id);

CREATE TABLE app.prescription_items (
	prescription_id UUID NOT NULL,
	medication VARCHAR(200) NOT NULL,
	dose VARCHAR(100) NOT NULL,
	frequency VARCHAR(100) NOT NULL,
	duration VARCHAR(100) NOT NULL,
	instructions TEXT,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(prescription_id) REFERENCES app.prescriptions (id)
)

;
CREATE INDEX ix_app_prescription_items_prescription_id ON app.prescription_items (prescription_id);
