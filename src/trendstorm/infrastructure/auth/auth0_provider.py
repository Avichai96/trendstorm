"""Auth0 implementation of IdentityProvider.

Uses two Auth0 APIs:
- Authentication API  (/oauth/token, /dbconnections/signup) — for end-user flows.
- Management API v2   (/api/v2/users/*)  — for admin operations (set password,
  mark email verified, delete user).

Management API tokens are client-credentials grants cached for their TTL.
All Auth0 SDK errors are mapped to domain errors before leaving this module.

Configuration (from Auth0Settings):
    domain                  — e.g. "my-tenant.us.auth0.com"
    client_id               — Regular Web Application
    client_secret           — Regular Web Application
    audience                — API identifier (e.g. "https://api.trendstorm.ai")
    management_client_id    — M2M app with read:users, update:users, delete:users,
                              create:users scopes
    management_client_secret
    database_connection     — "Username-Password-Authentication"
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from trendstorm.infrastructure.auth.identity_provider import ExternalUser, OAuthProvider
from trendstorm.shared.errors import (
    AuthenticationError,
    ConflictError,
    ExternalServiceError,
    ValidationError,
)
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.shared.config import Auth0Settings

logger = get_logger(__name__)

_MGMT_TOKEN_BUFFER_SECONDS = 60  # re-fetch if within 60s of expiry


class Auth0Provider:
    """Auth0-backed IdentityProvider."""

    def __init__(self, settings: Auth0Settings) -> None:
        self._s = settings
        self._base = f"https://{settings.domain}"
        self._mgmt_token: str | None = None
        self._mgmt_token_expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Management API token (cached)                                        #
    # ------------------------------------------------------------------ #

    async def _get_mgmt_token(self) -> str:
        if self._mgmt_token and time.monotonic() < self._mgmt_token_expires_at:
            return self._mgmt_token
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/oauth/token",
                json={
                    "grant_type": "client_credentials",
                    "client_id": self._s.management_client_id,
                    "client_secret": self._s.management_client_secret.get_secret_value(),
                    "audience": f"{self._base}/api/v2/",
                },
            )
            self._raise_for_status(resp, "management token fetch")
            data = resp.json()
        self._mgmt_token = data["access_token"]
        self._mgmt_token_expires_at = time.monotonic() + data.get("expires_in", 86400) - _MGMT_TOKEN_BUFFER_SECONDS
        return self._mgmt_token

    # ------------------------------------------------------------------ #
    # IdentityProvider interface                                           #
    # ------------------------------------------------------------------ #

    async def create_user(self, email: str, password: str) -> ExternalUser:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/dbconnections/signup",
                json={
                    "client_id": self._s.client_id,
                    "email": email,
                    "password": password,
                    "connection": self._s.database_connection,
                },
            )
        if resp.status_code == 400:
            body = resp.json()
            code = body.get("code", "")
            if code == "user_exists":
                raise ConflictError(
                    f"Account already exists for {email}", code="user_exists"
                )
            raise ValidationError(body.get("description", "Signup failed"), code="signup_failed")
        self._raise_for_status(resp, "create_user")
        data = resp.json()
        return ExternalUser(
            subject=f"auth0|{data['_id']}",
            email=data["email"],
            email_verified=data.get("email_verified", False),
        )

    async def authenticate(self, email: str, password: str) -> ExternalUser:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/oauth/token",
                json={
                    "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
                    "client_id": self._s.client_id,
                    "client_secret": self._s.client_secret.get_secret_value(),
                    "username": email,
                    "password": password,
                    "realm": self._s.database_connection,
                    "audience": self._s.audience,
                    "scope": "openid profile email",
                },
            )
        if resp.status_code in (400, 401, 403):
            body = resp.json()
            error = body.get("error", "")
            if error in ("invalid_grant", "unauthorized"):
                raise AuthenticationError("Invalid email or password", code="invalid_credentials")
            raise AuthenticationError(body.get("error_description", "Login failed"))
        self._raise_for_status(resp, "authenticate")
        data = resp.json()
        # Decode the id_token to extract user info (minimal decode, no verify needed
        # here — Auth0 already validated the credentials; we just need the claims).
        import base64
        import json as _json

        def _decode_jwt_payload(token: str) -> dict[str, Any]:
            parts = token.split(".")
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            return _json.loads(base64.urlsafe_b64decode(padded))  # type: ignore[no-any-return]

        claims = _decode_jwt_payload(data["id_token"])
        return ExternalUser(
            subject=claims["sub"],
            email=claims.get("email", email),
            email_verified=claims.get("email_verified", False),
            name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )

    async def set_password(self, subject: str, new_password: str) -> None:
        token = await self._get_mgmt_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{self._base}/api/v2/users/{subject}",
                headers={"Authorization": f"Bearer {token}"},
                json={"password": new_password, "connection": self._s.database_connection},
            )
        if resp.status_code == 404:
            raise AuthenticationError(f"User {subject} not found in IdP", code="user_not_found")
        self._raise_for_status(resp, "set_password")

    async def mark_email_verified(self, subject: str) -> None:
        token = await self._get_mgmt_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{self._base}/api/v2/users/{subject}",
                headers={"Authorization": f"Bearer {token}"},
                json={"email_verified": True},
            )
        if resp.status_code == 404:
            logger.warning("auth0.mark_email_verified.not_found", subject=subject)
            return
        self._raise_for_status(resp, "mark_email_verified")

    async def delete_user(self, subject: str) -> None:
        token = await self._get_mgmt_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{self._base}/api/v2/users/{subject}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 404:
            logger.info("auth0.delete_user.already_gone", subject=subject)
            return
        self._raise_for_status(resp, "delete_user")

    async def get_oauth_authorize_url(
        self, provider: OAuthProvider, state: str, redirect_uri: str
    ) -> str:
        connection_map = {"google": "google-oauth2", "github": "github"}
        connection = connection_map[provider]
        from urllib.parse import urlencode
        params = urlencode({
            "response_type": "code",
            "client_id": self._s.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "connection": connection,
        })
        return f"{self._base}/authorize?{params}"

    async def exchange_oauth_code(
        self,
        provider: OAuthProvider,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> ExternalUser:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base}/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": self._s.client_id,
                    "client_secret": self._s.client_secret.get_secret_value(),
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
        if resp.status_code in (400, 401):
            body = resp.json()
            raise AuthenticationError(
                body.get("error_description", "OAuth code exchange failed"),
                code="oauth_exchange_failed",
            )
        self._raise_for_status(resp, "exchange_oauth_code")
        data = resp.json()
        import base64
        import json as _json

        def _decode_jwt_payload(token: str) -> dict[str, Any]:
            parts = token.split(".")
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            return _json.loads(base64.urlsafe_b64decode(padded))  # type: ignore[no-any-return]

        claims = _decode_jwt_payload(data["id_token"])
        return ExternalUser(
            subject=claims["sub"],
            email=claims.get("email", ""),
            email_verified=claims.get("email_verified", False),
            name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _raise_for_status(resp: httpx.Response, operation: str) -> None:
        if resp.is_success:
            return
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise ExternalServiceError(
            f"Auth0 {operation} failed with HTTP {resp.status_code}",
            code="auth0_error",
            context={"status": resp.status_code, "detail": detail},
        )
