"""RegistrationService — create new accounts and accept invites.

Two entry points:
1. create_account(email, password, *, invite_token=None, ip=None)
   - Enforces SIGNUP_MODE policy (invite_only, open, closed).
   - Creates the IdP account OUTSIDE the Mongo transaction.
   - Inside the Mongo transaction: User + (Organization + Membership(OWNER))
     OR accept_invite + Membership.
   - Compensating delete of the IdP account on Mongo transaction rollback.
   - Fires welcome email + audit log.

2. create_account_from_oauth(external_user, *, invite_token=None, ip=None)
   - Called by the OAuth callback after exchange_oauth_code().
   - Same Mongo transaction pattern; IdP account already exists.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.domain.organizations.models import Organization, SignupMode
from trendstorm.domain.users.models import User
from trendstorm.infrastructure.auth.identity_provider import ExternalUser
from trendstorm.services.auth.token_utils import hash_token
from trendstorm.shared.errors import NotFoundError, SignupNotAllowedError
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS

if TYPE_CHECKING:
    from trendstorm.domain.invites.models import Invite
    from trendstorm.domain.invites.repository import InviteRepository
    from trendstorm.domain.memberships.repository import MembershipRepository
    from trendstorm.domain.organizations.repository import OrganizationRepository
    from trendstorm.domain.users.repository import UserRepository
    from trendstorm.infrastructure.auth.identity_provider import IdentityProvider
    from trendstorm.infrastructure.email.email_provider import EmailProvider
    from trendstorm.infrastructure.mongo.client import MongoClient
    from trendstorm.shared.config import EmailSettings, SignupSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9-]")


def _slugify(name: str) -> str:
    slug = name.lower().strip().replace(" ", "-")
    slug = _SLUG_RE.sub("", slug)
    return slug[:48] or "org"


class RegistrationService:
    def __init__(
        self,
        *,
        user_repo: UserRepository,
        org_repo: OrganizationRepository,
        membership_repo: MembershipRepository,
        invite_repo: InviteRepository,
        identity_provider: IdentityProvider,
        email_provider: EmailProvider,
        mongo: MongoClient,
        signup_settings: SignupSettings,
        email_settings: EmailSettings,
    ) -> None:
        self._users = user_repo
        self._orgs = org_repo
        self._members = membership_repo
        self._invites = invite_repo
        self._idp = identity_provider
        self._email = email_provider
        self._mongo = mongo
        self._signup = signup_settings
        self._email_cfg = email_settings

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def create_account(
        self,
        email: str,
        password: str,
        *,
        invite_token: str | None = None,
        ip: str | None = None,
    ) -> tuple[User, Organization]:
        """Create a password-based account. Returns (user, active_org)."""
        with tracer.start_as_current_span("registration.create_account"):
            email = email.lower().strip()
            invite = await self._resolve_invite(email, invite_token)
            self._check_signup_allowed(email, invite is not None)

            external = await self._idp.create_user(email, password)
            result = await self._finish_account(external, invite, ip=ip)
            try:
                METRICS.signups.labels(
                    method="password",
                    mode=self._signup.signup_mode,
                ).inc()
            except Exception:
                pass
            return result

    async def create_account_from_oauth(
        self,
        external: ExternalUser,
        *,
        invite_token: str | None = None,
        ip: str | None = None,
    ) -> tuple[User, Organization]:
        """Register a user from an OAuth callback — the IdP account already exists."""
        email = external.email.lower().strip()
        invite = await self._resolve_invite(email, invite_token)
        self._check_signup_allowed(email, invite is not None)
        result = await self._finish_account(external, invite, ip=ip)
        try:
            METRICS.signups.labels(
                method="oauth",
                mode=self._signup.signup_mode,
            ).inc()
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _check_signup_allowed(self, email: str, has_invite: bool) -> None:
        mode = SignupMode(self._signup.signup_mode)
        if mode == SignupMode.CLOSED:
            raise SignupNotAllowedError("Signups are currently closed.")
        if mode == SignupMode.INVITE_ONLY and not has_invite:
            domain = email.split("@")[-1] if "@" in email else ""
            if domain not in self._signup.allowlist_domains:
                raise SignupNotAllowedError(
                    "An invitation is required to sign up.",
                    context={"email": email},
                )

    async def _resolve_invite(self, email: str, token: str | None) -> Invite | None:
        if token is None:
            return None
        token_hash = hash_token(token)
        invite = await self._invites.get_by_token_hash(token_hash)
        if invite is None:
            raise NotFoundError("Invite not found or expired.", code="invalid_invite_token")
        if not invite.is_pending:
            raise NotFoundError("Invite is no longer valid.", code="invite_not_pending")
        if invite.email != email:
            raise SignupNotAllowedError(
                "This invite was sent to a different email address.",
                code="invite_email_mismatch",
            )
        return invite

    async def _finish_account(
        self, external: ExternalUser, invite: Invite | None, *, ip: str | None
    ) -> tuple[User, Organization]:
        """Create User + Org/Membership inside a Mongo transaction.

        If the transaction fails, a compensating delete of the IdP account
        is attempted (best-effort — IdP failures here are logged, not raised).
        """
        user = User(
            email=external.email.lower(),
            email_verified=external.email_verified,
            identity_provider_subject=external.subject,
            full_name=external.name,
            avatar_url=external.avatar_url,
        )

        if invite is not None:
            # User is joining an existing org via invite.
            org = await self._orgs.get(invite.tenant_id)
            if org is None:
                raise NotFoundError("Organization not found.", code="org_not_found")
            membership = Membership(
                tenant_id=invite.tenant_id,
                user_id=user.id,
                roles=list(invite.roles),
                invited_by_user_id=invite.invited_by_user_id,
            )
        else:
            # User is creating a new org.
            slug = _slugify(external.name or external.email.split("@")[0])
            org = Organization(
                name=external.name or external.email.split("@")[0],
                slug=slug,
                owner_user_id=user.id,
                billing_email=external.email,
            )
            membership = Membership(
                tenant_id=org.id,
                user_id=user.id,
                roles=[Role.OWNER],
            )

        try:
            async with await self._mongo.client.start_session() as session, session.start_transaction():
                    await self._users.insert(user, session=session)
                    if invite is None:
                        await self._orgs.insert(org, session=session)
                    await self._members.insert(membership, session=session)
                    if invite is not None:
                        await self._invites.accept(invite.tenant_id, invite.id, session=session)
        except Exception:
            logger.warning(
                "registration.transaction_failed.compensating_idp_delete",
                subject=external.subject,
            )
            try:
                await self._idp.delete_user(external.subject)
            except Exception as del_exc:
                logger.error(
                    "registration.idp_compensate_failed",
                    subject=external.subject,
                    error=str(del_exc),
                )
            raise

        # Post-transaction side-effects (best-effort).
        await self._send_welcome(user, org)
        logger.info(
            "registration.account_created",
            user_id=user.id,
            org_id=org.id,
            via_invite=invite is not None,
        )
        return user, org

    async def _send_welcome(self, user: User, org: Organization) -> None:
        try:
            await self._email.send_templated(
                to=user.email,
                template="welcome",
                variables={
                    "full_name": user.full_name or user.email,
                    "org_name": org.name,
                    "dashboard_url": self._email_cfg.app_base_url,
                },
            )
        except Exception as exc:
            logger.warning("registration.welcome_email_failed", error=str(exc))
