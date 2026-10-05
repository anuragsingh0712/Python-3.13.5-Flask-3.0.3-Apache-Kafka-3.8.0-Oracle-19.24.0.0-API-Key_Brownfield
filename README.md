# Gym Management API

Production-oriented Flask REST JSON API under `/api/v1` for members, memberships, attendance, trainers, workouts, classes, payments, and reports.

## Run

For a native process, copy the documented example and replace its illustrative values:

```sh
cp .env.example .env
# replace the illustrative API keys/password before a real deployment
sh start.sh
```

Docker Compose is self-contained and does **not** require an untracked `.env` file:

```sh
docker compose up --build
```

Compose uses the same deliberately non-secret illustrative values shown in `.env.example` (`replace-*-key` and `replace-me`) so a fresh checkout starts consistently. They are only suitable for local evaluation: change the `api` and `oracle` environment values (or provide an approved Compose override) before exposing the deployment.

The service binds `0.0.0.0:8000`. `GET /health`, `/docs`, and `/openapi.json` are public. `/docs` loads Swagger UI; all versioned business endpoints, including `/api/v1/openapi.json`, require `X-API-Key`.

## Authentication and roles

`API_KEY` is non-secret deployment configuration containing a JSON mapping of API-key strings to role arrays:

```json
{"admin-deployment-value":["ADMIN"],"staff-deployment-value":["STAFF"],"trainer-deployment-value":["TRAINER"]}
```

Never commit deployed values. Missing/invalid keys return 401; valid keys without the required role return 403. ADMIN/STAFF perform management mutations. Trainers have read access where appropriate and may mutate only their own workout plans using `X-Trainer-ID`.

## Collections

Collection endpoints accept `limit` (1–100) and an opaque `cursor`. Ordering is stable by monotonic ID. Resource-specific documented filters include member status/search, membership history, attendance `member_id`, `membership_id`, `from_date`, and `to_date`, trainer status, class status, and payment history. `next_cursor` is null/absent at the final page.

## Infrastructure

`docker-compose.yml` declares the API, free Oracle-compatible `gvenzl/oracle-free:23-slim`, and Kafka 3.8.0. The `Dockerfile` uses Python 3.13.5 and Gunicorn. Start infrastructure/deployment with Docker Compose in an environment with Docker available. Oracle DDL is in `migrations/001_initial.sql`; it contains exactly the eleven domain tables and supporting constraints/indexes.

## Architecture

Routes are thin Flask blueprints. Pydantic DTOs validate request payloads; services own transactional rules; injected repositories isolate persistence; publisher injection ensures events are issued only after a successful transaction. The in-memory repository/publisher test doubles support unit tests when Oracle/Kafka are unavailable. Production repository wiring is the `OracleRepository` adapter seam and must use bound parameters plus `SELECT ... FOR UPDATE` for critical workflows.

## Verification artifacts

Observed HTTP verification results are in `tests-artifacts/api_test_report.xlsx`; the delivery summary is `tests-artifacts/project_report.docx`.
