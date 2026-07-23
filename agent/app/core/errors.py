"""Typed error hierarchy mapped to RFC 9457 problem responses.

The agent never leaks a stack trace or a raw command line to the caller; ``detail`` is a
message written for a human operator reading the dashboard.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_correlation_id, logger

PROBLEM_BASE = "https://proxsync.dev/errors"
PROBLEM_CONTENT_TYPE = "application/problem+json"


class AgentError(Exception):
    """Base class for every error the agent reports deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = "internal-error"
    title: str = "Internal error"

    def __init__(self, detail: str, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra or {}

    def to_problem(self, instance: str) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{PROBLEM_BASE}/{self.error_type}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            problem["correlation_id"] = correlation_id
        problem.update(self.extra)
        return problem


class ValidationFailed(AgentError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "validation-failed"
    title = "Validation failed"


class SchemaValidationFailed(ValidationFailed):
    """Body failed Pydantic validation before any host-level check ran."""

    # Spelled numerically: Starlette renamed this constant, and the agent must build against
    # whichever version the host distribution ships.
    status_code = 422
    error_type = "schema-validation-failed"
    title = "Request schema validation failed"


class AuthenticationFailed(AgentError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "authentication-failed"
    title = "Authentication failed"


class ClientNotAllowed(AgentError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "client-not-allowed"
    title = "Client address not allowed"


class NotFound(AgentError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not-found"
    title = "Resource not found"


class ConcurrencyConflict(AgentError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "concurrency-conflict"
    title = "Another task holds this slot"


class GuestLocked(AgentError):
    status_code = status.HTTP_423_LOCKED
    error_type = "guest-locked"
    title = "Guest is locked by another operation"


class InsufficientStorage(AgentError):
    status_code = status.HTTP_507_INSUFFICIENT_STORAGE
    error_type = "insufficient-storage"
    title = "Insufficient storage"


class ExecutionFailed(AgentError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type = "execution-failed"
    title = "Command execution failed"


def _problem_response(request: Request, error: AgentError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_problem(request.url.path),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentError)
    async def _agent_error(request: Request, exc: AgentError) -> JSONResponse:
        log = logger.bind(path=request.url.path, error_type=exc.error_type)
        if exc.status_code >= 500:
            log.error("request_failed", detail=exc.detail, exc_info=exc)
        else:
            log.warning("request_rejected", detail=exc.detail)
        return _problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(part) for part in err["loc"][1:]), "message": err["msg"]}
            for err in exc.errors()
        ]
        logger.warning("request_schema_invalid", path=request.url.path, errors=errors)
        return _problem_response(
            request,
            SchemaValidationFailed(
                "Request body failed schema validation", extra={"errors": errors}
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", path=request.url.path, exc_info=exc)
        return _problem_response(request, ExecutionFailed("An unexpected error occurred"))
