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
- [ ] Scaffold/config/schema/migration
- [ ] Resource groups
- [ ] Tests/lint/boot/report/Docker verification
