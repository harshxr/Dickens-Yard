# Render deployment preparation

This document describes the existing FastAPI application. It does not create a
Render service or database and does not apply database migrations.

## Service configuration

- **Root directory:** `voice-agent-platform/backend`
- **Python:** `.python-version` pins Python `3.12.12`, matching the version
  used to generate and clean-install test the lock.
- **Build command:** `pip install -r requirements-lock-py312.txt`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Liveness path:** `/health`
- **Readiness path:** `/ready`

Run these commands from the backend root. The module imports are top-level
(`main`, `database`, `models`, `calendar_service`, and `schemas`), so a different
working directory would require a different import path. The current
`requirements.txt` remains the unpinned dependency input. The deployment lock
pins direct and transitive dependencies in `requirements-lock-py312.txt` from
the passing Python 3.12.12 environment. Re-freeze only after intentional
dependency review and a successful clean installation and regression run.
The backend has no `pyproject.toml` or `uv.lock`.

`/health` returns `{"status":"healthy"}` without checking dependencies; use it
for process liveness. `/ready` executes only `SELECT 1` on PostgreSQL. It does
not create tables, modify records, or call Google Calendar. It returns 503 with
a generic message when PostgreSQL cannot be reached. Use `/ready` as Render's
HTTP health-check path when database connectivity is required before traffic.

## Environment variables

Required for the backend to import and connect to PostgreSQL:
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

Optional database transport and timeout settings:
`DB_SSLMODE` (falls back to `PGSSLMODE`), `DB_CONNECT_TIMEOUT` (falls back to
`PGCONNECT_TIMEOUT`, default 10 seconds), and `DB_POOL_TIMEOUT` (default 10
seconds). The two timeouts must be 1–60 seconds.

Install the pinned set and check dependency consistency with:

```powershell
python -m pip install -r requirements-lock-py312.txt
python -m pip check
```

The lock was clean-installed in a separate Python 3.12.12 environment. The
tested key versions are FastAPI 0.162.0, Uvicorn 0.54.0, SQLAlchemy 2.1.6,
psycopg 3.3.6, Google API client 2.201.0, cryptography 50.0.2, and
`livekit-api` 1.2.1. The lock also pins Uvicorn's Linux-only `uvloop` extra
with a platform marker; its CPython 3.12 manylinux wheel was verified
available. `pip check` reported no broken requirements. The clean
environment passed all 74 tests, including 11 isolated PostgreSQL integration
tests, and local Uvicorn checks returned `/health` 200, `/ready` 200 with the
test database, and `/ready` 503 with an unreachable local database. These are
local validation results, not a Render deployment or a production database
check.

Required for appointment Calendar access: `CALENDAR_DEMO_BUSINESS_ID` and
`CALENDAR_ENCRYPTION_KEY`. Required only for local OAuth-management routes:
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`,
`OAUTH_STATE_SECRET`, and `CALENDAR_LOCAL_DEV_ONLY`. Required for LiveKit webhook
verification: `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`; `LIVEKIT_URL`,
`LIVEKIT_AGENT_NAME`, and `LIVEKIT_OUTBOUND_TRUNK_ID` are used by optional
LiveKit/outbound features. Keep `OUTBOUND_CALLS_ENABLED=false` and
`AUTO_CREATE_TABLES=false`. The agent separately needs `BACKEND_URL` and the
same `CALENDAR_DEMO_BUSINESS_ID`; set `BACKEND_URL` to the eventual Render HTTPS
service URL only after it exists. See `.env.example` for names and placeholders.

The outbound-call endpoint is an internal server-to-server interface, not a
browser endpoint. It requires an `Authorization: Bearer ...` header using
`OUTBOUND_CALL_API_TOKEN` (minimum 32 characters); query-string credentials
are ignored. Its payload must include the configured `OUTBOUND_CALL_BUSINESS_ID`,
a valid lead ID, call purpose, E.164 destination and allowlisted E.164 caller ID,
and a consent attestation reference/source. The authenticated caller is trusted
to verify the underlying consent record; `consent=true` alone is not proof.
Business approval is required to define trusted callers, evidence retention,
per-business limits, simultaneous-call limits, caller-ID authorization, and any
applicable consent policy. Do not put the bearer token in a browser or public
client.

Keep `OUTBOUND_CALLS_ENABLED=false` except for a separately approved controlled
test. `OUTBOUND_CALL_TEST_MODE=true` is validation-only: it requires destination
numbers in `OUTBOUND_TEST_ALLOWED_NUMBERS`, conflicts with enabled calling, and
does not create database rows or contact LiveKit. Set
`OUTBOUND_MAX_CALLS_PER_BUSINESS_PER_HOUR` and
`OUTBOUND_MAX_CONCURRENT_CALLS_PER_BUSINESS` conservatively. Runtime rate limits
and idempotency use PostgreSQL transaction advisory locks and the additive
`2026_11_02_outbound_call_safeguards.psql` migration; review and test that
migration against a restored database before any production application.

## PostgreSQL configuration

The application currently reads `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and
`DB_PASSWORD`, then builds a `postgresql+psycopg` SQLAlchemy URL. It does not
consume Render's `DATABASE_URL` variable directly. Populate the five `DB_*`
variables from the Render Postgres internal connection details. Put the
service and database in the same region and prefer the internal host and port.

For an external Postgres connection, set `DB_SSLMODE=require`; TLS is required
for Render external connections. `DB_SSLMODE` accepts PostgreSQL SSL modes and
falls back to `PGSSLMODE` when unset. Internal connections can use the
provider's internal host and port; set SSL mode according to the selected
database's connection instructions. `DB_CONNECT_TIMEOUT` (or `PGCONNECT_TIMEOUT`
as a fallback) bounds a new connection attempt to 1–60 seconds, default 10.
`DB_POOL_TIMEOUT` bounds a pool checkout to 1–60 seconds, default 10. The
engine also uses `pool_pre_ping=True`. Credentials are assembled into a SQLAlchemy
URL object and are not logged by application code.

Appointment `start_at` and `end_at` use timezone-aware SQLAlchemy columns.
HTTP timestamps must include an offset and are normalized to UTC. Opening
hours remain 09:00–17:00 fixed UTC year-round. The Phase 4 migration is a
separate, reviewed database operation; Render startup must not be used to apply
it. Verify the existing production column types and migration status before a
deployment window.

## Schema initialization

Application import previously called `Base.metadata.create_all()`. This could
create missing tables during a service start, while it could not update columns
on existing tables or apply the Phase 4 migration. It is now opt-in through
`AUTO_CREATE_TABLES=true` for disposable local development only. The default is
false; leave it unset or false on Render. Production schema changes must use
the reviewed migration procedure.

## Phase 4 migration preflight (read-only)

Before applying `migrations/2026_11_01_booking_idempotency.psql`, use a
read-only session against the intended database to verify:

1. Inspect the appointment timestamp column types:

   ```sql
   SELECT column_name, data_type, datetime_precision
   FROM information_schema.columns
   WHERE table_schema = current_schema()
     AND table_name = 'appointments'
     AND column_name IN ('start_at', 'end_at');
   ```

   Both should be `timestamp with time zone`. Confirm representative stored
   instants against known UTC timestamps; do not infer timezone semantics from
   displayed wall-clock values alone.
2. Record the existing row count:

   ```sql
   SELECT count(*) AS appointment_count FROM appointments;
   ```

   Compare it after migration. The migration adds booking fields and does not
   delete appointment rows.
3. Check for duplicate non-null `(business_id, idempotency_key)` pairs before
   creating the unique index:

   ```sql
   SELECT business_id, idempotency_key, count(*)
   FROM appointments
   WHERE idempotency_key IS NOT NULL
   GROUP BY business_id, idempotency_key
   HAVING count(*) > 1;
   ```

4. The migration's regular `CREATE UNIQUE INDEX` is not concurrent and can
   block writes while it builds. Schedule a maintenance window based on table
   size and staging timings. Do not substitute `CREATE INDEX CONCURRENTLY`
   without reviewing transaction behavior.
5. Take a verified logical backup and confirm a restore procedure before any
   migration. Record the backup identifier and restore point.
6. There is no down migration. Do not drop new columns/index as a routine
   rollback. If a migration partially fails, inspect each object before
   retrying; the DDL uses `IF NOT EXISTS`. Roll back application code if needed,
   preserve rows, and identify pending reservations before any recovery action.
7. After migration, compare appointment counts and schema. Existing rows should
   have nullable idempotency keys and confirmed state. Pending or
   reconciliation-required reservations must be retried with their original
   keys; do not delete them or issue replacement keys.

These are preflight instructions only. This preparation did not connect to or
inspect a production database.

## Calendar, LiveKit, and safety settings

- The booking service needs `CALENDAR_DEMO_BUSINESS_ID` and
  `CALENDAR_ENCRYPTION_KEY` to identify the business and decrypt its stored
  Google refresh token. Google OAuth client settings are needed only by OAuth
  management routes.
- Google OAuth `/connect` and `/callback` are guarded as local-development-only
  and reject non-loopback requests. Do not enable that flow on Render without a
  separate security review and an approved redirect design.
- The agent and backend must use the same `CALENDAR_DEMO_BUSINESS_ID` and the
  agent's `BACKEND_URL` must point to the deployed HTTPS service.
- LiveKit webhook verification uses `LIVEKIT_API_KEY` and
  `LIVEKIT_API_SECRET`. The outbound-call endpoint is disabled unless
  `OUTBOUND_CALLS_ENABLED=true`; keep it false unless separately authorized.
- CORS currently allows `https://nivakya.pro`, `https://www.nivakya.pro`, and
  localhost development origins, with credentials disabled. Add any additional
  browser origin only after it is identified. The voice agent calls the backend
  server-to-server, so browser CORS does not control that path.
- Set credentials through Render's secret environment settings. Do not commit
  `.env`, `.env.deploy`, or real credential files.

See `.env.example` for variable names and placeholders only. It is not a source
of production values.

## Deployment verification

Before accepting traffic, configure the service root, Python 3.12, build/start
commands above, and required variables listed in `.env.example`. Set
`OUTBOUND_CALLS_ENABLED=false` and `AUTO_CREATE_TABLES=false`. Confirm the
database host/port, bounded timeouts, and SSL mode required for the chosen
connection path. Apply the reviewed migration only through an approved,
backed-up procedure. Then verify `/health` (liveness), `/ready` (database
readiness), `/openapi.json`, a read-only availability request, and the agent's
configured HTTPS `BACKEND_URL` against the backend request schema. Do not
verify by creating a production appointment or Calendar event. Confirm the
deployed version and logs before routing callers.
