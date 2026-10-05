"""Hand-authored-at-runtime OpenAPI document for Flask routes."""
from __future__ import annotations
from typing import Any
from flask import Flask, Response


def build_spec(app: Flask) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or rule.rule in {"/health", "/docs", "/openapi.json"}:
            continue
        path = rule.rule.replace("<int:", "{").replace("<", "{").replace(">", "}")
        operations: dict[str, Any] = {}
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            operations[method.lower()] = {
                "operationId": rule.endpoint.replace(".", "_"),
                "security": [{"ApiKeyAuth": []}],
                "responses": {"200": {"description": "Success"}, "401": {"description": "Authentication required"}, "403": {"description": "Forbidden"}, "422": {"description": "Validation failure"}},
            }
        paths[path] = operations
    return {"openapi": "3.0.3", "info": {"title": "Gym Management API", "version": "1.0.0", "description": "REST JSON API. Collections use opaque cursor + limit (1-100), stable ID ordering."}, "servers": [{"url": "/"}], "components": {"securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}}, "paths": paths}


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
