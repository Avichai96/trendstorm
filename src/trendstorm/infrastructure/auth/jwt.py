"""JWT validation with JWKS (JSON Web Key Set) support.

Validates JWTs issued by Auth0 (production) or a local issuer (dev/CI).
JWKS is fetched once at startup and cached for the process lifetime; a
background refresh is deferred to Phase 13+ (key rotation is rare and the
process restarts on deploys anyway).

Tenant extraction:
  The JWT `sub` claim is the user identity. Tenant membership is derived
  from a custom claim: `https://trendstorm.ai/tenant_id`. This claim is
  set by an Auth0 Action (Rule) that looks up the user's tenant on login.
  For the local test issuer (dev/CI), we generate tokens with this claim
  directly — see `scripts/generate_test_token.py`.

Multiple IdP support:
  `JWTValidator` accepts a list of `(issuer_url, audience)` pairs.
  A token is valid if it passes verification against ANY configured IdP.
  This allows migration from one IdP to another without a hard cutover.
"""

from __future__ import annotations

import httpx
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

from trendstorm.shared.errors import TrendStormError
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_TENANT_CLAIM = "https://trendstorm.ai/tenant_id"


class JWTAuthError(TrendStormError):
    """Raised when JWT validation fails (expired, bad sig, missing claims)."""


class IdPConfig:
    """Configuration for one identity provider."""

    def __init__(self, issuer_url: str, audience: str) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.audience = audience
        self._jwks: dict[str, object] | None = None

    async def _fetch_jwks(self) -> dict[str, object]:
        """Download the JWKS from the /.well-known endpoint."""
        url = f"{self.issuer_url}/.well-known/jwks.json"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]  # httpx.Response.json() returns Any

    async def jwks(self) -> dict[str, object]:
        if self._jwks is None:
            self._jwks = await self._fetch_jwks()
        return self._jwks


class JWTValidator:
    """Validates JWTs against one or more configured IdPs.

    Args:
        idps:       list of IdPConfig — token valid if any IdP accepts it.
        algorithms: accepted signing algorithms (default RS256).

    """

    def __init__(
        self,
        idps: list[IdPConfig],
        *,
        algorithms: list[str] | None = None,
    ) -> None:
        self._idps = idps
        self._algorithms = algorithms or ["RS256"]

    async def validate(self, token: str) -> dict[str, object]:
        """Validate a raw Bearer token. Returns the decoded payload.

        Raises `JWTAuthError` if the token is invalid, expired, or no IdP
        accepts it.
        """
        last_error: Exception | None = None

        for idp in self._idps:
            try:
                jwks = await idp.jwks()
                payload = jwt.decode(
                    token,
                    jwks,
                    algorithms=self._algorithms,
                    audience=idp.audience,
                    issuer=idp.issuer_url,
                    options={"verify_at_hash": False},
                )
                return payload  # type: ignore[no-any-return]  # jose.jwt.decode returns Any; we trust the type at runtime
            except ExpiredSignatureError as e:
                raise JWTAuthError(
                    "Token has expired",
                    code="token_expired",
                ) from e
            except JWTError as e:
                last_error = e
                logger.debug(
                    "jwt.idp_validation_failed",
                    issuer=idp.issuer_url,
                    error=str(e),
                )
                continue

        raise JWTAuthError(
            "Token validation failed",
            code="token_invalid",
            context={"detail": str(last_error) if last_error else "no IdPs configured"},
        )

    def extract_tenant_id(self, payload: dict[str, object]) -> str:
        """Extract tenant_id from the decoded JWT payload.

        Raises `JWTAuthError` if the custom claim is missing — the Auth0
        Action / token generator is responsible for setting it.
        """
        tenant_id = payload.get(_TENANT_CLAIM)
        if not tenant_id:
            raise JWTAuthError(
                f"JWT missing required claim: {_TENANT_CLAIM}",
                code="missing_tenant_claim",
            )
        return str(tenant_id)
