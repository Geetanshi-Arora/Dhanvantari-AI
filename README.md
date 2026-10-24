# Dhanvantari AI

A multilingual, nurse-assisted healthcare support platform for rural India.

**Phase 1: frontend foundation.** A responsive HTML, CSS, and vanilla JavaScript prototype in a light sky-blue, lavender, purple, and pink theme. No external APIs, real authentication, medical analysis, or voice services are connected. Use synthetic data only.

## Run locally

Python 3 is the only prerequisite for serving the frontend; there are no application dependencies to install.

```sh
cd /workspace/Dhanvantari-AI
python3 -m http.server 8000 --bind 0.0.0.0 --directory frontend
```

On your own computer, open `http://localhost:8000`. Serve over HTTP rather than opening HTML files directly, because JavaScript uses ES modules. In cloud onboarding, local HTTP requests can validate the site; no public preview is configured.

## Explore Phase 1

1. Open the landing page and select **Explore the demo**.
2. Choose **Nurse**, optionally enter a fictional display name, then enter the demo. No password is requested.
3. Use **Register demo patient**, choose a language preference, and continue.
4. Complete registration with fictional data and check the simulated consent box. Save to return to the directory.
5. Search by name or generated ID. Change the language through navigation. **Exit demo** clears this tab's demo state.
6. Choose **Doctor** at role selection to view its roadmap. Doctor authentication and dashboards are future features.

New registrations live only in this tab's sessionStorage. No server receives or persists them. Sample records are explicitly synthetic. Language choices demonstrate the interface and do not assert Gnani.ai language support. Browser session storage is required for saving demo selections.

## Files

```text
frontend/
  index.html                 Landing page
  login.html                 Nurse / doctor demo role selection
  language.html              Illustrative language preference
  dashboard.html             Nurse workspace and searchable sample directory
  register.html              Validated synthetic registration
  assets/
    favicon.svg              Local brand icon
    css/styles.css           Shared theme, components, responsive layouts
    js/app.js                Navigation and page interactions
    js/store.js              Tab-scoped demo data
backend/
  README.md                  Backend phase boundary
  app/README.md              Future FastAPI module boundary
database/migrations/README.md  Future Supabase migration boundary
docs/architecture.md        Full roadmap and safety boundaries
tests/smoke.cjs             Browser functional smoke test
```

See [architecture](docs/architecture.md) for the planned stack and phase boundaries. Backend implementation starts only after Phase 2 approval. Chart.js is reserved for Phase 9 analytics; it is not loaded unnecessarily in Phase 1.

## Validate

With the static server running, use the browser smoke test. It requires Node.js, Playwright, and Chromium as development tooling, not application dependencies. The cloud development image provides them. On another machine, install Playwright outside this checkout (for example, `npm install --prefix /tmp/dhanvantari-tools playwright` and `NODE_PATH=/tmp/dhanvantari-tools/node_modules node tests/smoke.cjs`; install Chromium using that Playwright CLI if needed).

```sh
node tests/smoke.cjs
```

The test covers page navigation, demo role selection, language selection, invalid/valid registration, search, reload persistence, logout clearing, mobile layout, and browser errors. `BASE_URL` can change the test target; `CHROMIUM_PATH` selects the browser executable (defaults to `/usr/bin/chromium`). Do not run these synthetic-data checks against future production systems.
