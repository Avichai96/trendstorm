"""IdentityProvider Protocol and ExternalUser data class.

The IdentityProvider abstracts the external auth system (Auth0 in production,
a stub in tests). Token lifecycle (password-reset tokens, email-verification
tokens, invite tokens) is managed by our domain — we only call the IdP for
operations that require it (create user, authenticate, set password, etc.).

This separation means:
- Invite/reset/verification emails come from PostMark (our brand, our template).
- Auth0 handles password hashing and OAuth token exchange.
- Token revocation lives in Redis (fast, synchronous).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ExternalUser:
    """Identity returned by the IdP after a successful create or authenticate.

    subject: Auth0 `sub` claim (e.g. "auth0|abc123", "google-oauth2|456").
    email_verified: whether the IdP considers the email verified. For
        Database-connection signups this is False until the user clicks
        the email link (or we mark it ourselves via mark_email_verified).
    """

    subject: str
    email: str
    email_verified: bool
    name: str | None = None
    avatar_url: str | None = None


OAuthProvider = Literal["google", "github"]


class IdentityProvider(Protocol):
    """Contract for an external identity provider.

    All methods are async. Implementations map provider-specific errors to
    domain errors (AuthenticationError, ConflictError, ExternalServiceError).
    """

    async def create_user(self, email: str, password: str) -> ExternalUser:
        """Create a Database-connection account.

        Raises ConflictError if the email is already registered.
        Raises ValidationError if the password doesn't meet policy.
        """
        ...

    async def authenticate(self, email: str, password: str) -> ExternalUser:
        """Username/password login via Auth0 Resource Owner Password grant.

        Raises AuthenticationError on invalid credentials.
        Raises AuthenticationError(code="account_deleted") if the user was
        soft-deleted in Auth0 (rare; belt-and-suspenders check).
        """
        ...

    async def set_password(self, subject: str, new_password: str) -> None:
        """Hard-set a password for an existing user (Management API).

        Called by PasswordResetService.consume_reset() after our token is
        validated. We manage the token; Auth0 applies the password change.
        Raises AuthenticationError if the subject doesn't exist.
        """
        ...

    async def mark_email_verified(self, subject: str) -> None:
        """Mark the user's email as verified in the IdP (Management API).

        Called by EmailVerificationService.consume_verification() after our
        token is validated.
        """
        ...

    async def delete_user(self, subject: str) -> None:
        """Hard-delete the IdP account.

        Called by AccountDeletionService.execute_purge() as part of GDPR
        hard delete. Idempotent — raises nothing if subject not found.
        """
        ...

    async def get_oauth_authorize_url(
        self, provider: OAuthProvider, state: str, redirect_uri: str
    ) -> str:
        """Return the full /authorize URL for a social login redirect."""
        ...

    async def exchange_oauth_code(
        self,
        provider: OAuthProvider,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> ExternalUser:
        """Exchange an authorization code for user identity.

        Raises AuthenticationError if the code is invalid or expired.
        """
        ...
