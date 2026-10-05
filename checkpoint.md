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

## Unit updates
- Members resource implemented in `app/members.py`; no focused tests added (test runner was not available in this unit environment).
- Classes resource implemented in `app/classes.py`: class DTOs/service/blueprint, authenticated reads, ADMIN/STAFF class and enrollment writes, serialized capacity/membership enrollment validation, enrollment cancellation state updates, and post-commit class/enrollment events.
- Membership resource group implemented in `app/memberships.py`: plan and membership DTOs/services/blueprint, transactional state changes, snapshot terms, pagination, RBAC, and post-commit membership events. No shared registration or schema files changed.
