"""InvitationService — invite users to an organization.

Token lifecycle: 32-byte random token → URL-safe base64 → SHA-256 hash stored
in Mongo. Plaintext sent in the invite email link. Single-use.

Invite acceptance:
  - Accepts the invite (stamps accepted_at).
  - Creates the Membership for the accepting user.
  - Called from RegistrationService._finish_account() for new users OR
    from accept_existing_user() for users who already have an account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.invites.models import Invite
from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.services.auth.token_utils import generate_token, hash_token
from trendstorm.shared.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    TokenExpiredError,
)
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS

if TYPE_CHECKING:
    from trendstorm.domain.invites.repository import InviteRepository
    from trendstorm.domain.memberships.repository import MembershipRepository
    from trendstorm.domain.users.models import User
    from trendstorm.infrastructure.email.email_provider import EmailProvider
    from trendstorm.infrastructure.mongo.client import MongoClient
    from trendstorm.shared.config import EmailSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_EXPIRY_DAYS = 7


class InvitationService:
    def __init__(
        self,
        *,
        invite_repo: InviteRepository,
        membership_repo: MembershipRepository,
        email_provider: EmailProvider,
        email_settings: EmailSettings,
        mongo: MongoClient,
    ) -> None:
        self._invites = invite_repo
        self._members = membership_repo
        self._email = email_provider
        self._email_cfg = email_settings
        self._mongo = mongo

    async def invite_user(
        self,
        *,
        tenant_id: str,
        email: str,
        roles: list[Role],
        invited_by: User,
        org_name: str,
    ) -> tuple[Invite, str]:
        """Create an invite. Returns (Invite, plaintext_token).

        Raises ConflictError if there's already a pending invite for this email.
        """
        email = email.lower().strip()
        existing = await self._invites.get_pending_for_email(tenant_id, email)
        if existing is not None:
            raise ConflictError(
                f"A pending invite already exists for {email}",
                code="invite_exists",
            )

        plaintext = generate_token()
        token_hash = hash_token(plaintext)
        now = datetime.now(UTC)
        invite = Invite(
            tenant_id=tenant_id,
            email=email,
            token_hash=token_hash,
            roles=roles,
            invited_by_user_id=invited_by.id,
            expires_at=now + timedelta(days=_EXPIRY_DAYS),
        )
        await self._invites.insert(invite)
        await self._send_invite_email(invite, plaintext, inviter=invited_by, org_name=org_name)
        logger.info("invitation.sent", tenant_id=tenant_id, email=email, invite_id=invite.id)
        try:
            METRICS.invites_sent.inc()
        except Exception:
            pass
        return invite, plaintext

    async def preview_invite(self, token: str) -> Invite:
        """Return invite info for the accept page (no side-effects)."""
        token_hash = hash_token(token)
        invite = await self._invites.get_by_token_hash(token_hash)
        if invite is None:
            raise NotFoundError("Invite not found.", code="invalid_invite_token")
        if invite.is_expired:
            raise TokenExpiredError("This invitation has expired.")
        if not invite.is_pending:
            raise AuthenticationError("This invitation has already been used or revoked.", code="invite_not_pending")
        return invite

    async def accept_existing_user(self, token: str, user: User) -> Membership:
        """Accept an invite for a user who already has an account.

        New users go through RegistrationService which handles the Mongo
        transaction that creates User + accepts invite atomically.
        """
        invite = await self.preview_invite(token)
        existing = await self._members.get_for_user(invite.tenant_id, user.id)
        if existing is not None:
            raise ConflictError(
                "You are already a member of this organization.",
                code="already_member",
            )
        membership = Membership(
            tenant_id=invite.tenant_id,
            user_id=user.id,
            roles=list(invite.roles),
            invited_by_user_id=invite.invited_by_user_id,
        )
        async with await self._mongo.client.start_session() as session, session.start_transaction():
            await self._members.insert(membership, session=session)
            await self._invites.accept(invite.tenant_id, invite.id, session=session)
        logger.info("invitation.accepted", invite_id=invite.id, user_id=user.id)
        try:
            METRICS.invites_accepted.inc()
        except Exception:
            pass
        return membership

    async def revoke_invite(self, tenant_id: str, invite_id: str) -> None:
        invite = await self._invites.get(tenant_id, invite_id)
        if invite is None:
            raise NotFoundError("Invite not found.")
        if not invite.is_pending:
            raise ConflictError("Invite is not in pending state.")
        await self._invites.revoke(tenant_id, invite_id)
        logger.info("invitation.revoked", invite_id=invite_id)

    async def resend_invite(
        self, tenant_id: str, invite_id: str, *, inviter: User, org_name: str
    ) -> tuple[Invite, str]:
        """Revoke the old invite and create a fresh one (resets expiry)."""
        old = await self._invites.get(tenant_id, invite_id)
        if old is None:
            raise NotFoundError("Invite not found.")
        if not old.is_pending:
            raise ConflictError("Only pending invites can be resent.")
        await self._invites.revoke(tenant_id, invite_id)
        return await self.invite_user(
            tenant_id=tenant_id,
            email=old.email,
            roles=old.roles,
            invited_by=inviter,
            org_name=org_name,
        )

    async def list_pending(self, tenant_id: str, *, limit: int = 50) -> list[Invite]:
        return await self._invites.list_pending_for_tenant(tenant_id, limit=limit)

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    async def _send_invite_email(
        self, invite: Invite, plaintext: str, *, inviter: User, org_name: str
    ) -> None:
        invite_url = (
            f"{self._email_cfg.app_base_url}/auth/invite/{plaintext}"
        )
        try:
            await self._email.send_templated(
                to=invite.email,
                template="invite",
                variables={
                    "org_name": org_name,
                    "inviter_name": inviter.full_name or inviter.email,
                    "role": ", ".join(r.value for r in invite.roles),
                    "invite_url": invite_url,
                },
            )
        except Exception as exc:
            logger.warning("invitation.email_failed", invite_id=invite.id, error=str(exc))
