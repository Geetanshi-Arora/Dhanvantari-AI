-- Run as a migration owner able to create roles. Never use this connection in FastAPI.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_owner') THEN
    CREATE ROLE app_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dhanvantari_api') THEN
    CREATE ROLE dhanvantari_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;
GRANT authenticated TO dhanvantari_api;
-- Required to transfer object ownership when the migration operator is not a superuser.
GRANT app_owner TO CURRENT_USER;
CREATE SCHEMA app AUTHORIZATION app_owner;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO authenticated, dhanvantari_api;
