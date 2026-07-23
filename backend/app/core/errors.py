"""Typed error hierarchy mapped to RFC 9457 problem responses."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_correlation_id, logger

PROBLEM_BASE = "https://proxsync.dev/errors"
PROBLEM_CONTENT_TYPE = "application/problem+json"


class AppError(Exception):
    status_code: int = 500
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


class ValidationFailed(AppError):
    status_code = 400
    error_type = "validation-failed"
    title = "Validation failed"


class SchemaValidationFailed(ValidationFailed):
    # Spelled numerically: Starlette renamed its 422 constant between releases.
    status_code = 422
    error_type = "schema-validation-failed"
    title = "Request schema validation failed"


class AuthenticationFailed(AppError):
    status_code = 401
    error_type = "authentication-failed"
    title = "Authentication failed"


class PermissionDenied(AppError):
    status_code = 403
    error_type = "permission-denied"
    title = "Permission denied"


class CsrfFailed(AppError):
    status_code = 403
    error_type = "csrf-failed"
    title = "CSRF validation failed"


class NotFound(AppError):
    status_code = 404
    error_type = "not-found"
    title = "Resource not found"


class Conflict(AppError):
    status_code = 409
    error_type = "conflict"
    title = "Conflict"


class AccountLocked(AppError):
    status_code = 423
    error_type = "account-locked"
    title = "Account temporarily locked"


class RateLimited(AppError):
    status_code = 429
    error_type = "rate-limited"
    title = "Too many requests"

    def __init__(self, detail: str, *, retry_after: int) -> None:
        super().__init__(detail, extra={"retry_after": retry_after})
        self.retry_after = retry_after


class ConfigurationError(AppError):
    status_code = 500
    error_type = "configuration-error"
    title = "Configuration error"


class AgentUnavailable(AppError):
    status_code = 503
    error_type = "agent-unavailable"
    title = "Backup agent unavailable"


class AgentError(AppError):
    """The agent answered, but refused or failed the request."""

    status_code = 502
    error_type = "agent-error"
    title = "Backup agent reported an error"

    def __init__(
        self, detail: str, *, agent_status: int | None = None, problem: dict[str, Any] | None = None
    ) -> None:
        extra: dict[str, Any] = {}
        if agent_status is not None:
            extra["agent_status"] = agent_status
        if problem:
            extra["agent_problem"] = problem
        super().__init__(detail, extra=extra)
        self.agent_status = agent_status


class UpstreamError(AppError):
    status_code = 502
    error_type = "upstream-error"
    title = "Upstream service error"


class TelegramError(AppError):
    """Telegram refused the message, or could not be reached.

    `retryable` is the only thing the outbox worker needs to decide with, and it is not a
    property of the status code alone: a 400 "chat not found" will fail identically forever,
    while a 429 or a dropped connection is the same message arriving at a bad moment. Retrying
    the first would burn the attempt budget that the second needs.
    """

    status_code = 502
    error_type = "telegram-error"
    title = "Telegram rejected the message"

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool,
        error_code: int | None = None,
        description: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        extra: dict[str, Any] = {}
        if error_code is not None:
            extra["telegram_error_code"] = error_code
        if description is not None:
            extra["telegram_description"] = description
        if retry_after is not None:
            extra["retry_after"] = retry_after
        super().__init__(detail, extra=extra)
        self.retryable = retryable
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after


def _problem_response(request: Request, error: AppError) -> JSONResponse:
    headers = {}
    if isinstance(error, RateLimited):
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_problem(request.url.path),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log = logger.bind(path=request.url.path, error_type=exc.error_type)
        if exc.status_code >= 500:
            log.error("request_failed", detail=exc.detail, exc_info=exc)
        else:
            log.warning("request_rejected", detail=exc.detail)
        return _problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _schema_error(request: Request, exc: RequestValidationError) -> JSONResponse:
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
        return _problem_response(request, AppError("An unexpected error occurred"))
