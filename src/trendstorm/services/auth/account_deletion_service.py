"""AccountDeletionService — GDPR-compliant soft-delete and hard purge.

Lifecycle:
  schedule_deletion(user)  → sets deleted_at, purge_at = now+30d
  cancel_deletion(user)    → clears deleted_at, purge_at (within 30-day window)
  execute_purge(user)      → hard delete; called by account_purge_worker

execute_purge edge cases:
  - User is sole OWNER of an org with other ADMIN(s): transfer ownership to
    the first other admin.
  - User is sole OWNER with no other admins: mark org as orphaned (owner_user_id=None).
    The org is NOT deleted — it may have billing history, categories, jobs, etc.
  - User is a non-owner member: just delete the membership.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.memberships.models import Role
from trendstorm.shared.errors import BusinessRuleError, NotFoundError
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS

if TYPE_CHECKING:
    from trendstorm.domain.memberships.repository import MembershipRepository
    from trendstorm.domain.organizations.repository import OrganizationRepository
    from trendstorm.domain.users.models import User
    from trendstorm.domain.users.repository import UserRepository
    from trendstorm.infrastructure.auth.identity_provider import IdentityProvider
    from trendstorm.infrastructure.email.email_provider import EmailProvider
    from trendstorm.services.auth.session_service import SessionService
    from trendstorm.shared.config import EmailSettings, SignupSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


class AccountDeletionService:
    def __init__(
        self,
        *,
        user_repo: UserRepository,
        membership_repo: MembershipRepository,
        org_repo: OrganizationRepository,
        session_service: SessionService,
        identity_provider: IdentityProvider,
        email_provider: EmailProvider,
        signup_settings: SignupSettings,
        email_settings: EmailSettings,
    ) -> None:
        self._users = user_repo
        self._members = membership_repo
        self._orgs = org_repo
        self._sessions = session_service
        self._idp = identity_provider
        self._email = email_provider
        self._signup = signup_settings
        self._email_cfg = email_settings

    async def schedule_deletion(self, user: User) -> User:
        """Tombstone the account. Revokes all sessions immediately."""
        with tracer.start_as_current_span("account_deletion.schedule"):
            if not user.is_active:
                raise BusinessRuleError("Account is already scheduled for deletion.")
            now = datetime.now(UTC)
            purge_at = now + timedelta(days=self._signup.account_deletion_grace_days)
            updated = await self._users.tombstone(user.id, deleted_at=now, purge_at=purge_at)
            if updated is None:
                raise NotFoundError("User not found.")

            await self._sessions.revoke_all_for_user(user.id)

            cancel_url = f"{self._email_cfg.app_base_url}/settings/account/restore"
            try:
                await self._email.send_templated(
                    to=user.email,
                    template="deletion_scheduled",
                    variables={
                        "full_name": user.full_name or user.email,
                        "purge_date": purge_at.strftime("%B %d, %Y"),
                        "cancel_url": cancel_url,
                    },
                )
            except Exception as exc:
                logger.warning("account_deletion.email_failed", user_id=user.id, error=str(exc))

            logger.info("account_deletion.scheduled", user_id=user.id, purge_at=purge_at.isoformat())
            try:
                METRICS.account_deletions_scheduled.inc()
            except Exception:
                pass
            return updated

    async def cancel_deletion(self, user: User) -> User:
        """Cancel a pending deletion within the 30-day window."""
        if user.is_active:
            raise BusinessRuleError("Account is not scheduled for deletion.")
        restored = await self._users.cancel_tombstone(user.id)
        if restored is None:
            raise NotFoundError("User not found or already purged.")
        logger.info("account_deletion.cancelled", user_id=user.id)
        return restored

    async def execute_purge(self, user: User) -> None:
        """Hard-delete a tombstoned user. Called only by account_purge_worker.

        Steps (order matters):
        1. Handle org ownership for each org where user is sole owner.
        2. Delete all memberships.
        3. Delete user document.
        4. Delete IdP account (best-effort; failure doesn't abort).
        """
        with tracer.start_as_current_span("account_deletion.purge"):
            memberships = await self._members.list_for_user(user.id)

            for membership in memberships:
                if Role.OWNER not in membership.roles:
                    continue
                # Check if this user is the registered owner on the org doc.
                org = await self._orgs.get(membership.tenant_id)
                if org is None or org.owner_user_id != user.id:
                    continue
                # Find another admin to hand off to.
                admins = await self._members.list_admins_for_tenant(membership.tenant_id)
                successor = next(
                    (m for m in admins if m.user_id != user.id), None
                )
                if successor is not None:
                    await self._orgs.transfer_ownership(membership.tenant_id, successor.user_id)
                    logger.info(
                        "account_deletion.ownership_transferred",
                        org_id=membership.tenant_id,
                        new_owner=successor.user_id,
                    )
                else:
                    await self._orgs.mark_orphaned(membership.tenant_id)
                    logger.warning(
                        "account_deletion.org_orphaned",
                        org_id=membership.tenant_id,
                        purged_user=user.id,
                    )

            await self._members.delete_all_for_user(user.id)
            await self._users.hard_delete(user.id)

            if user.identity_provider_subject:
                try:
                    await self._idp.delete_user(user.identity_provider_subject)
                except Exception as exc:
                    logger.error(
                        "account_deletion.idp_delete_failed",
                        user_id=user.id,
                        subject=user.identity_provider_subject,
                        error=str(exc),
                    )

            logger.info("account_deletion.purged", user_id=user.id)
