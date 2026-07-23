"""Authentication endpoints.

Cookie strategy: the refresh token is `HttpOnly; Secure; SameSite=Strict` and scoped to
`/api/v1/auth`, so it is never attached to any other request — an XSS payload cannot read it
and a CSRF cannot aim it anywhere useful. The CSRF token is a readable mirror cookie that the
SPA echoes in a header, which is what makes the double-submit check meaningful.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import (
    CSRF_COOKIE,
    REFRESH_COOKIE,
    AuthServiceDep,
    ContainerDep,
    CurrentUserDep,
    RequestContextDep,
    verify_csrf,
)
from app.core.config import Settings
from app.core.errors import AuthenticationFailed
from app.core.security import hash_token
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MessageResponse,
    SessionResponse,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import IssuedSession

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(response: Response, issued: IssuedSession, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/v1/auth",
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=False,  # deliberately readable: the SPA must echo it in a header
        secure=settings.cookie_secure,
        samesite="strict",
        path=settings.cookie_path,
        domain=settings.cookie_domain,
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth", domain=settings.cookie_domain)
    response.delete_cookie(CSRF_COOKIE, path=settings.cookie_path, domain=settings.cookie_domain)


def _token_response(issued: IssuedSession) -> TokenResponse:
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        csrf_token=issued.csrf_token,
        user=UserResponse.model_validate(issued.user),
    )


@router.post("/login", summary="Sign in")
async def login(
    payload: LoginRequest,
    response: Response,
    service: AuthServiceDep,
    container: ContainerDep,
    context: RequestContextDep,
) -> TokenResponse:
    issued = await service.login(payload.username, payload.password, context)
    _set_auth_cookies(response, issued, container.settings)
    return _token_response(issued)


@router.post(
    "/refresh",
    dependencies=[Depends(verify_csrf)],
    summary="Rotate the refresh token and issue a new access token",
)
async def refresh(
    request: Request,
    response: Response,
    service: AuthServiceDep,
    container: ContainerDep,
    context: RequestContextDep,
) -> TokenResponse:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise AuthenticationFailed("No session cookie was presented")

    issued = await service.refresh(raw, context)
    _set_auth_cookies(response, issued, container.settings)
    return _token_response(issued)


@router.post(
    "/logout",
    dependencies=[Depends(verify_csrf)],
    summary="Revoke the current session family",
)
async def logout(
    request: Request,
    response: Response,
    service: AuthServiceDep,
    container: ContainerDep,
    context: RequestContextDep,
) -> MessageResponse:
    await service.logout(request.cookies.get(REFRESH_COOKIE), context)
    _clear_auth_cookies(response, container.settings)
    return MessageResponse(message="Signed out")


@router.get("/me", summary="The signed-in user")
async def me(user: CurrentUserDep) -> UserResponse:
    return UserResponse.model_validate(user)


@router.post("/change-password", summary="Change your own password")
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    user: CurrentUserDep,
    service: AuthServiceDep,
    container: ContainerDep,
    context: RequestContextDep,
) -> MessageResponse:
    await service.change_password(user, payload.current_password, payload.new_password, context)
    # Every session was revoked, including this one; the client must sign in again.
    _clear_auth_cookies(response, container.settings)
    return MessageResponse(message="Password changed. Please sign in again.")


@router.get("/sessions", summary="List your active sessions")
async def list_sessions(
    request: Request,
    user: CurrentUserDep,
    service: AuthServiceDep,
) -> list[SessionResponse]:
    raw = request.cookies.get(REFRESH_COOKIE)
    current_digest = hash_token(raw) if raw else None

    sessions = await service.list_sessions(user.id)
    return [
        SessionResponse(
            id=session.id,
            family_id=session.family_id,
            created_at=session.created_at,
            expires_at=session.expires_at,
            ip_address=session.ip_address,
            user_agent=session.user_agent,
            current=session.token_hash == current_digest,
        )
        for session in sessions
    ]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    summary="Revoke one of your sessions",
)
async def revoke_session(
    session_id: int, user: CurrentUserDep, service: AuthServiceDep
) -> MessageResponse:
    await service.revoke_session(user.id, session_id)
    return MessageResponse(message="Session revoked")
