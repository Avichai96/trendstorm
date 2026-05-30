"""Unit tests for AccountDeletionService.

No Docker — uses in-memory fakes for all dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.domain.organizations.models import Organization
from trendstorm.domain.users.models import User
from trendstorm.shared.errors import BusinessRuleError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeUserRepo:
    def __init__(self, user: User) -> None:
        self._store: dict[str, User] = {user.id: user}

    async def get(self, uid: str) -> User | None:
        return self._store.get(uid)

    async def tombstone(self, uid: str, *, deleted_at, purge_at) -> User | None:
        u = self._store.get(uid)
        if u:
            self._store[uid] = u.model_copy(update={"deleted_at": deleted_at, "purge_at": purge_at})
        return self._store.get(uid)

    async def cancel_tombstone(self, uid: str) -> User | None:
        u = self._store.get(uid)
        if u:
            self._store[uid] = u.model_copy(update={"deleted_at": None, "purge_at": None})
        return self._store.get(uid)

    async def hard_delete(self, uid: str) -> None:
        self._store.pop(uid, None)

    async def get_by_email(self, email): ...
    async def get_by_subject(self, subject): ...
    async def insert(self, user, *, session=None): self._store[user.id] = user
    async def update(self, user): self._store[user.id] = user
    async def list_due_for_purge(self, *, limit=50): return []
    async def set_email_verified(self, uid): ...


class _FakeMembershipRepo:
    def __init__(self) -> None:
        self._store: dict[str, Membership] = {}

    async def list_for_user(self, uid: str) -> list[Membership]:
        return [m for m in self._store.values() if m.user_id == uid]

    async def list_admins_for_tenant(self, tid: str) -> list[Membership]:
        return [
            m for m in self._store.values()
            if m.tenant_id == tid and (Role.ADMIN in m.roles or Role.OWNER in m.roles)
        ]

    async def delete_all_for_user(self, uid: str, *, session=None) -> None:
        ids = [mid for mid, m in self._store.items() if m.user_id == uid]
        for mid in ids:
            del self._store[mid]

    async def insert(self, m: Membership, *, session=None) -> None:
        self._store[m.id] = m

    async def get(self, tid, mid): return self._store.get(mid)
    async def get_for_user(self, tid, uid): ...
    async def list_for_tenant(self, tid): return [m for m in self._store.values() if m.tenant_id == tid]
    async def list_for_user(self, uid): return [m for m in self._store.values() if m.user_id == uid]
    async def update_roles(self, tid, mid, roles): ...
    async def update_last_active(self, tid, uid): ...
    async def delete(self, tid, mid, *, session=None): ...


class _FakeOrgRepo:
    def __init__(self) -> None:
        self._store: dict[str, Organization] = {}
        self.transferred: list[tuple[str, str]] = []  # (org_id, new_owner_id)
        self.orphaned: list[str] = []

    async def get(self, org_id: str) -> Organization | None:
        return self._store.get(org_id)

    async def transfer_ownership(self, org_id: str, new_owner_user_id: str) -> None:
        self.transferred.append((org_id, new_owner_user_id))
        org = self._store.get(org_id)
        if org:
            self._store[org_id] = org.model_copy(update={"owner_user_id": new_owner_user_id})

    async def mark_orphaned(self, org_id: str) -> None:
        self.orphaned.append(org_id)

    async def insert(self, org, *, session=None): self._store[org.id] = org
    async def get_by_slug(self, slug): ...
    async def get_by_name(self, name): ...
    async def update(self, org): self._store[org.id] = org
    async def list_for_user(self, uid): return []


class _FakeIdP:
    def __init__(self) -> None:
        self.deleted_subjects: list[str] = []

    async def delete_user(self, subject: str) -> None:
        self.deleted_subjects.append(subject)

    async def create_user(self, email, password): ...
    async def authenticate(self, email, password): ...
    async def set_password(self, subject, new_password): ...
    async def mark_email_verified(self, subject): ...
    async def get_oauth_authorize_url(self, provider, state, redirect_uri): return ""
    async def exchange_oauth_code(self, provider, code, state, redirect_uri): ...


class _FakeEmail:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_templated(self, to, template, variables) -> None:
        self.sent.append({"to": to, "template": template})

    async def send_raw(self, to, subject, html, text) -> None:
        self.sent.append({"to": to})


class _FakeSessionSvc:
    def __init__(self) -> None:
        self.revoked_users: list[str] = []

    async def revoke_all_for_user(self, user_id: str) -> None:
        self.revoked_users.append(user_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_service(user: User) -> tuple:
    from trendstorm.services.auth.account_deletion_service import AccountDeletionService
    from trendstorm.shared.config import EmailSettings, SignupSettings

    user_repo = _FakeUserRepo(user)
    membership_repo = _FakeMembershipRepo()
    org_repo = _FakeOrgRepo()
    session_svc = _FakeSessionSvc()
    idp = _FakeIdP()
    email = _FakeEmail()

    svc = AccountDeletionService(
        user_repo=user_repo,
        membership_repo=membership_repo,
        org_repo=org_repo,
        session_service=session_svc,  # type: ignore[arg-type]
        identity_provider=idp,
        email_provider=email,
        signup_settings=SignupSettings(signup_mode="open"),
        email_settings=EmailSettings(app_base_url="http://localhost"),
    )
    return svc, user_repo, membership_repo, org_repo, session_svc, idp, email


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def active_user() -> User:
    return User(
        email="alice@example.com",
        email_verified=True,
        identity_provider_subject="auth0|alice",
    )


@pytest.fixture
def tombstoned_user() -> User:
    now = datetime.now(UTC)
    from datetime import timedelta
    return User(
        email="bob@example.com",
        email_verified=True,
        identity_provider_subject="auth0|bob",
        deleted_at=now,
        purge_at=now + timedelta(days=30),
    )


# ---------------------------------------------------------------------------
# Tests — schedule_deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_deletion_sets_tombstone(active_user: User) -> None:
    svc, user_repo, _, _, session_svc, _, email = _make_service(active_user)
    updated = await svc.schedule_deletion(active_user)

    assert updated.deleted_at is not None
    assert updated.purge_at is not None
    assert not updated.is_active
    # Sessions revoked immediately.
    assert active_user.id in session_svc.revoked_users
    # Deletion confirmation email sent.
    sent = [m for m in email.sent if m["template"] == "deletion_scheduled"]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_schedule_deletion_raises_if_already_tombstoned(tombstoned_user: User) -> None:
    svc, *_ = _make_service(tombstoned_user)
    with pytest.raises(BusinessRuleError):
        await svc.schedule_deletion(tombstoned_user)


# ---------------------------------------------------------------------------
# Tests — cancel_deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_deletion_restores_account(tombstoned_user: User) -> None:
    svc, user_repo, *_ = _make_service(tombstoned_user)
    restored = await svc.cancel_deletion(tombstoned_user)

    assert restored.deleted_at is None
    assert restored.purge_at is None
    assert restored.is_active


@pytest.mark.asyncio
async def test_cancel_deletion_raises_if_account_is_active(active_user: User) -> None:
    svc, *_ = _make_service(active_user)
    with pytest.raises(BusinessRuleError):
        await svc.cancel_deletion(active_user)


# ---------------------------------------------------------------------------
# Tests — execute_purge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_purge_transfers_ownership_to_admin(tombstoned_user: User) -> None:
    svc, user_repo, membership_repo, org_repo, _, idp, _ = _make_service(tombstoned_user)

    org = Organization(
        id="org-1",
        name="Acme",
        slug="acme",
        owner_user_id=tombstoned_user.id,
        billing_email=tombstoned_user.email,
    )
    org_repo._store[org.id] = org

    # Tombstoned user is the OWNER.
    owner_membership = Membership(
        tenant_id="org-1",
        user_id=tombstoned_user.id,
        roles=[Role.OWNER],
        invited_by_user_id=tombstoned_user.id,
    )
    # Another admin in the org.
    admin_user = User(email="admin@example.com")
    admin_membership = Membership(
        tenant_id="org-1",
        user_id=admin_user.id,
        roles=[Role.ADMIN],
        invited_by_user_id=tombstoned_user.id,
    )
    membership_repo._store[owner_membership.id] = owner_membership
    membership_repo._store[admin_membership.id] = admin_membership

    await svc.execute_purge(tombstoned_user)

    # Ownership transferred to the admin.
    assert len(org_repo.transferred) == 1
    assert org_repo.transferred[0] == ("org-1", admin_user.id)
    # Tombstoned user's memberships deleted.
    remaining = await membership_repo.list_for_user(tombstoned_user.id)
    assert len(remaining) == 0
    # User hard-deleted.
    assert user_repo._store.get(tombstoned_user.id) is None
    # IdP account deleted.
    assert tombstoned_user.identity_provider_subject in idp.deleted_subjects


@pytest.mark.asyncio
async def test_execute_purge_orphans_org_when_no_other_admins(tombstoned_user: User) -> None:
    svc, user_repo, membership_repo, org_repo, *_ = _make_service(tombstoned_user)

    org = Organization(
        id="org-2",
        name="Solo Corp",
        slug="solo-corp",
        owner_user_id=tombstoned_user.id,
        billing_email=tombstoned_user.email,
    )
    org_repo._store[org.id] = org

    owner_membership = Membership(
        tenant_id="org-2",
        user_id=tombstoned_user.id,
        roles=[Role.OWNER],
        invited_by_user_id=tombstoned_user.id,
    )
    membership_repo._store[owner_membership.id] = owner_membership

    await svc.execute_purge(tombstoned_user)

    # No transfer; org marked orphaned.
    assert len(org_repo.transferred) == 0
    assert "org-2" in org_repo.orphaned
    assert user_repo._store.get(tombstoned_user.id) is None


@pytest.mark.asyncio
async def test_execute_purge_non_owner_just_deletes_membership(tombstoned_user: User) -> None:
    svc, user_repo, membership_repo, org_repo, *_ = _make_service(tombstoned_user)

    member_membership = Membership(
        tenant_id="org-3",
        user_id=tombstoned_user.id,
        roles=[Role.MEMBER],
        invited_by_user_id="some-other-user",
    )
    membership_repo._store[member_membership.id] = member_membership

    await svc.execute_purge(tombstoned_user)

    # No ownership transfer or orphan.
    assert len(org_repo.transferred) == 0
    assert len(org_repo.orphaned) == 0
    # Membership deleted.
    remaining = await membership_repo.list_for_user(tombstoned_user.id)
    assert len(remaining) == 0
    # User hard-deleted.
    assert user_repo._store.get(tombstoned_user.id) is None


@pytest.mark.asyncio
async def test_execute_purge_idp_failure_is_non_fatal(tombstoned_user: User) -> None:
    """IdP delete failure must not abort the purge — user doc already deleted."""
    svc, user_repo, membership_repo, org_repo, _, idp, _ = _make_service(tombstoned_user)

    async def _fail(subject: str) -> None:
        raise RuntimeError("Auth0 is down")

    idp.delete_user = _fail  # type: ignore[method-assign]

    # No membership, no org — just a bare purge.
    await svc.execute_purge(tombstoned_user)

    # User still hard-deleted.
    assert user_repo._store.get(tombstoned_user.id) is None


@pytest.mark.asyncio
async def test_execute_purge_user_without_idp_subject(tombstoned_user: User) -> None:
    """Purge succeeds even if no IdP subject was ever set."""
    user_no_idp = tombstoned_user.model_copy(update={"identity_provider_subject": None})
    svc, user_repo, *_, idp, _ = _make_service(user_no_idp)

    await svc.execute_purge(user_no_idp)

    assert user_repo._store.get(user_no_idp.id) is None
    assert len(idp.deleted_subjects) == 0
