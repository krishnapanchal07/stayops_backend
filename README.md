> Frontend test cleanup: the temporary Node test scripts, custom TypeScript/VM loader, npm test commands, and their backend Node integration runner were removed at the user's request. Earlier test results and implementation file lists below are historical; those frontend tests are no longer retained.

# StayOps backend: foundation and authentication

FastAPI modular monolith, synchronous SQLAlchemy 2.0, Psycopg 3, PostgreSQL 18,
Alembic, Argon2id (pwdlib), and PyJWT. No public signup, OTP, business APIs, or
mock-user imports. `Fronted/` remains the existing React application.

## Windows PowerShell setup

Run from the repository root. The existing Python 3.15.2 installation was used
successfully; a second Python installation is unnecessary. All required binary
dependencies were available for it. `requirements.txt` pins the tested environment,
including transitive dependencies and pytest/httpx used for verification.

```powershell
py --version
# Only create the environment if Backend/.venv does not already exist:
py -3.15 -m venv Backend/.venv
Set-Location Backend
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check

# Copy only if .env does not already exist. Never overwrite existing credentials.
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Activation is optional. If PowerShell blocks activation, use
`.\.venv\Scripts\python.exe` in place of `python`; changing execution policy is
unnecessary. These instructions assume subsequent backend commands run in `Backend`.

Edit `.env` privately. Configure `DATABASE_URL`, `JWT_SECRET_KEY`, and SMTP settings.
Generate a unique JWT signing secret locally:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use `postgresql+psycopg://krishna_stayops:<URL_ENCODED_PASSWORD>@localhost:5432/stayops_db`.
Percent-encode reserved characters **in the password component only**, using
`urllib.parse.quote(password, safe='')` in trusted local configuration tooling.
For example, `@` becomes `%40`, `#` becomes `%23`, and `%` becomes `%25`.
Do not paste credentials into source files, command history, tickets, or logs.
Process environment variables override `.env`; JSON-list syntax is required for
`CORS_ORIGINS`. Settings are validated at startup; missing secrets fail closed.

## PostgreSQL setup

The existing PostgreSQL service must be running. Check existing databases/roles
in pgAdmin first, or inspect with the following queries:

```sql
SELECT rolname FROM pg_roles WHERE rolname = 'krishna_stayops';
SELECT datname FROM pg_database WHERE datname = 'stayops_db';
```

Only if the role/database is absent, run the corresponding command as your local
PostgreSQL administrator. Do not recreate or drop an existing database.

```sql
CREATE ROLE krishna_stayops LOGIN;
CREATE DATABASE stayops_db OWNER krishna_stayops;
```

Set the role's password through pgAdmin's role properties, or use the interactive
psql password prompt (avoids a literal password in SQL/query history):

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -h localhost -U postgres -d postgres
```

Inside psql:

```text
\password krishna_stayops
\q
```

For an existing database owned by another role, grant only the schema privileges
needed for migrations, with the database owner/administrator's authorization.
The application role does not need superuser, CREATEDB, or CREATEROLE privileges.

## Migrate and bootstrap

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic check
python -m app.cli seed
python -m app.cli bootstrap
```

Migrations create users, roles, permissions, role-permission links, trusted property
assignments, sessions, refresh-token history, and password-reset tokens.
No `create_all()` startup shortcuts are used. `seed` is idempotent and synchronizes
the versioned role/permission snapshot with the original frontend contract.
Bootstrap seeds roles too, prompts for name/email and a hidden password with
confirmation, and refuses if any super-admin already exists. PostgreSQL advisory
locking prevents concurrent first-admin creation. No default password is provided.

Further accounts are provisioned by an authenticated `SUPER_ADMIN` through
`POST /api/v1/auth/users`. Send `full_name`, `email`, `password`, and one of the
seven existing role codes. This endpoint is deliberately unavailable to owners
and public clients. There is one role per account. Unknown request fields, including
attempts to set permissions, status or property assignments, are rejected.
Assign initial passwords through an appropriate private channel; users can change
them through their existing profile screen or request a password-reset link.

## Run backend and frontend

From `Backend`:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

- Swagger: <http://localhost:8000/docs>
- Health: <http://localhost:8000/api/v1/health>
- API base: <http://localhost:8000/api/v1>

Health runs `SELECT 1`, returning HTTP 503 if the database is unavailable.
Database names, URLs, exception details and credentials are never returned.
The app logs method, matched route template, status and duration, excluding
query strings and request bodies. Disable upstream request-body/query logging for
auth routes too. API responses use `Cache-Control: no-store`.

In a second PowerShell terminal:

```powershell
Set-Location Fronted
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm.cmd run dev
```

Use <http://localhost:5173> consistently. If Vite chooses another port, update
`FRONTEND_URL` and `CORS_ORIGINS` and restart the API, or free port 5173.
Avoid mixing `localhost` and `127.0.0.1` between the browser frontend and API:
SameSite cookie behavior depends on the hostname. Set `VITE_API_URL` when the API
base differs. Existing mock credentials no longer sign users in. `/register`
remains the inquiry form; private business services remain mocks.

## Auth endpoints

| Method | Path under `/api/v1` | Behavior |
|---|---|---|
| GET | `/health` | Application/database readiness |
| POST | `/auth/login` | JSON `{email,password,remember?}`; access token and refresh cookie |
| POST | `/auth/refresh` | Rotate HttpOnly refresh cookie; issue new access token |
| POST | `/auth/logout` | Revoke current session and clear cookie; idempotent |
| GET | `/auth/me` | Current public user, role, permissions |
| POST | `/auth/forgot-password` | JSON `{email}`; generic HTTP 202 response |
| POST | `/auth/reset-password` | JSON `{token,new_password}`; revoke all user sessions |
| POST | `/auth/change-password` | Authenticated `{current_password,new_password}`; revoke all sessions |
| PATCH | `/auth/me` | Authenticated `{name,avatar?}`; identity and role are immutable |
| POST | `/auth/users` | Super-admin-only account provisioning |

All POST/PATCH auth requests require an exact configured `Origin` and
`X-StayOps-CSRF: 1`. The custom header forces browser CORS preflight; it is not a
secret. For Swagger's Try It Out on port 8000, the UI origin must also be an
explicitly trusted origin and the header must be supplied (use browser frontend
or a REST client for normal testing). Bearer tokens go in `Authorization`.

Public sessions preserve `{user,role,permissions}` and the frontend's camelCase
user fields. No passwords, hashes, refresh tokens or reset tokens appear in JSON
responses. Short-lived access JWTs live only in frontend memory; refresh cookies
are HttpOnly, host-only, and scoped to `/api/v1/auth`. `remember=false` creates a
browser-session cookie; its server session still has a bounded absolute lifetime.

Refresh rotation retains consumed token digests so replay revokes the entire
session family, including access JWTs. JWT verification checks algorithm,
signature, issuer, audience, type, expiry and required claims. Every authenticated
request rechecks the database session, user activity and current role permissions.
Row locks serialize refresh/reset/password changes. Logout revokes the current
session; password reset/change revokes all sessions and unused reset links.

React restores sessions once before protected navigation, uses one shared refresh
promise for concurrent 401s, serializes refreshes between tabs with Web Locks when
available, and retries requests once. Failed logout leaves the UI session available
for retry. Existing staff profile name/photo and change-password controls use the API;
photo inputs accept the same JPG/PNG/WEBP formats up to 5 MB. Profile images remain
inline fields for compatibility in this phase; a media module is outside scope.
Tenant business profiles still depend on mock tenant IDs, so new database UUIDs
can show unavailable-profile/room states until tenant data is integrated. No
mock-ID aliases or fabricated business assignments are created.

`get_current_user`, `get_active_user`, `require_role`, and `require_permission` are
reusable backend dependencies. Future resource handlers must also call
`ensure_property_access` and/or `ensure_record_owner` with ownership/assignment
loaded from the target record. Never trust client-supplied ownership. Property
assignments default to empty; no business ownership or assignment is inferred from
mock data. Future property models must add their foreign-key relationship.

## SMTP password reset

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`,
`SMTP_USE_TLS`, and trusted `FRONTEND_URL`. Gmail normally uses port 587, TLS and a
provider-approved application password. Port 465 supports implicit TLS. No SMTP
library or OTP flow is used.

Eligible existing users receive a one-use link valid for 20 minutes by default.
Only SHA-256 digests of high-entropy opaque tokens are stored. The reset token is
placed in the URL fragment, never the HTTP query; the React page reads it into
memory and removes it from browser history. The reset route remains accessible
even when another session is signed in, and referrers are disabled.
Password policy: 12-128 characters, including a letter and a number.

Existing, inactive and unknown addresses receive the same HTTP 202 response.
Database lookup and SMTP execute after the response in a background task for all
addresses, limiting enumeration through response time. Unknown/inactive addresses
receive no email. Delivery failure rolls back the new token and preserves any
previous usable link; a generic warning is logged without email/token details.
No failed-token rows accumulate. These tasks are in-process: a server restart can
interrupt a pending email; users can request another link. SMTP acceptance followed
by a rare database commit failure can also require a new request.

## Verification

From `Backend`:

```powershell
python -m pytest -q
```

Tests use the installed PostgreSQL 18 `initdb`/`pg_ctl` binaries to create a
temporary cluster on a random **loopback-only** port, then a unique migrated
schema per test. They never connect to, truncate or clear `stayops_db`.
The disposable cluster uses local trust authentication and is stopped/removed in
fixture cleanup. Override `POSTGRES_BIN` if PostgreSQL is installed elsewhere:

```powershell
$env:POSTGRES_BIN = 'C:\Program Files\PostgreSQL\18\bin'
python -m pytest -q
```

The suite covers migration upgrade/downgrade/schema drift, database constraints,
role contracts, Argon2 verification, login, JWT checks, permissions, provisioning,
resource scopes, cookies, CSRF/CORS, refresh rotation/replay/concurrency, logout,
password reset/expiry/reuse/concurrency, SMTP rollback, and session revocation.
SMTP is mocked; no real emails are sent by tests. A live Uvicorn test runs the
actual React Axios service over HTTP, so Node and existing frontend dependencies
must be available. It checks login, cookie rotation, restoration, `/me` and logout.

From `Fronted`:

```powershell
npm.cmd run build
npm.cmd run lint
```

The auth suite executes real Axios interceptors, Redux state and route guards
using the existing TypeScript compiler and Node test runner. Its standalone live
API case skips unless `STAYOPS_TEST_API_URL` is set; the backend pytest suite runs
that case automatically against Uvicorn and the isolated PostgreSQL cluster.
These are service/route tests, not full browser automation. Existing discovery
regressions retain their business-module checks; their former mock-auth test has
moved to the API auth suite. No frontend package dependencies were added.

## Deployment considerations and remaining local configuration

Set `APP_ENV=production`, a unique signing secret, HTTPS frontend/origins,
`COOKIE_SECURE=true`, SMTP TLS and a protected PostgreSQL connection. Keep frontend
and API on the same site when practical; genuinely cross-site deployments need
`COOKIE_SAMESITE=none` and HTTPS, and browser third-party-cookie restrictions apply.
There is no wildcard credentialed CORS.

The bounded in-memory rate limiter is thread-safe and appropriate for one API
worker. It limits login/reset requests by direct client IP and normalized account,
and refreshes by IP. It deliberately does not trust forwarded headers. For multiple
workers/hosts or a reverse proxy, configure a trusted edge rate limiter and proxy
client-IP handling; process-local counters do not coordinate across workers and
reset on restart. Run one application worker until that protection exists.

Periodically remove expired sessions/refresh history and reset rows:

```powershell
python -m app.cli cleanup
```

Actual `stayops_db` migration/seeding/bootstrap and SMTP delivery require your real
private configuration. The repository supplies placeholders and secure commands;
it does not invent credentials or provision accounts with known passwords.
The selected libraries and patterns follow the official
[FastAPI password/JWT guidance](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
and [SQLAlchemy session documentation](https://docs.sqlalchemy.org/en/20/orm/session_api.html).

## Executed verification (21 November 2026)

- Backend: 47 tests passed on existing Python 3.15.2 and disposable PostgreSQL 18.7,
  including real Uvicorn startup, readiness/docs HTTP requests and the live Axios flow.
- Frontend auth: 8 passed; the standalone live case skips and is executed by pytest.
- Discovery regressions: 10 passed.
- TypeScript/Vite production build: passed; existing chunk-size/mixed-import warnings remain.
- ESLint: passed with zero errors and three existing `PropertyListPage` hook warnings.
- `pip check`: no broken requirements. `git diff --check`: passed.
- The installed Starlette version emits a deprecation warning for its httpx-backed
  TestClient. Tests pass; no extra replacement HTTP library was installed.
- Real `stayops_db` credentials, actual account bootstrap and live SMTP delivery
  remain configuration steps. Full browser automation was not performed.

## Phase 2A + 2B — Inquiry and tenant management

The inquiry and tenant services now use the existing authenticated Axios client
and PostgreSQL. `/register` remains a general lead form; its tenant/owner/staff
choice never creates a login account or grants a role. General leads have no
fabricated property or room reference and are visible to the Super Admin.

### Migration and data boundaries

`alembic/versions/0002_inquiries_tenants.py` follows revision `0001_auth`.
It adds `inquiries`, `inquiry_status_events`, `tenants`, `tenant_occupancies`,
`tenant_activity`, and `tenant_invitations`. It adds no mock residents or accounts,
changes no auth data, and creates no foreign keys to fictional inventory tables.
Existing user, session, refresh and reset tables remain intact.

Run from Backend with the existing environment and virtual environment:

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m alembic current
```

The temporary inventory adapter is `app/modules/inventory/service.py`; its
server-owned `development_catalog.json` preserves the stable IDs and inventory
quotes from the existing frontend fixture. Mock occupant names, tenant IDs and
move-in dates were excluded. Fixture-occupied/blocked beds stay unavailable;
new allocations use the durable occupancy ledger. Browser-local inventory edits
never authorize server allocation and are not synchronized into this catalog.
The adapter is explicitly disabled for `APP_ENV=production`. General leads and
stored profiles remain readable, but operations needing inventory require the
future canonical Property/Room adapter before production deployment.

Replace this adapter's selection, property lookup, snapshots, availability and
allocation validation with canonical PostgreSQL Room queries when that module
arrives. Migrate ledger allocations into that module's inventory transaction;
do not run the two allocation authorities independently. Discovery cards/maps
remain mock-backed and may show stale availability until a room detail is opened;
room-detail applications, conversion previews and tenant allocation selectors
read server availability. All final mutation checks run again on the server.

### API endpoints

All paths below are relative to `/api/v1`. Static paths precede UUID detail paths.
Lists return `{items,total,offset,limit}` with a default page of 50 and maximum
of 100. Query inputs are bounded. Activity accepts bounded offset/limit and
returns the existing activity array contract.

| Method | Path | Access / purpose |
| --- | --- | --- |
| POST | `/inquiries` | Limited guest or authenticated room inquiry |
| POST | `/inquiries/general` | General tenant/owner/staff lead, no inventory required |
| GET | `/inquiries` | INQUIRY_VIEW; scoped staff or own tenant requests |
| GET | `/inquiries/summary` | Scoped SQL status counts |
| GET | `/inquiries/availability` | Public bed-availability booleans; no occupant identity |
| GET | `/inquiries/me` | Trusted account identity and controlled tenant linkage |
| GET | `/inquiries/{id}` | Scoped detail / owned inquiry |
| PATCH | `/inquiries/{id}/status` | INQUIRY_MANAGE and valid transition |
| POST | `/inquiries/{id}/withdraw` | Only the submitting/securely linked account |
| POST | `/inquiries/{id}/convert` | INQUIRY_MANAGE + TENANT_CREATE; ROOM_ALLOCATE if allocating |
| GET, POST | `/tenants` | TENANT_VIEW / TENANT_CREATE; property scoped |
| GET | `/tenants/summary` | Scoped SQL status counts |
| GET, PATCH | `/tenants/me` | TENANT identity; restricted personal fields only |
| POST | `/tenants/accept-invitation` | Authenticated TENANT + single-use mailbox token |
| GET, PATCH | `/tenants/{id}` | TENANT_VIEW / TENANT_EDIT and property scope |
| PATCH | `/tenants/{id}/status` | Validated lifecycle; activation requires ledger allocation |
| POST | `/tenants/{id}/allocate` | TENANT_EDIT + ROOM_ALLOCATE; validates target property too |
| POST | `/tenants/{id}/checkout` | Atomic move-out, lifecycle update and bed release |
| GET | `/tenants/{id}/activity` | Real persistent audit events, authorized management |
| GET | `/tenants/{id}/documents` | Authorized empty array; document storage is not implemented |
| POST | `/tenants/{id}/invite` | Super Admin only; SMTP account setup/linking |

Inquiry list filters: query, propertyId, status. Tenant list filters: query,
propertyId, roomId, status. Existing frontend filtering, sorting and pagination
remain available; building/floor/move-in filters still run in the UI.

### Transactions, authorization and identity

Conversion locks the inquiry, serializes normalized tenant-email creation, and
writes the tenant, optional ledger allocation, inquiry linkage and status event
in one transaction. Repeated conversion returns 409. Competing allocation is
protected by the ledger bed primary key and unique tenant ID; failed conversion,
reallocation or checkout rolls back all associated changes. Tenant codes use
UUID-derived unique codes rather than a racy count/max sequence.

Open room inquiries have a partial unique email/room/coalesced-bed index. Public
POST supports `Idempotency-Key`; payload mismatch returns 409. Forms retain their
key through a network failure and use a new key when the payload changes. Body
limits apply before JSON decoding, including chunked bodies. Existing trusted
Origin + CSRF-header checks and bounded IP/email rate limits are reused. Deploy
one worker or configure coordinated edge limits as described above.

Server permissions and auth property assignments govern every staff operation;
empty scopes grant no access. Tenants cannot supply ownership IDs, actors,
privileged status or tenant linkage in public requests. Inquiry actors come from
the authenticated session. Tenant `/me` responses whitelist fields and exclude
internal notes, management activity and user linkage metadata.

Guest inquiry emails alone confer no account access. Authenticated submissions
are linked only to the authenticated user's matching account email. A guest's
past inquiries become accessible only after a server-controlled tenant/account
link exists. An existing TENANT account must accept a short-lived, single-use
mailbox invitation while signed in as that same account; a matching privileged
email is rejected. A newly provisioned TENANT account receives an unguessable
password hash and the existing SMTP password-setup/reset link. Existing account
roles/passwords are never overwritten by email matching. Delivery failure rolls
back provisioning/token changes, and raw invitation/reset tokens are not stored.
No real SMTP email is sent by automated tests.

Tenant lifecycle: PROSPECT → ONBOARDING/INACTIVE; ONBOARDING → ACTIVE/INACTIVE/
CHECKED_OUT; ACTIVE ↔ NOTICE_PERIOD; ACTIVE/NOTICE_PERIOD → INACTIVE/CHECKED_OUT;
INACTIVE → ONBOARDING/ACTIVE/CHECKED_OUT; CHECKED_OUT → ONBOARDING. ACTIVE and
NOTICE_PERIOD require an allocation. INACTIVE and CHECKED_OUT release it.
Profile deactivation prevents self-profile editing; authentication account
activation remains a separate existing auth responsibility.

### Changed files for this phase

- New backend packages `app/modules/inquiries/` and `app/modules/tenants/`, each
  with `__init__.py`, `models.py`, `schemas.py`, `service.py`, and `router.py`.
- New `app/modules/inventory/{__init__.py,service.py,development_catalog.json}`,
  `app/core/workflow_limits.py`, and the `0002_inquiries_tenants.py` migration.
- Updated `alembic/env.py`, `app/api/v1/router.py`, `app/main.py`,
  `app/modules/auth/dependencies.py`, and `app/services/email_service.py`.
- New `tests/test_inquiries_tenants.py`; updated `tests/test_auth.py` migration
  head assertion and `tests/test_live_integration.py` for live workflow coverage.
- Frontend services: inquiry/inquiryService.ts, tenant/tenantService.ts,
  dashboard/dashboardService.ts (tenant dashboard method only), and new
  http/workflows.ts. Existing inquiryStore/tenantStore remain solely for legacy
  unrelated mock modules; migrated services do not import them.
- Frontend inquiry pages: auth/RegisterPage.tsx, find-room/FindRoomDetailsPage.tsx,
  admin/inquiries/{InquiryListPage,InquiryDetailsPage}.tsx, and InquiryForm.tsx.
  Existing status/conversion modals and MyInquiriesPage use the migrated service.
- Frontend tenant pages: admin/tenants/{TenantListPage,TenantDetailsPage}.tsx,
  tenant/{TenantProfilePage,MyRoomPage,TenantDashboardPage}.tsx; tenant components
  TenantForm.tsx, TenantStatusModal.tsx, TenantCheckoutModal.tsx.
- Frontend contract: types/inquiry.ts. The former temporary frontend workflow
  scripts were subsequently removed; current checks are build/lint and browser QA.
- This README and Fronted/README.md document usage and limitations.

### Performance and remaining boundaries

Indexed email, user, property, room, status and creation timestamps support
scoped queries. Inquiry status events use select-in loading instead of per-row
queries. Summaries aggregate in SQL. Profile extras and immutable display
snapshots use typed, validated JSON; indexed routing/identity fields are scalar
columns. Text search currently uses bounded ILIKE queries, not full-text search.
The compatibility array adapter consumes API pages to preserve existing table
components; large datasets should move table pagination/filtering fully to the
server rather than loading all permitted pages. Inline profile images retain the
existing bounded data-image behavior and are not a new document-storage system.

Property/Room modules are still development fixtures; these allocations are
transitional, not a production-ready inventory system. No payment settlement,
complaint, visitor, leave, notice, roommate directory or document storage backend
was added. Migrated tenant screens show unavailable states for those integrations
rather than fabricated balances or history. Unrelated staff and business mock
pages remain in place. Their IDs are separate from persistent tenant IDs; they
cannot operate on a new tenant until their own module is migrated. Notification
preferences also retain their existing browser-local implementation.

### Verification commands

```powershell
# Backend; isolated disposable PostgreSQL, never stayops_db
.venv\Scripts\python.exe -m pytest -q
# Fronted
npm.cmd run lint
npm.cmd run build
```

Backend tests include migrations/schema drift, guest/authenticated submissions,
general leads, body/rate/origin/idempotency controls, unknown/unavailable inventory,
open-inquiry uniqueness, ownership/property access, invalid transitions,
concurrent conversion/allocation, transactional rollback, checkout/reuse,
provisioning/SMTP rollback, existing-account mailbox proof, self-service privacy,
and Phase 1 authentication regressions. Two live Uvicorn tests execute the actual
React Axios auth and inquiry/tenant services against disposable PostgreSQL.
Standalone Node runs skip their live cases; pytest runs those cases automatically.
Full browser click automation and delivery through the real SMTP account are not
part of this automated verification.

### Executed Phase 2 verification (24 November 2026)

- Full backend suite: **63 passed**, including both live Uvicorn/Axios cases.
- Frontend auth: **8 passed**; discovery: **10 passed**; workflows: **7 passed**.
  Two live cases skip in standalone Node runs and execute successfully in pytest.
- TypeScript/Vite production build: passed. ESLint: zero errors, three existing
  property-list hook warnings. Existing Vite large-chunk/mixed-import warnings remain.
- `pip check`: no broken requirements. `git diff --check`: passed.
- Starlette's existing httpx TestClient deprecation warning remains; no extra
  replacement dependency was installed.
- The configured local database was upgraded additively from `0001_auth` to
  `0002_inquiries_tenants`. Tests used disposable PostgreSQL only, and no test
  data/accounts were inserted into the configured application database.
- SMTP delivery uses existing configuration but was mocked in tests; actual
  mailbox delivery and full browser click automation were not performed.

## Phase 2C — Staff Management

Staff Management now persists staff profiles in PostgreSQL and uses the existing
`users`, roles, password hashes, SMTP delivery and `auth_property_assignments`.
No mock staff records or known-password accounts are imported. Existing staged
Phase 0–2B work is preserved.

### Data model and migration

Migration `0003_staff_management` follows `0002_inquiries_tenants` and adds:

- `staff_profiles`: UUID ID, unique user FK, unique employee code, phone,
  department, designation, employment status, joining date, address, emergency
  contact, link-confirmation flag and audit timestamps.
- `staff_link_invitations`: hashed single-use token, expiry, expected staff role
  and proposed property IDs for an existing-account link awaiting mailbox proof.
- `staff_employee_code_seq`: PostgreSQL sequence generating `STF-1001`, etc.
  Sequence gaps after rollbacks are expected; codes remain unique under concurrency.

Name, email, avatar, authentication role, account activation and password remain
exclusively authoritative in `users`. Staff profile output derives first/last
names from the canonical user display name. Existing single-name accounts can be
linked without altering that identity. JSON address/contact fields are validated
and bounded; status, department and phone have database check constraints.

Existing user/session/reset/inquiry/tenant tables are not recreated or cleared.
The migration was checked against the actual Phase 2B schema, and a disposable
migration test verifies that all prior table row counts survive the additive
upgrade. Effective assignments always use `auth_property_assignments`; invitation
proposals grant no permissions before confirmation. There are no invented property
foreign keys or permanent parallel staff-assignment table.

### API endpoints

Paths are relative to `/api/v1`. Static routes precede UUID detail routes.

| Method | Path | Authorization and behavior |
| --- | --- | --- |
| GET | `/staff` | STAFF_VIEW; search, filters, sorting and bounded pagination |
| POST | `/staff` | STAFF_CREATE plus SUPER_ADMIN; atomic provisioning and SMTP invitation |
| GET | `/staff/summary` | STAFF_VIEW; actual scoped SQL counts |
| GET | `/staff/properties` | STAFF_VIEW; server-validated property choices within the caller's scope |
| GET | `/staff/account-candidate?email=...` | Super Admin only; explicitly select a compatible existing account |
| POST | `/staff/accept-invitation` | Signed-in target account plus short-lived mailbox token |
| GET | `/staff/{id}` | STAFF_VIEW plus complete property-scope check |
| PATCH | `/staff/{id}` | STAFF_EDIT; bounded personal/employment fields; atomic optional status/assignment edit |
| PATCH | `/staff/{id}/status` | STAFF_EDIT; employment status and auth/session enforcement |
| PATCH | `/staff/{id}/assignments` | STAFF_EDIT; validate all properties before replacing trusted assignments |
| POST | `/staff/{id}/resend-invite` | Super Admin only; safely rotate and deliver setup/link invitation |

List query parameters: `query`, `role`, `department`, `status`, `propertyId`,
`offset`, `limit`, `sortBy`, `sortDirection`. Search covers name, email, employee
code and phone. Sort keys are allowlisted: fullName, email, employeeCode, role,
department, status and createdAt; direction is asc/desc. A stable UUID tie-breaker
avoids ambiguous ordering. Default page size is 50; maximum is 100. Responses use
`{items,total,offset,limit}`. Summary returns `{total,active,inactive,onLeave}`.

Role inputs accept only WARDEN, ACCOUNTANT, SECURITY and MAINTENANCE. Department
and status inputs use the existing frontend enum values. Generic edits reject
email, role, user ID, password and arbitrary activation flags. Name/avatar edits
write the existing auth identity rather than maintaining a conflicting copy.

### Provisioning and account access

**New account:** Add Staff validates permissions, normalized email and trusted
properties, locks the creation key, creates an unguessable password hash, creates
the profile and assignments, and issues a normal `PasswordResetToken`. SMTP sends
a staff setup message pointing to the existing `/reset-password#token=...` page.
The user sets a password and signs in through the existing login service, role
permissions and dashboard routing. No plaintext password is returned or emailed.

**Existing compatible account:** The admin explicitly uses **Select an existing
login account** in Add Staff. The backend checks its exact ID, normalized email,
role, enabled status and absence of another staff/tenant profile link. It reserves
one staff profile for that account but preserves its name/avatar, password,
activation and current effective assignments. A separate hashed mailbox link goes
to the account's existing role profile page. The signed-in account must accept it;
only then are the proposed assignments applied. Pending staff are clearly labeled,
are not editable and are hidden from Owner management. Wrong-account, expired,
replayed and role-mismatched acceptance attempts fail. The acceptance revokes old
sessions when the effective scope changes; the UI returns to normal login.

Email/role matching alone never silently links an account or changes its role.
SUPER_ADMIN, OWNER and TENANT accounts are incompatible. Disabled existing staff
accounts must be handled through their existing account administration before
linking. A unique user FK prevents duplicate profiles. The existing auth email
unique index and transaction-scoped locking prevent duplicate account creation.

**Email failure:** Provisioning and invitation changes roll back together if SMTP
fails. Resending a pending link reuses its profile and rotates the token; a failed
resend preserves the previously valid token. Resending setup links invalidates
previous unused setup/reset tokens using existing reset semantics, without
changing the password. SMTP is synchronous inside the bounded transaction to
preserve this existing architecture's rollback behavior; no queue or email package
was added. A process/database failure after email delivery can produce an unusable
link; the existing resend action is the recovery path.

**ACTIVE:** enable the linked account; normal login still requires possession of
a valid password. **INACTIVE:** disable the account and invalidate all sessions
and unused setup/reset tokens, preserving profile/history records. Reactivation
does not revive revoked sessions. **ON_LEAVE:** preserve existing enabled/disabled
login access and active sessions. Assignment changes revoke existing sessions;
unchanged assignments do not. An account created INACTIVE must be activated and
sent a fresh setup invitation before password setup/login can succeed.

### Property permissions and inventory boundary

Super Admin has authorized global management. Existing OWNER role permissions
allow scoped reads/edits, but do not authorize account provisioning, linking or
invitation resends. Add Staff is therefore Super Admin-only in both UI and API.
An Owner must have trusted access to **every** property assigned to a staff member
before viewing/editing/suspending that account. Shared staff spanning unauthorized
sites are excluded from the list and denied at detail/mutation endpoints. Target
assignments are checked separately, preventing scope expansion or reassignment to
another owner's properties. An empty Owner scope grants no staff access.

Properties are validated through the existing server-controlled development
inventory adapter. No property fixtures or frontend stores are overwritten.
Mutation/selection operations requiring that adapter remain unavailable in
production until canonical PostgreSQL Property Management replaces the adapter.
Stored staff/status data remains persistent. Mock business dashboards, finance,
visitor and maintenance workflows are outside this phase; the staff detail page
honestly identifies unavailable maintenance integration for persistent staff IDs.

### Frontend behavior and performance

The existing `/admin/staff` layout, summary cards, form, table, filters, action menu,
detail page, responsive cards and Tailwind tokens are retained. Staff service calls
use the existing Axios client and workflow error adapter, including refresh tokens
and auth headers. Text search is debounced; list filters, ordering and 10-row UI
pagination are served by PostgreSQL. The shared DataTable gained optional server
pagination/sorting props; its default client behavior is unchanged and tested.
Summary counts come from the separate SQL summary endpoint.

Forms validate contacts, dates, role choices and required details, disable
submission while saving, preserve input on API errors, show success/error toasts
and refresh after changes. Role/email are locked when editing linked accounts.
Edit submits profile, optional assignment and employment status changes in one
backend transaction. Staff details support invitation resend and pending-link
explanation. StaffProfilePage handles only the explicit staff-link fragment;
normal account profile/password behavior remains unchanged.

Indexes cover user uniqueness, employee codes, department, status and creation
order; existing users email/role and assignment keys are reused. User, role,
permission, assignment and pending-invitation relationships use select-in loading,
avoiding per-row queries. Search currently uses bounded ILIKE queries, not a
full-text/trigram service. API pages are capped; the staff list does not load the
whole directory to filter in the browser. Existing array-returning service methods
remain available for compatibility.

### Files changed in Phase 2C

- New `app/modules/staff/{__init__.py,models.py,schemas.py,service.py,router.py}`.
- New `alembic/versions/0003_staff_management.py`.
- Updated `alembic/env.py`, `app/api/v1/router.py` and
  `app/core/workflow_limits.py` (extend bounded bodies to staff requests).
- Updated `app/services/email_service.py` with staff setup/link templates using
  the existing SMTP transport. Auth login/reset/service/models/role definitions
  and inquiry/tenant business logic were not rewritten.
- New `tests/test_staff.py`; updated `tests/test_auth.py` migration-head assertion
  and `tests/test_live_integration.py` for the live staff workflow. The mock mailbox
  inspection endpoint exists only in the disposable test application.
- Frontend `src/services/staff/staffService.ts`, `src/types/staff.ts`,
  `src/utils/staffValidation.ts`, `src/components/staff/StaffForm.tsx`,
  `src/pages/admin/staff/{StaffListPage,StaffDetailsPage}.tsx` and the explicit
  invitation handler in `src/pages/common/StaffProfilePage.tsx`.
- Frontend `src/components/tables/DataTable.tsx`: optional server paging/sorting.
- The former temporary frontend staff/workflow harness was subsequently removed.
  Current checks are backend pytest, frontend build/lint and browser QA.
- Backend/README.md and Fronted/README.md document usage, boundaries and results.
  Mock staff/property/room files remain available; no seed import runs.

### Verification

```powershell
# Backend: disposable PostgreSQL, never the actual stayops_db
.venv\Scripts\python.exe -m pytest -q
# Fronted
npm.cmd run lint
npm.cmd run build
```

Staff tests cover valid/invalid roles, duplicate accounts/profiles, compatible
mailbox linking, incompatible accounts, concurrent creates, employee codes,
property validation/scope, edits, status/session enforcement, all four staff role
logins, SMTP rollback, expired/reused/resend links, disabled access, privilege
escalation, list filters/sorting/pagination and additive migration preservation.
Node tests execute actual staff Axios adapters and render existing list/detail/
form/table components. A live Uvicorn test creates staff, consumes a mocked setup
email, sets the password, logs in at the correct role, edits the profile, preserves
ON_LEAVE access, suspends it and reactivates it. Existing auth, inquiry, tenant and
public discovery regression suites remain included.

Live cases skip in standalone Node runs unless `STAYOPS_TEST_API_URL` is set;
backend pytest executes all three live cases automatically using disposable
PostgreSQL. No real invitation emails or test accounts are created in stayops_db.
Actual external SMTP delivery and full browser click automation are not performed
by these tests. Existing Starlette TestClient, Vite chunk/mixed-import and three
property-list ESLint warnings remain unrelated limitations.

### Executed Phase 2C verification (24 November 2026)

- Full backend suite: **96 passed**, including migration/schema checks and all
  three live Uvicorn/Axios workflows (auth, inquiries/tenants, staff setup/login).
- Standalone frontend tests: auth **8 passed**, discovery **10 passed**,
  workflows **7 passed**, staff **8 passed** — **33 passed** in total. The three
  live cases skip standalone and execute successfully from backend pytest.
- TypeScript/Vite production build passed. ESLint passed with zero errors and
  the same three existing PropertyListPage hook warnings. Existing Vite chunk and
  mixed-import warnings remain. Starlette's TestClient deprecation warning remains.
- `pip check` found no broken requirements. `git diff --check` passed.
- The configured application database was upgraded additively to
  `0003_staff_management`. No fixture accounts/staff or test records were inserted
  into it. All automated database tests used disposable PostgreSQL.
- SMTP was mocked during verification. Actual external mailbox delivery and full
  browser click automation were not performed.
# Phase 3 property management (November 2026)

The canonical property module is `app/modules/properties/`: models, schemas,
service, router, Pillow image processor, local storage adapter, development demo
seeder and explicit orphan-maintenance command. Integration changes cover auth
ownership checks, inventory selection, inquiry/tenant snapshots and staff property
choices. Existing room IDs and historical snapshots remain intact.

Migration `0004_properties` adds properties/images, imports nine legacy property
IDs as private demo metadata, and adds property foreign keys. Ownership is only
backfilled for unambiguous existing Owner assignments. Run `alembic upgrade head`.
Production operators must review ownership/publication before exposing imported
records. No destructive reset is required.

All paths below have prefix `/api/v1`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET / POST | `/properties` | Scoped paginated search / create |
| GET | `/properties/summary`, `/properties/owners` | Scoped counts/filter choices / eligible owners |
| GET / PATCH | `/properties/{id}` | Detail / validated form replacement |
| PATCH | `/properties/{id}/status` | Status |
| POST | `/properties/{id}/archive` | Soft archive |
| GET | `/public/properties`, `/public/properties/{id}` | Published active metadata |
| GET / POST | `/properties/{id}/images` | Gallery / multipart upload |
| DELETE | `/properties/{id}/images/{imageId}` | Remove image |
| PATCH | `/properties/{id}/cover-image`, `/properties/{id}/images/order` | Cover / order |
| GET | `/property-images/{imageId}/{variant}` | Public or signed private image |

SuperAdmin manages all properties; Owners manage actual owned properties; staff
can read their assigned properties; Tenants cannot use management endpoints.
Public responses omit owner/contact/internal data. Production excludes demos.

Images must be nonempty JPG/JPEG, PNG or WebP and **strictly below 4,194,304 bytes**
each; maximum 12 per property. The frontend checks type, extension, size and count,
shows removable previews and prevents repeated submission. The backend bounds the
body and each file, verifies actual decoder format against MIME/extension, rejects
corrupt/animated files and images above 24 million pixels or 12,000 pixels per side,
normalizes EXIF orientation and strips metadata. Coordinates require both values
within map bounds. Property forms validate required fields and lengths.

WebP variants are thumbnail (maximum width 400, quality 75), medium (900, 80),
large (1600, 82), without upscaling. A smaller lossless encoding may replace lossy
output for flat images. Every output is also below 4 MiB. Metadata only is stored
in PostgreSQL; files live at `storage/property-images/<random-hex-id>/<variant>.webp`
(`PROPERTY_IMAGE_DIRECTORY` can override). Back up both database and storage.
Private image URLs are short-lived signed capabilities; do not share them.

Uploads validate the whole batch before storage writes, lock the property, dedupe
content, write immutable objects then commit metadata; failures compensate by
removing written files. Deletes commit metadata before removing files. Interrupted
deletes/writes can leave inaccessible orphans: `python -m app.modules.properties.maintenance`
removes only recognized, unreferenced objects older than 24 hours. This operator
command must be scheduled separately; no background scheduler was introduced.

Run `python -m app.modules.properties.demo` from Backend with an existing development
SuperAdmin login to seed/reconcile three clearly marked fictional demo properties
through the real API. Re-running preserves IDs and deduplicates images. The command
is disabled outside development. Sources remain in `storage/demo-sources`.

Reusable demos: DEMO Cedar Courtyard PG, DEMO Willow Shared Hostel, DEMO Amber
Co-living. Each has four photos (12 uploads / 36 WebP variants total). Source bytes
total 3,858,201; thumbnail bytes 193,234; medium 795,138; large 2,055,122. Large
variants are about 47% smaller in aggregate and thumbnails about 95% smaller.
Demo coordinates are deliberately empty because these are fictional properties.

Photo sources, reused as illustrative demo photography under the
[Pexels license](https://www.pexels.com/license/) (free personal/commercial use;
attribution not required): [exterior](https://www.pexels.com/photo/an-apartment-building-5504654/),
[bedroom](https://www.pexels.com/photo/bedroom-interior-with-minimal-design-12222276/),
[shared room](https://www.pexels.com/photo/a-room-in-a-hostel-5137980/),
[kitchen](https://www.pexels.com/photo/a-kitchen-in-an-apartment-18470956/),
[living room/balcony](https://www.pexels.com/photo/opened-doors-for-balcony-in-living-room-19857226/).
These photos do not depict actual StayOps listings.

Real browser checks exercised login/refresh/logout, create with three uploads,
edit/refresh, archive/reactivate, search, cover/reorder/delete, invalid/exact-limit/
oversize/3.9-MiB uploads, Owner isolation, Tenant denial, public gallery/lightbox,
map zoom/marker navigation/filter synchronization and responsive layouts. Existing
staff, tenant and inquiry lists loaded. Bugs fixed included empty-room cards saying
"Beds available", stale canonical names in new allocation snapshots, staff invite
validation's missing database parameter, and property filter pagination reset.

The final test results and cleanup outcome are reported in the delivery message.
Automated tests use disposable PostgreSQL and isolated storage. Temporary Phase 3
tests/accounts/property/uploads are removed after verification, preserving existing
backend tests and all reusable demos. No frontend test harness was reintroduced.

Limits: Room CRUD is the next phase. Buildings/rooms use the transitional existing
catalog with actual occupancy-ledger counts; new properties honestly have no beds.
Public discovery still aggregates paged property metadata with that room catalog
client-side for map/filter behavior; admin listing uses server pagination. Full
public server-side room-aware filtering awaits the Room module. Real SMTP delivery,
browser offline/500 injection and long-duration token-expiry behavior were not
verified. Unrelated finance/dashboard mocks and existing Vite chunk/import warnings
remain outside this phase.

## Phase 4 delivery and verification — 25 November 2026

This section supersedes the Phase 3 inventory/discovery limitations above.

1. **Backend modules:** `app/modules/inventory/{models,schemas,management,router,discovery,demo}.py`; `service.py` now resolves canonical database inventory for tenant/inquiry workflows. Shared property image processing/storage is reused.
2. **Frontend changes:** API-backed `roomService`, new `inventoryService`, `InventoryHierarchy`, `BedEditor`; integrated property/building details, room list/detail/form/gallery/image manager, discovery hook/filters/map/search/property details, tenant room search, dashboard inventory counts and related types/utilities. No frontend test framework or dependency added.
3. **Migration:** `0005_inventory` follows `0004_properties`. Run `python -m alembic upgrade head` from Backend after backing up PostgreSQL and image storage. Applied successfully here; `alembic check` reports no new operations. Migration does not reset the database. Production/development downgrade is intentionally blocked: restore the verified pre-migration backup or use a reviewed forward migration. Destructive downgrade exists only for isolated test schemas.
4. **Endpoints:** `GET/POST /properties/{id}/buildings`; `GET/PATCH /buildings/{id}` and `/status`; `GET/POST /buildings/{id}/floors`; `GET/PATCH /floors/{id}` and `/status`; `GET/POST /rooms`; `GET /rooms/summary`, `/rooms/assignable-tenants`; `GET/PATCH /rooms/{id}`, `PATCH /rooms/{id}/status`, `POST /rooms/{id}/archive`; `GET/POST /rooms/{id}/beds`; `PATCH /beds/{id}` and `/status`; `POST /beds/{id}/release`; room image upload/list/delete/order/cover and `/room-images/{id}/{variant}`; public room list/detail, paginated `/public/available-beds`, `/public/discovery`, `/public/discovery/options`. All paths use `/api/v1`.
5. **Relationships:** Property → Building → Floor → Room → Bed; Room → RoomImage. Unique building name/code within property, floor number within building, room number within floor, bed number within room. Operational status is separate from ledger-derived occupancy. Archive and capacity reductions preserve referenced bed IDs.
6. **Allocation migration:** frozen `0005_inventory_catalog.json` is migration input only. It imports 7 buildings, 10 floors, 11 rooms and 23 real bed rows with original IDs. Invented fixture occupants are not allocations. Migration validates inquiry/tenant/occupancy references before adding room and composite bed/room foreign keys; mismatch aborts. An occupancy trigger checks hierarchy consistency. Property-row locking serializes allocation/inventory writes; unique occupancy bed and tenant constraints remain.
7. **Existing occupancy:** the actual database contained zero active ledger rows before migration. No active records were removed or replaced by migration. Existing reference-preservation and conflict paths were also exercised in isolated tests. This is not evidence of migration against a production database with active tenants.
8. **Dropdowns:** property API → property buildings API → building floors API → room/bed APIs. Dependent selections clear when parents change; hierarchy is locked when editing existing rooms. Browser creation selected actual Cedar building/floor and refreshed successfully. Owner property choices were scoped. Rapid-change race/failure injection was not exhaustively exercised in the browser.
9. **Images:** existing Phase 3 processor validates bytes, MIME, extension, dimensions and decoding; JPEG/PNG/WebP only. Files >= 4,194,304 bytes are rejected. Shared storage writes WebP thumbnail/medium/large variants; metadata includes order, cover and dedupe hashes. Lists return cover images; thumbnails/srcset/lightbox use variants. Automated checks covered 3.9 MiB, exact limit, above limit, spoofed/empty/SVG/GIF payloads, dedupe and removal. Browser checked upload, cover/reorder, refresh and exact/oversize/unsupported rejection.
10. **Public discovery:** SQL filters eligible canonical beds by rent, room/sharing type and amenities, with property query/city/type/locality/bounds filters. Only published active properties and active hierarchy with available beds qualify. Tenant details are redacted. Property results, property-detail rooms and tenant bed search are paginated on the server; the homepage intentionally previews 12 available beds. A reserved/occupied/blocked bed is not advertised as available.
11. **Performance:** SQL grouped counts and existence predicates; select-in/joined loading; bounded room pages (100 maximum), property discovery pages (20 maximum), bounded discovery previews and cover-only list payloads. Property-detail page fetches six rooms. Images are lazily loaded. Compatibility selectors still page through scoped inventory, and property summary/hierarchy aggregation is not load-tested at production scale.
12. **Removed mocks:** runtime `development_catalog.json`, frontend `src/mock/rooms.json`, browser inventory generation and fake bed-assignment/release helpers. No runtime room service `tenantStore`, fake waits, or business localStorage fallback. Frozen migration snapshots remain as category 4 migration/seed inputs; room/status/sharing labels are category 3 enums.
13. **Remaining unrelated mocks:** finance/invoices/payments/expenses, complaints, maintenance, leave, visitors, notices, documents, support, reports/audit and portions of dashboard/profile presentation retain their existing fixtures/waits. Several use `tenantStore`; they were not deleted because unfinished modules still depend on them. Dashboard inventory totals now use real APIs, but unrelated activity/payment/complaint panels are not migrated. These modules are not claimed complete.
14. **Actual browser checks:** SuperAdmin login/logout/refresh; building creation, floor creation/edit; room creation with images, edit and refresh; search; cover/reorder; guest discovery/property/room navigation; inquiry submission and Submitted→Contacted→Shortlisted→Approved→conversion with a selected bed; room occupancy update and tenant link; checkout/release and availability restoration; bed reserve/restore; Owner-only room list and cross-property denial; Tenant admin route denial and real bed search; mobile/tablet/desktop layout observations; public map zoom and missing-coordinate state. API/SQL checks verified conversion references and rejected a second allocation with 409. Public property refresh was checked after QA cleanup.
15. **Defects fixed during verification:** duplicate generated migration constraint name; stale floor collection after mutation; fixture-based occupancy assumptions in retained tests; missing public-publication guard for inquiries; UTC browser checkout date off by one day (release now uses trusted server checkout service); reserved-only room availability display; non-finite map bounds; public detail/tenant search truncation; building counts now use database aggregates; full-image edit fetch prevents loss of gallery metadata.
16. **Backend tests:** final full run: **103 passed**, 78.34 seconds (93 retained backend tests plus 10 temporary Phase 4 checks). After temporary-test cleanup, the retained suite passed again: **93 passed**, 68.20 seconds. One existing Starlette/httpx deprecation warning. Existing backend tests remain, adjusted for canonical inventory/migration behavior. Alembic schema check passed.
17. **Frontend checks:** production TypeScript/Vite build and ESLint passed. Existing large-bundle and ineffective dynamic-import warnings remain. No new package/test commands were added for this phase.
18. **Cleanup:** removed temporary QA scripts, test file, oversized/invalid upload fixtures and account metadata. Removed exactly three QA users, two QA tenants and one QA inquiry with their dependent events/sessions. Restored Cedar's temporary QA ownership to its original null value. No user/customer records were targeted.
19. **Reusable demos:** preserved the existing 3 demo properties, 12 property photos/36 variants and five licensed source photos. Added 3 buildings, 3 floors, 3 DEMO-101 rooms, 6 beds and 6 room photos/18 variants. `python -m app.modules.inventory.demo` uses an existing development SuperAdmin login, is disabled outside development, and was run twice to verify stable IDs. Final database: 12 properties, 10 buildings, 13 floors, 14 rooms, 29 beds, 6 room images, 12 property images and zero active occupancies. Fictional demos have no fabricated map coordinates or occupants.
20. **Limits/unverified:** not all 75 requested browser scenarios were run individually. Browser offline/500 injection, every staff role, archive/capacity/image deletion through UI, large-data paging and long-duration token expiry were not fully exercised; backend tests cover several of those business rules. Map marker navigation with a newly created Phase 4 geocoded property was not checked (demos deliberately have no coordinates). Distance sorting applies to the current map/result page; map pins also show the current page. Gender policy is not persisted, so choosing a gender currently returns no matches rather than inventing eligibility. Image operations and room edits are separate transactions; partial image failure leaves the saved room and a retry message. Real SMTP and production-scale/concurrency load testing remain unverified. No claim of zero bugs or exhaustive testing is made.


## Phase 5 — Finance delivery and verification (26 November 2026)

This section supersedes earlier transitional statements about finance and tenant
payments. Finance uses the canonical PostgreSQL tenants/properties and existing
permissions; no financial fixtures are imported into the database.

1. **Backend modules.** Added `app/modules/finance/{models,schemas,service,summary,
   ledger,reports,router}.py` and registered the router/model metadata. Existing
   property serializers now query monthly receipt revenue, and body size limits
   include finance. No new Python or frontend dependencies were introduced.
2. **Frontend integration.** Replaced all four finance services with typed Axios
   requests through the existing client. Added `services/finance/api.ts`,
   `hooks/useFinanceTable.ts` and `components/finance/FinanceFilters.tsx`. Updated
   all invoice/payment/expense/outstanding pages, finance forms, tenant payment and
   dashboard pages, admin tenant summary, dashboard/report/tenant services, finance
   types/calculation/validation/receipt helpers and currency formatting. Shared
   Modal scrolling and async response identity were corrected during browser QA.
3. **Migration.** `0006_finance` follows the verified `0005_inventory` head. It adds
   `finance_invoices`, `finance_invoice_items`, `finance_payments` and
   `finance_expenses` only. Both the real application database and disposable test
   schemas were upgraded. No table reset, `create_all`, mock import or user-record
   conversion is performed.
4. **Endpoints.** All paths below are under `/api/v1/finance`:

   | Method | Path | Purpose |
   | --- | --- | --- |
   | GET, POST | `/invoices` | Scoped server pages / invoice creation |
   | GET | `/invoices/summary` | SQL billed/received/outstanding/overdue totals |
   | GET, PATCH | `/invoices/{id}` | Detail / eligible edit |
   | POST | `/invoices/{id}/cancel` | Retained cancellation without payment history |
   | GET, POST | `/payments` | Scoped server pages / staff receipt recording |
   | GET | `/payments/summary` | SQL collections/pending/overdue/today |
   | GET | `/payments/{id}`, `/payments/{id}/receipt` | Authorized persisted payment/receipt |
   | POST | `/payments/{id}/refund` | Record a completed external refund with reason/actor/time |
   | GET, POST | `/expenses` | Scoped server pages / creation |
   | GET | `/expenses/summary` | SQL paid total/month/year/category totals |
   | GET, PATCH | `/expenses/{id}` | Detail / editing |
   | POST | `/expenses/{id}/cancel` | Retain cancelled expense |
   | GET | `/overview` | SQL dashboard totals and bounded chart/recent/due rows |
   | GET | `/ledger` | Server-paginated invoice/payment ledger |
   | GET | `/tenants/me/summary`, `/tenants/{id}/summary` | Linked tenant/scoped staff balances |
   | GET | `/options/properties`, `/options/tenants` | Authorized canonical entity choices |
   | GET | `/reports/{REVENUE,PAYMENTS,OUTSTANDING,EXPENSES}` | Existing SQL financial reports/export data |

   Lists return `{items,total,offset,limit}`; default 20, maximum 100. Filters include
   bounded query, property/tenant/invoice, status, period, due status, method/category,
   dates and allowlisted sorting expressions. Ledger additionally supports its
   existing all/due/overdue/completed tabs. Dashboard filters use property/from/to.
   Static summary routes precede UUID routes. Creation requires `Idempotency-Key`;
   writes require the existing trusted Origin and CSRF header.
5. **Relationships/integrity.** Invoices reference canonical tenant/property IDs.
   Relational line items carry numeric amounts. Payments reference invoices and
   canonical tenant/property IDs; a composite invoice FK prevents inconsistent
   payment relationships even at the database layer. Users are referenced for
   creation and refund audit. Unique UUID-based document numbers and submission keys,
   positive numeric amounts, date/total checks and enum constraints are enforced.
   No cascade silently deletes payment history. Foreign keys preserve historical
   tenant/property relationships after checkout.
6. **Inventory/rent.** Allocation's saved `Tenant.data.roomRent` supplies the UI rent
   preview, rather than today's mutable room price. Item amounts remain explicitly
   editable as in the existing form, and the server validates all submitted charges.
   Invoices and receipts save identity/location snapshots and their own amounts;
   room rent changes, tenant renaming, property renaming and checkout do not rewrite
   old documents. There is no second occupancy ledger or tenant/property catalog.
7. **Data preservation/backup.** Before upgrading, `pg_dump -Fc` created the ignored
   local backup `Backend/backups/before-finance-20261126-010059.dump` (86,429 bytes),
   alongside the pre-migration table-count manifest. All 24 existing table counts
   were unchanged by the additive migration. Existing auth, inquiries, tenants,
   staff, inventory, occupancy and image data were not deleted or reseeded. QA
   mutations used a separate disposable PostgreSQL cluster, never the application
   database. Reusable Phase 3/4 demos and image files were untouched.
8. **Dropdowns.** Finance options derive scope from existing Owner relationships or
   staff assignments. Accountants do not require unrelated management permissions.
   Property changes clear tenant/invoice/amount, tenant changes clear invoice/amount,
   and async result keys reject obsolete responses. Browser tests verified scoped
   choices, no eligible invoices, current edit selections and selection reset.
   Static business enums remain application constants.
9. **Calculation/payment rules.** Authoritative calculations use Python Decimal
   and PostgreSQL NUMERIC(12,2), with at most two input decimal places. Invoice
   subtotal minus discount plus late fee must be positive. Paid amount is a SQL sum
   of successful persisted payments; cancellation, paid, overdue, partial and issued
   statuses are derived from those records and the due date. Zero/negative/excessive,
   future-dated, wrong-tenant and cancelled-invoice payments are rejected. Electronic
   methods require a transaction reference. An invoice row lock serializes payments,
   edits, cancellation and refund corrections; advisory locks serialize submission
   keys and reused transaction references. Payments and invoice balance changes are
   one transaction. Any recorded payment history blocks invoice edits/cancellation.
   Tenant accounts cannot record successful payments; the fake Pay Now flow is gone.
10. **Expenses/receipts.** Expense amount/category/property/method/status are validated.
    Cancellation retains the expense and excludes it from paid dashboard totals;
    the existing Expense Report separately includes non-cancelled pending expenses.
    Refunds record an already-completed external return and preserve the original
    payment amount/reference/notes plus separate reason, actor and timestamp. No
    payment gateway or money transfer is implemented. Receipt identifiers are stable,
    downloads re-fetch authorized persisted data and retain the original identity
    snapshot. Generating a receipt creates no new payment.
11. **Dashboards/tenant portal/reports.** Admin/Owner and Accountant financial cards,
    charts, property revenue and reports are database-backed. Empty query results
    remain empty, including charts. Tenant identity is loaded from the trusted
    account-to-profile link, not by equating account and tenant UUIDs. Tenant ledger
    rows include every installment, with independently paginated invoices and ledger.
    Checkout leaves existing balances and payment history available without imposing
    settlement or refund policies.
12. **Performance.** SQL joins/aggregates handle balances, statuses, filtering,
    reports, search, totals and sorting. Indexed FKs, billing periods, dates, enums
    and unique identifiers support the common queries. Invoice items use select-in
    loading; ledger pages batch-load selected rows rather than query per result.
    Registers request 20-row pages and debounce search by 300 ms. Option requests
    discard stale results; obsolete responses are not shown while a new filter loads.
    Dashboard recent/due lists are capped at 10 and recent payments at 5. Existing
    outstanding-report previews retain their 40-row limit, with SQL totals over the
    entire authorized matching set.
13. **Removed mocks.** Deleted `src/mock/{invoices,payments,expenses}.json`,
    `src/services/finance/financeStore.ts`, unused Accountant/Tenant dashboard JSON
    and `src/components/finance/PayNowModal.tsx`. Removed fake finance waits, fixture
    CRUD, frontend aggregate/status hydration and financial fixture fields in the
    Admin dashboard scaffold. Repository searches found no remaining imports of
    those deleted sources or finance service references to tenantStore/localStorage.
14. **Remaining boundaries.** Unfinished complaints, visitors, leave, notices,
    document/support and maintenance services retain their isolated legacy mock
    dependencies, including maintenance cost estimates. Admin operational dashboard
    sections still use those unfinished services. They supply no invoice, payment,
    expense or migrated financial statistic. Shared property/tenant fixtures needed
    by unfinished modules and frozen migration/demo sources were preserved.
15. **Actual browser verification.** The real React app and FastAPI ran against a
    disposable PostgreSQL cluster, with explicit QA-only accounts and fixtures.
    Executed: Super Admin login; invoice create, rent preview, edit discount,
    detail/refresh; cash payments of 3,000 then 4,900 against a 7,900 invoice;
    outstanding zero/paid status; receipt preview/download and downloaded content;
    expense create (1,250.50), edit (1,200.25), refresh, search/sort/cancel; dependent
    option reset and no-eligible-invoice state; Accountant assigned-property options,
    dashboard and payments; Owner scoped invoice list and page 2 of 22 records;
    cross-owner and cross-tenant invoice denial; unauthorized Security route 403;
    canonical tenant checkout with 210 outstanding, retained history and receipt;
    Revenue Report and property filter; tenant ledger/summary and phone/tablet/desktop
    layout observations. Browser refresh exercised session restoration. SQL verified
    the same invoice totals, payment sums and expense status. Expected denied network
    requests were observed; console inspections during valid finance flows showed no
    errors. This is not a claim that every requested scenario was individually run.
16. **Defects fixed.** Corrected grouped SQL month expression binding; account/profile
    UUID mismatch; absent accountant-compatible selectors; missing invoice edit UI;
    reused form state on reopen; UTC date defaults showing the previous day; modal
    overflow hiding submit controls; rounded-away paise; tenant history dropping
    earlier successful installments; empty chart fixture fallback; hardcoded property
    revenue; stale previous-filter responses; misleading migration-era tenant text;
    and a zero-capacity occupancy percentage showing NaN during browser QA.
17. **Backend tests.** Full retained suite: **109 passed**, including **16 finance
    cases** on isolated PostgreSQL. Coverage includes invoice validation/totals/
    numbering/edit/cancel/search/pages, payment partial/full/excess/invalid/retry/
    concurrent balance safety/transaction rollback/receipt/refund, expense mutations
    and aggregates, Owner/Accountant/Tenant scopes, unauthorized roles/receipts,
    immutable snapshots, ledger installment paging, reports/property revenue and
    migration upgrade/downgrade/schema comparison. Existing tests were preserved;
    the old migration-head assertion was updated for `0006_finance`. Starlette's
    existing httpx TestClient deprecation warning remains.
18. **Frontend/schema verification.** Production build and ESLint pass. Alembic
    upgrade/current/check pass on the configured application database: head is
    `0006_finance`, with no schema drift. Existing Vite large-chunk and mixed-import
    notices remain. No permanent frontend QA framework/scripts or npm test commands
    were added. Run the final verification commands below with the existing venv.
19. **QA cleanup.** Disposable financial records/accounts lived only in isolated
    PostgreSQL. Temporary QA server scripts, cluster data and downloaded QA receipts
    are removed after the final pass. The intentional application backup is retained
    under ignored `Backend/backups/`. Reusable demo records and images remain intact.
20. **Limits/unverified.** Not all 91 browser scenarios were executed individually.
    Browser offline/500 injection, prolonged token-expiry stress, every role/property
    permutation and production-scale load were not exhaustively tested. Invalid and
    excessive payments, duplicate submissions, refund correction and concurrency
    have backend regression coverage; they were not all forced through the browser.
    Frontend date defaults and backend finance dates use Asia/Kolkata. Entity dropdowns paginate through
    authorized option endpoints; very large option sets may benefit from a future
    searchable selector. No recurring billing, advance payments, automated refunds,
    tax accounting, external payment integration or automated checkout settlement
    was introduced.

### Latest verification rerun

Aligned invoice status, payment date validation, dashboard date ranges and property
revenue with the Asia/Kolkata date. Expense month and year figures now exclude later
periods. Invoice, payment and expense row menus now have one native action button.
On disposable PostgreSQL, the browser saved and reloaded a ₹7,996.75 invoice, a
₹500.25 cash receipt and a ₹75.50 expense. Direct SQL confirmed the ₹7,496.50 balance
and the persisted amounts. The browser displayed the receipt preview and opened all
three row menus with Enter. Build, lint, Alembic current and schema comparison passed.
The retained backend suite passed **110 tests**, including **17 finance cases**.
The added regression verifies that expenses in later months or years stay out of
the corresponding current-period figures.

### Final verification commands

Run from Backend using the existing environment; do not overwrite `.env`:

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m alembic current
.venv\Scripts\python.exe -m alembic check
.venv\Scripts\python.exe -m pytest -q
```

Run from Fronted:

```powershell
npm.cmd ci
npm.cmd run build
npm.cmd run lint
```

### Rollback strategy

Back up the application database and securely archive all finance records before an
explicit rollback. `alembic downgrade 0005_inventory` removes only the four finance
tables and therefore destroys their finance data; it does not reverse unrelated
modules. Prefer rolling back application code while preserving the tables, or
restoring the verified pre-migration custom-format dump into a separate database
for recovery/inspection. Do not overwrite the live database or automatically run a
downgrade during deployment. The backup contains private application data and is
ignored by Git. Restart any API process that is not running with reload after
installing the new module; the normal frontend API default remains port 8000.
