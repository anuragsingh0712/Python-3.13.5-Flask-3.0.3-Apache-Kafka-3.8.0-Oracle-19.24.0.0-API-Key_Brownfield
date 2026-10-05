COMMIT_MESSAGE: feat: complete Oracle persistence remediation

## Summary
Implemented Oracle-backed persistence, transaction-safe Kafka publishing, schema-consistent gym workflows, hardened API-key authentication, documented OpenAPI contracts, and expanded automated coverage.

## Validation
- `python -m pytest -q`
- `python -m ruff check app scripts`
- Native Gunicorn live HTTP verification and generated report artifacts.

## Deployment
Production compose config enables Oracle mode, publishes API port 8000, and runs the service as a non-root user.
