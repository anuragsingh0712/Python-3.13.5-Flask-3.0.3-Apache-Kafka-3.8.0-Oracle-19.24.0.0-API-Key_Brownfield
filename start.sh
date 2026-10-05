#!/bin/sh
set -eu
exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers ${GUNICORN_WORKERS:-2} --access-logfile - --error-logfile - wsgi:app
