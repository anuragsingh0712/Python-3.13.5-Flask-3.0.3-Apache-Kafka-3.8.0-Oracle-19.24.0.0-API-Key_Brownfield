# Checkpoint

## Design
- Flask 3.0.3 / Python 3.13 REST API on port 8000; layered `app` package with DTO validation (Pydantic), thin blueprints, injected repositories/services, Oracle repository abstraction and in-memory test repository.
- API envelope: `{data, meta}` successes and `{error:{code,message,timestamp,correlation_id}}` errors. All `/api/v1/*` endpoints require `X-API-Key`; roles are ADMIN/STAFF/TRAINER.
- Cursor collections use opaque base64 integer IDs and stable `id` ordering; filters/sort are allow-listed.
- Kafka topic convention `gym.<entity>.<action>.v1`, events publish only after repository transaction commit via injected publisher.
- Entity groups: members; memberships; attendance; trainers; workouts; classes; payments; reports. Shared service contracts use repository protocol methods and normalized dict DTOs.
- Oracle migration owns exactly 11 specified tables; Docker infra uses `gvenzl/oracle-free:23-slim` and `apache/kafka:3.8.0`.

## Status
- [x] STEP 1 design
- [x] Scaffold/config/schema/migration
- [x] Resource groups
  - [x] Members: `app/members.py` provides DTOs, service, cursor collection/read/write routes, uniqueness enforcement, and post-commit events.
- [x] pytest (2 passed), ruff (clean), native Gunicorn boot and live HTTP report
- [!] Docker image pull/build/run verification blocked: Docker daemon unavailable
- [x] Payments/reports unit — added `app/payments.py` with injected repository-backed payment creation/listing, bounded transactional refunds, role/auth rules, post-commit events, and safe named reports; added focused `app/test_payments.py` coverage.
- [x] Workouts resource — added `app/workouts.py` with plan/exercise schemas, transactional service, authenticated blueprint endpoints, trainer ownership checks, timestamps, and post-commit creation events.

## Unit status — attendance
- [x] Added `app/attendance.py`: DTO validation, transactional attendance service, role-protected check-in/check-out/list blueprint routes, cursor/filter pagination, and post-commit attendance events.
- [ ] Attendance tests were not present in the workspace and could not be run in this unit.
- [x] Trainers resource: added `app/trainers.py` with injected repository/publisher service, trainer CRUD collection routes, transactional trainer assignment lifecycle, validation, RBAC, and post-commit assignment event publishing.

## Resume fix design
- Canonical Oracle/application names are the migration names: plans `is_active`; memberships `start_date`, `end_date`, `price_paid`, `remaining_visits`; attendance `check_in_at`, `check_out_at`; enrollments `class_id`; payments `method`, `reference_id`, `created_at`. Payment status remains `PENDING|COMPLETED|FAILED|REFUNDED`; partial refunds retain `COMPLETED` with `refunded_amount`.
- Production wiring defaults to `OracleRepository` when `APP_ENV=production` (or `USE_ORACLE=true`); explicit injected repositories remain test seams. Oracle CRUD uses static table/column allow-lists, bind variables, transactions with commit/rollback, constraint translation, and `SELECT ... FOR UPDATE`.
- Kafka publisher buffers transaction events and flushes only after commit. Production default is a `KafkaEventPublisher`; tests can inject `CapturingPublisher`/`NullEventPublisher`.
- API-key configuration accepts only nonempty mappings to ADMIN/STAFF/TRAINER role lists; authentication uses `secrets.compare_digest` against every configured key to avoid direct lookup timing leaks.
- Parallel repair groups: membership/attendance/classes/payment-report schema alignment; foundation persistence/auth/infra; test suite/OpenAPI/Docker documentation.
- [x] Resume verifier remediation
  - [x] Real Oracle pool-backed CRUD/transactions/row-locking, deferred post-commit Kafka producer, constant-time API-key middleware, production factory wiring, idempotent migration runner, and non-root Docker image.
  - [x] Canonical schema names enforced in memberships, attendance, classes, payments, reports, and trainer assignments; removed legacy Oracle column aliases.
  - [x] Explicit OpenAPI operations include request bodies and path/query parameters; role/auth and resource/event tests expanded, including Oracle transaction/locking fake-driver coverage.
  - [x] Validation: `python -m pytest -q` (13 passed); `python -m ruff check app scripts` (clean); native one-worker Gunicorn live workflow passed and produced `tests-artifacts/api_test_report.xlsx` + `changes_report.docx`. A final native health boot probe also passed after the final schema-mapping alignment.
  - [!] Docker image pull/build/run verification attempted but blocked by unavailable Docker daemon (`Cannot connect to the Docker daemon at unix:///var/run/docker.sock`). Dockerfile and compose contract were still updated (non-root user, app port 8000, Oracle/Kafka pinned images, schema command).

## Unit updates
- Members resource implemented in `app/members.py`; no focused tests added (test runner was not available in this unit environment).
- Classes resource implemented in `app/classes.py`: class DTOs/service/blueprint, authenticated reads, ADMIN/STAFF class and enrollment writes, serialized capacity/membership enrollment validation, enrollment cancellation state updates, and post-commit class/enrollment events.
- Membership resource group implemented in `app/memberships.py`: plan and membership DTOs/services/blueprint, transactional state changes, snapshot terms, pagination, RBAC, and post-commit membership events. No shared registration or schema files changed.

## Unit status — foundation remediation
- [x] Replaced the Oracle placeholder with bound-SQL pooled `OracleRepository`, static table/column maps, identity `RETURNING`, CRUD/filtering, row locks, transaction commit/rollback, and domain error translation.
- [x] Added production Oracle/Kafka factories, transaction-aware deferred publisher, API-key constant-time middleware, hardened settings validation, idempotent schema runner, and unprivileged Docker user.
- [ ] Focused command execution was unavailable in this environment; files were reviewed for syntax only.
- [x] Membership/attendance/classes schema consistency repair: canonical plan, membership, attendance, and enrollment names are now used by DTOs, persistence, outputs, and events; classes allow only SCHEDULED/CANCELLED. Enrollment locks class and qualifying membership in its transaction, rejects exhausted/expired memberships, and consumes finite visits. Added `app/test_membership_attendance_classes.py` focused coverage. Test/lint execution was unavailable because this environment exposes no shell runner.
- Payments/reports repair: `app/payments.py` now persists canonical payment columns (`method`, `reference_id`, `created_at`), retains `COMPLETED` for partial refunds and uses `REFUNDED` only at the full amount, and uses schema-specific allow-listed report date columns/tables. Added `app/test_payments_reports.py` regression coverage. Test execution was not available through this unit's tool interface.

## Unit status — documentation/test deliverables
- [x] Replaced runtime-route-derived OpenAPI with an explicit OpenAPI 3.0.3 contract in `app/openapi.py`, covering public documentation routes and all API resources, real path/query parameters, JSON request schemas, response/error declarations, and `X-API-Key` security for business operations.
- [x] Added `app/test_documentation.py` Flask test-client coverage for public docs, key/role rejection, members, trainers, workouts, and post-commit event publication using `InMemoryRepository`/`CapturingPublisher` injection seams.
- [ ] Focused pytest/ruff not run: this unit environment exposed filesystem tools only and no command-execution tool.

## Verifier remediation design (resumed)
- Member contract uses required `name`, `email`, `phone`, and client-supplied `joining_date`; optional `date_of_birth`, `address`, and `emergency_contact`. `name` is canonical; legacy split-name API fields are removed. Migration is aligned by making split columns nullable for compatibility while API/repository use canonical fields.
- Membership plans accept `price >= 0` and nullable `visit_limit` (`null` means unlimited). Renewal closes the existing ACTIVE lifecycle record and creates a new immutable snapshot record from the current plan, preserving the original row's price, dates, plan ID, and visit balance.
- Trainers accept ACTIVE, INACTIVE, and ON_LEAVE. `max_members` is nullable (unlimited); capacity comparison happens only for a non-null value.
- Named reports are: `daily-attendance`, `monthly-attendance`, `active-members`, `expiring-memberships`, `expired-memberships`, `payment-statistics`, `trainer-statistics`, `member-statistics`, and `class-enrollment`; report aggregates derive presence from `check_in_at` rather than a nonexistent attendance status.
- Independent repair groups: members; memberships; trainers; reports; test/verification + deployment. Shared contract is the canonical schema above, response envelope/auth unchanged, and OpenAPI must track each altered DTO/report enum.
- [ ] Verifier gap remediation in progress
