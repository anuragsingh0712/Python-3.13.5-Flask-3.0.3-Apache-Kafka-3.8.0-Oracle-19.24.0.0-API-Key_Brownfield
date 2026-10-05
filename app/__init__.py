"""Gym management API application factory."""
from __future__ import annotations

from flask import Flask

from app.attendance import AttendanceService, attendance_bp
from app.classes import ClassesService, classes_bp
from app.core import Settings, configure_app
from app.members import configure_members, members_bp
from app.memberships import configure_memberships, memberships_bp
from app.payments import bind_payments, payments_bp, reports_bp
from app.repository import EventPublisher, InMemoryRepository, NullEventPublisher, Repository
from app.trainers import configure_trainers, trainers_bp
from app.workouts import init_workouts, workouts_bp


def create_app(repository: Repository | None = None, publisher: EventPublisher | None = None, settings: Settings | None = None) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    resolved_settings = settings or Settings.from_env()
    repo = repository or InMemoryRepository()
    event_publisher = publisher or NullEventPublisher()
    app.extensions["repository"] = repo
    app.extensions["event_publisher"] = event_publisher
    configure_app(app, resolved_settings)

    configure_members(repo, event_publisher)
    configure_memberships(repo, event_publisher)
    app.extensions["attendance_service"] = AttendanceService(repo, event_publisher)
    configure_trainers(repo, event_publisher)
    init_workouts(app, repo, event_publisher)
    app.extensions["classes_service"] = ClassesService(repo, event_publisher)
    bind_payments(repo, event_publisher)
    for blueprint in (members_bp, memberships_bp, attendance_bp, trainers_bp, payments_bp, reports_bp):
        app.register_blueprint(blueprint, url_prefix="/api/v1")
    app.register_blueprint(workouts_bp)
    app.register_blueprint(classes_bp)

    from app.openapi import register_public_routes
    register_public_routes(app)
    return app
