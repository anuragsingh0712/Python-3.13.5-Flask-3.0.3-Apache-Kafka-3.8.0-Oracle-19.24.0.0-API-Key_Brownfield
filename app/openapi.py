"""Hand-authored OpenAPI 3.0 document for the public Flask API."""
from __future__ import annotations

from typing import Any

from flask import Flask, Response


JSON = {"content": {"application/json": {"schema": {"type": "object"}}}}
ERRORS = {
    "401": {"description": "Authentication required", **JSON},
    "403": {"description": "Forbidden", **JSON},
    "404": {"description": "Resource not found", **JSON},
    "409": {"description": "Conflict", **JSON},
    "422": {"description": "Validation failure", **JSON},
}


def _parameter(name: str, schema: dict[str, Any], *, location: str = "query", required: bool = False) -> dict[str, Any]:
    return {"name": name, "in": location, "required": required, "schema": schema}


def _body(schema_name: str) -> dict[str, Any]:
    return {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}}}


def _operation(operation_id: str, *, body: str | None = None, parameters: list[dict[str, Any]] | None = None,
               status: str = "200", description: str = "Success", secured: bool = True) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "responses": {status: {"description": description, **JSON}, **ERRORS},
    }
    if secured:
        operation["security"] = [{"ApiKeyAuth": []}]
    if parameters:
        operation["parameters"] = parameters
    if body:
        operation["requestBody"] = _body(body)
    return operation


ID = {"type": "integer", "minimum": 1}
CURSOR = _parameter("cursor", {"type": "string", "description": "Opaque cursor returned in meta.next_cursor."})
LIMIT = _parameter("limit", {"type": "integer", "minimum": 1, "maximum": 100, "default": 20})
STATUS = _parameter("status", {"type": "string"})


def build_spec(app: Flask | None = None) -> dict[str, Any]:
    """Return a deliberately explicit contract, independent of Flask route introspection."""
    path_id = lambda name: _parameter(name, ID, location="path", required=True)
    paths: dict[str, Any] = {
        "/health": {"get": _operation("health", secured=False)},
        "/docs": {"get": {"operationId": "docs", "responses": {"200": {"description": "Swagger UI HTML", "content": {"text/html": {"schema": {"type": "string"}}}}}}},
        "/openapi.json": {"get": {"operationId": "openapi", "responses": {"200": {"description": "OpenAPI document", **JSON}}}},
        "/api/v1/openapi.json": {"get": _operation("authenticated_openapi")},
        "/api/v1/members": {
            "get": _operation("list_members", parameters=[CURSOR, LIMIT, _parameter("status", {"type": "string", "enum": ["active", "inactive", "suspended"]}), _parameter("search", {"type": "string"}), _parameter("sort", {"type": "string", "enum": ["id"]}), _parameter("order", {"type": "string", "enum": ["asc", "desc"]})]),
            "post": _operation("create_member", body="MemberCreate", status="201", description="Member created"),
        },
        "/api/v1/members/{member_id}": {"get": _operation("get_member", parameters=[path_id("member_id")]), "patch": _operation("update_member", body="MemberPatch", parameters=[path_id("member_id")])},
        "/api/v1/trainers": {"get": _operation("get_trainers", parameters=[CURSOR, LIMIT, STATUS]), "post": _operation("post_trainer", body="TrainerCreate", status="201", description="Trainer created")},
        "/api/v1/trainers/{trainer_id}": {"patch": _operation("patch_trainer", body="TrainerPatch", parameters=[path_id("trainer_id")])},
        "/api/v1/trainer-assignments": {"post": _operation("post_trainer_assignment", body="TrainerAssignmentCreate", status="201", description="Assignment created")},
        "/api/v1/trainer-assignments/{assignment_id}": {"delete": _operation("delete_trainer_assignment", parameters=[path_id("assignment_id")])},
        "/api/v1/workout-plans": {
            "get": _operation("list_workout_plans", parameters=[CURSOR, LIMIT, _parameter("member_id", ID), _parameter("trainer_id", ID)]),
            "post": _operation("create_workout_plan", body="WorkoutPlanCreate", status="201", description="Workout plan created"),
        },
        "/api/v1/workout-plans/{plan_id}": {"patch": _operation("update_workout_plan", body="WorkoutPlanPatch", parameters=[path_id("plan_id")]), "delete": _operation("delete_workout_plan", parameters=[path_id("plan_id")], status="204", description="Workout plan deleted")},
        "/api/v1/workout-plans/{plan_id}/exercises": {"post": _operation("create_workout_exercise", body="WorkoutExerciseCreate", parameters=[path_id("plan_id")], status="201", description="Exercise created")},
        "/api/v1/workout-plans/{plan_id}/exercises/{exercise_id}": {"delete": _operation("delete_workout_exercise", parameters=[path_id("plan_id"), path_id("exercise_id")], status="204", description="Exercise deleted")},
        "/api/v1/attendance/check-in": {"post": _operation("check_in", body="AttendanceCheckIn", status="201", description="Check-in created")},
        "/api/v1/attendance/{attendance_id}/check-out": {"post": _operation("check_out", body="AttendanceCheckOut", parameters=[path_id("attendance_id")])},
        "/api/v1/attendance": {"get": _operation("list_attendance", parameters=[CURSOR, LIMIT, _parameter("member_id", ID)])},
        "/api/v1/membership-plans": {"get": _operation("list_membership_plans", parameters=[CURSOR, LIMIT, _parameter("is_active", {"type": "boolean"})]), "post": _operation("create_membership_plan", body="MembershipPlanCreate", status="201", description="Plan created")},
        "/api/v1/membership-plans/{plan_id}": {"patch": _operation("update_membership_plan", body="MembershipPlanPatch", parameters=[path_id("plan_id")])},
        "/api/v1/memberships": {"get": _operation("list_memberships", parameters=[CURSOR, LIMIT, _parameter("member_id", ID), STATUS]), "post": _operation("create_membership", body="MembershipCreate", status="201", description="Membership created")},
        "/api/v1/memberships/{membership_id}/{action}": {"post": _operation("membership_action", body="MembershipAction", parameters=[path_id("membership_id"), _parameter("action", {"type": "string", "enum": ["renew", "cancel", "suspend", "expire"]}, location="path", required=True)])},
        "/api/v1/classes": {"get": _operation("list_classes", parameters=[CURSOR, LIMIT, STATUS]), "post": _operation("create_class", body="ClassCreate", status="201", description="Class created")},
        "/api/v1/classes/{class_id}": {"get": _operation("get_class", parameters=[path_id("class_id")]), "patch": _operation("patch_class", body="ClassPatch", parameters=[path_id("class_id")])},
        "/api/v1/classes/{class_id}/enrollments": {"post": _operation("create_enrollment", body="EnrollmentCreate", parameters=[path_id("class_id")], status="201", description="Enrollment created")},
        "/api/v1/classes/{class_id}/enrollments/{enrollment_id}": {"delete": _operation("delete_enrollment", parameters=[path_id("class_id"), path_id("enrollment_id")])},
        "/api/v1/payments": {"get": _operation("list_payments", parameters=[CURSOR, LIMIT, _parameter("member_id", ID), _parameter("membership_id", ID), STATUS]), "post": _operation("create_payment", body="PaymentCreate", status="201", description="Payment created")},
        "/api/v1/payments/{payment_id}/refunds": {"post": _operation("refund_payment", body="RefundCreate", parameters=[path_id("payment_id")])},
        "/api/v1/reports/{report_name}": {"get": _operation("report", parameters=[_parameter("report_name", {"type": "string", "enum": ["revenue", "payments"]}, location="path", required=True), _parameter("from", {"type": "string", "format": "date"}), _parameter("to", {"type": "string", "format": "date"})])},
    }
    schemas = {
        "MemberCreate": {"type": "object", "required": ["first_name", "last_name", "email", "phone"], "properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "email": {"type": "string", "format": "email"}, "phone": {"type": "string"}, "status": {"type": "string", "enum": ["active", "inactive", "suspended"]}, "date_of_birth": {"type": "string", "format": "date", "nullable": True}}},
        "MemberPatch": {"type": "object", "minProperties": 1, "properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "email": {"type": "string", "format": "email"}, "phone": {"type": "string"}, "status": {"type": "string", "enum": ["active", "inactive", "suspended"]}, "date_of_birth": {"type": "string", "format": "date", "nullable": True}}},
        "TrainerCreate": {"type": "object", "required": ["email", "phone", "max_members"], "properties": {"name": {"type": "string"}, "first_name": {"type": "string"}, "last_name": {"type": "string"}, "email": {"type": "string", "format": "email"}, "phone": {"type": "string"}, "specialization": {"type": "string"}, "status": {"type": "string", "enum": ["ACTIVE", "INACTIVE"]}, "max_members": ID}},
        "TrainerPatch": {"type": "object", "minProperties": 1, "properties": {"name": {"type": "string"}, "email": {"type": "string", "format": "email"}, "phone": {"type": "string"}, "specialization": {"type": "string"}, "status": {"type": "string", "enum": ["ACTIVE", "INACTIVE"]}, "max_members": ID}},
        "TrainerAssignmentCreate": {"type": "object", "required": ["trainer_id", "member_id"], "properties": {"trainer_id": ID, "member_id": ID}},
        "WorkoutPlanCreate": {"type": "object", "required": ["member_id", "trainer_id", "name"], "properties": {"member_id": ID, "trainer_id": ID, "name": {"type": "string"}, "description": {"type": "string", "nullable": True}}},
        "WorkoutPlanPatch": {"type": "object", "minProperties": 1, "properties": {"member_id": ID, "trainer_id": ID, "name": {"type": "string"}, "description": {"type": "string", "nullable": True}}},
        "WorkoutExerciseCreate": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "sets": ID, "repetitions": ID, "weight": {"type": "number", "minimum": 0}, "duration_seconds": ID, "rest_seconds": {"type": "integer", "minimum": 0}, "difficulty": {"type": "string"}}},
        "AttendanceCheckIn": {"type": "object", "required": ["member_id"], "properties": {"member_id": ID, "check_in_at": {"type": "string", "format": "date-time"}}},
        "AttendanceCheckOut": {"type": "object", "properties": {"check_out_at": {"type": "string", "format": "date-time"}}},
        "MembershipPlanCreate": {"type": "object", "required": ["name", "price", "visit_limit", "duration_days"], "properties": {"name": {"type": "string"}, "price": {"type": "string"}, "visit_limit": ID, "duration_days": ID, "is_active": {"type": "boolean"}}}, "MembershipPlanPatch": {"type": "object", "minProperties": 1, "properties": {"name": {"type": "string"}, "price": {"type": "string"}, "visit_limit": ID, "duration_days": ID, "is_active": {"type": "boolean"}}}, "MembershipCreate": {"type": "object", "required": ["member_id", "plan_id"], "properties": {"member_id": ID, "plan_id": ID, "start_date": {"type": "string", "format": "date"}}}, "MembershipAction": {"type": "object", "properties": {"start_date": {"type": "string", "format": "date"}}},
        "ClassCreate": {"type": "object", "required": ["name", "trainer_id", "scheduled_at", "capacity"], "properties": {"name": {"type": "string"}, "trainer_id": ID, "scheduled_at": {"type": "string", "format": "date-time"}, "capacity": ID, "duration_minutes": ID}}, "ClassPatch": {"type": "object", "minProperties": 1, "properties": {"name": {"type": "string"}, "trainer_id": ID, "scheduled_at": {"type": "string", "format": "date-time"}, "capacity": ID, "duration_minutes": ID, "status": {"type": "string", "enum": ["SCHEDULED", "CANCELLED"]}}}, "EnrollmentCreate": {"type": "object", "required": ["member_id"], "properties": {"member_id": ID}},
        "PaymentCreate": {"type": "object", "required": ["member_id", "amount", "method", "reference_id"], "properties": {"member_id": ID, "membership_id": {"allOf": [ID], "nullable": True}, "amount": {"type": "string", "pattern": "^\\d+(\\.\\d{1,2})?$"}, "method": {"type": "string", "enum": ["CASH", "CARD", "UPI", "BANK_TRANSFER", "OTHER"]}, "reference_id": {"type": "string"}, "created_at": {"type": "string", "format": "date-time"}, "status": {"type": "string", "enum": ["PENDING", "COMPLETED", "FAILED", "REFUNDED"]}}},
        "RefundCreate": {"type": "object", "required": ["amount"], "properties": {"amount": {"type": "string"}, "reason": {"type": "string", "nullable": True}}},
    }
    return {"openapi": "3.0.3", "info": {"title": "Gym Management API", "version": "1.0.0", "description": "REST JSON API. Collections use opaque cursor + limit (1-100), stable ID ordering."}, "servers": [{"url": "/"}], "components": {"securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}, "schemas": schemas}, "paths": paths}


def register_public_routes(app: Flask) -> None:
    @app.get("/health")
    def health() -> Response:
        return Response('{"status":"ok"}', mimetype="application/json")

    @app.get("/openapi.json")
    def public_openapi() -> Response:
        return build_spec(app)

    @app.get("/api/v1/openapi.json")
    def authenticated_openapi() -> Response:
        return build_spec(app)

    @app.get("/docs")
    def docs() -> Response:
        return Response("""<!doctype html><html><body><div id='swagger-ui'></div><script src='https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js'></script><script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui'})</script></body></html>""", mimetype="text/html")
