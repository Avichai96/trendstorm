"""Unit tests for InvitationService.

No Docker — uses in-memory fakes for invite repo, membership repo, email provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trendstorm.domain.invites.models import Invite
from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.domain.users.models import User
from trendstorm.services.auth.token_utils import hash_token
from trendstorm.shared.errors import AuthenticationError, ConflictError, NotFoundError, TokenExpiredError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeInviteRepo:
    def __init__(self) -> None:
        self._store: dict[str, Invite] = {}

    async def insert(self, i: Invite, *, session=None) -> None:
        self._store[i.id] = i

    async def get(self, tid: str, iid: str) -> Invite | None:
        inv = self._store.get(iid)
        return inv if inv and inv.tenant_id == tid else None

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        return next((i for i in self._store.values() if i.token_hash == token_hash), None)

    async def get_pending_for_email(self, tid: str, email: str) -> Invite | None:
        return next(
            (i for i in self._store.values()
             if i.tenant_id == tid and i.email == email and i.is_pending),
            None,
        )

    async def list_pending_for_tenant(self, tid: str, *, limit: int = 50) -> list[Invite]:
        return [i for i in self._store.values() if i.tenant_id == tid and i.is_pending][:limit]

    async def accept(self, tid: str, iid: str, *, session: object = None) -> None:
        inv = self._store.get(iid)
        if inv:
            self._store[iid] = inv.model_copy(update={"accepted_at": datetime.now(UTC)})

    async def revoke(self, tid: str, iid: str) -> None:
        inv = self._store.get(iid)
        if inv:
            self._store[iid] = inv.model_copy(update={"revoked_at": datetime.now(UTC)})


class _FakeMembershipRepo:
    def __init__(self) -> None:
        self._store: dict[str, Membership] = {}

    async def insert(self, m: Membership, *, session=None) -> None:
        self._store[m.id] = m

    async def get_for_user(self, tid: str, uid: str) -> Membership | None:
        return next(
            (m for m in self._store.values() if m.tenant_id == tid and m.user_id == uid), None
        )

    async def list_for_user(self, uid: str) -> list[Membership]:
        return [m for m in self._store.values() if m.user_id == uid]

    async def get(self, tid, mid): return self._store.get(mid)
    async def list_for_tenant(self, tid): return [m for m in self._store.values() if m.tenant_id == tid]
    async def list_admins_for_tenant(self, tid): return []
    async def update_roles(self, tid, mid, roles): ...
    async def update_last_active(self, tid, uid): ...
    async def delete(self, tid, mid, *, session=None): ...
    async def delete_all_for_user(self, uid, *, session=None): ...


class _FakeTransaction:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class _FakeSession:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    def start_transaction(self): return _FakeTransaction()


class _FakeMongoInner:
    async def start_session(self): return _FakeSession()


class _FakeMongo:
    def __init__(self) -> None:
        self.client = _FakeMongoInner()


class _FakeEmail:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_templated(self, to, template, variables) -> None:
        self.sent.append({"to": to, "template": template, "vars": variables})

    async def send_raw(self, to, subject, html, text) -> None:
        self.sent.append({"to": to, "subject": subject})


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_service() -> tuple:
    from trendstorm.services.auth.invitation_service import InvitationService
    from trendstorm.shared.config import EmailSettings

    invite_repo = _FakeInviteRepo()
    membership_repo = _FakeMembershipRepo()
    email = _FakeEmail()

    svc = InvitationService(
        invite_repo=invite_repo,
        membership_repo=membership_repo,
        email_provider=email,
        email_settings=EmailSettings(app_base_url="http://localhost"),
        mongo=_FakeMongo(),
    )
    return svc, invite_repo, membership_repo, email


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def inviter() -> User:
    return User(
        email="admin@org.example.com",
        email_verified=True,
        full_name="Alice Admin",
        identity_provider_subject="auth0|alice",
    )


@pytest.fixture
def pending_invite(inviter: User) -> Invite:
    return Invite(
        tenant_id="org-1",
        email="bob@example.com",
        token_hash=hash_token("fake-token-plaintext-1"),
        roles=[Role.MEMBER],
        invited_by_user_id=inviter.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_user_sends_email_and_returns_token(inviter: User) -> None:
    svc, invite_repo, _, email = _make_service()
    invite, plaintext = await svc.invite_user(
        tenant_id="org-1",
        email="bob@example.com",
        roles=[Role.MEMBER],
        invited_by=inviter,
        org_name="Acme Corp",
    )

    assert invite.email == "bob@example.com"
    assert invite.tenant_id == "org-1"
    assert invite.token_hash == hash_token(plaintext)
    assert invite.is_pending
    # Email was dispatched.
    sent = [m for m in email.sent if m["template"] == "invite"]
    assert len(sent) == 1
    assert sent[0]["to"] == "bob@example.com"
    # Invite URL embedded in email variables.
    assert plaintext in sent[0]["vars"]["invite_url"]


@pytest.mark.asyncio
async def test_invite_user_normalises_email_case(inviter: User) -> None:
    svc, invite_repo, *_ = _make_service()
    invite, _ = await svc.invite_user(
        tenant_id="org-1",
        email="Bob@EXAMPLE.COM",
        roles=[Role.MEMBER],
        invited_by=inviter,
        org_name="Acme Corp",
    )
    assert invite.email == "bob@example.com"


@pytest.mark.asyncio
async def test_invite_user_conflict_if_pending_exists(inviter: User, pending_invite: Invite) -> None:
    svc, invite_repo, *_ = _make_service()
    invite_repo._store[pending_invite.id] = pending_invite

    with pytest.raises(ConflictError, match="pending invite"):
        await svc.invite_user(
            tenant_id="org-1",
            email="bob@example.com",
            roles=[Role.MEMBER],
            invited_by=inviter,
            org_name="Acme Corp",
        )


@pytest.mark.asyncio
async def test_preview_invite_returns_invite(inviter: User, pending_invite: Invite) -> None:
    svc, invite_repo, *_ = _make_service()
    invite_repo._store[pending_invite.id] = pending_invite

    found = await svc.preview_invite("fake-token-plaintext-1")
    assert found.id == pending_invite.id


@pytest.mark.asyncio
async def test_preview_invite_raises_on_invalid_token() -> None:
    svc, *_ = _make_service()
    with pytest.raises(NotFoundError):
        await svc.preview_invite("nonexistent-token")


@pytest.mark.asyncio
async def test_preview_invite_raises_on_expired(inviter: User) -> None:
    svc, invite_repo, *_ = _make_service()
    expired = Invite(
        tenant_id="org-1",
        email="carol@example.com",
        token_hash=hash_token("expired-token"),
        roles=[Role.MEMBER],
        invited_by_user_id=inviter.id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),  # already past
    )
    invite_repo._store[expired.id] = expired

    with pytest.raises(TokenExpiredError):
        await svc.preview_invite("expired-token")


@pytest.mark.asyncio
async def test_accept_existing_user_creates_membership(
    inviter: User, pending_invite: Invite
) -> None:
    svc, invite_repo, membership_repo, _ = _make_service()
    invite_repo._store[pending_invite.id] = pending_invite

    acceptor = User(email="bob@example.com", email_verified=True)
    membership = await svc.accept_existing_user("fake-token-plaintext-1", acceptor)

    assert membership.user_id == acceptor.id
    assert membership.tenant_id == "org-1"
    assert Role.MEMBER in membership.roles
    # Invite stamped accepted.
    stored = invite_repo._store[pending_invite.id]
    assert stored.accepted_at is not None


@pytest.mark.asyncio
async def test_accept_existing_user_raises_if_already_member(
    inviter: User, pending_invite: Invite
) -> None:
    svc, invite_repo, membership_repo, _ = _make_service()
    invite_repo._store[pending_invite.id] = pending_invite

    acceptor = User(email="bob@example.com", email_verified=True)
    existing = Membership(tenant_id="org-1", user_id=acceptor.id, roles=[Role.MEMBER], invited_by_user_id=inviter.id)
    membership_repo._store[existing.id] = existing

    with pytest.raises(ConflictError, match="already a member"):
        await svc.accept_existing_user("fake-token-plaintext-1", acceptor)


@pytest.mark.asyncio
async def test_revoke_invite(inviter: User, pending_invite: Invite) -> None:
    svc, invite_repo, *_ = _make_service()
    invite_repo._store[pending_invite.id] = pending_invite

    await svc.revoke_invite("org-1", pending_invite.id)
    assert invite_repo._store[pending_invite.id].revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_invite_raises_on_not_found() -> None:
    svc, *_ = _make_service()
    with pytest.raises(NotFoundError):
        await svc.revoke_invite("org-1", "nonexistent-id")


@pytest.mark.asyncio
async def test_resend_revokes_old_and_creates_new(inviter: User, pending_invite: Invite) -> None:
    svc, invite_repo, *_ = _make_service()
    invite_repo._store[pending_invite.id] = pending_invite

    new_invite, new_token = await svc.resend_invite(
        "org-1", pending_invite.id, inviter=inviter, org_name="Acme Corp"
    )

    # Old invite revoked.
    assert invite_repo._store[pending_invite.id].revoked_at is not None
    # New invite has a different id and token.
    assert new_invite.id != pending_invite.id
    assert new_invite.email == pending_invite.email
    assert new_invite.token_hash == hash_token(new_token)


@pytest.mark.asyncio
async def test_preview_invite_raises_if_revoked(inviter: User, pending_invite: Invite) -> None:
    svc, invite_repo, *_ = _make_service()
    revoked = pending_invite.model_copy(update={"revoked_at": datetime.now(UTC)})
    invite_repo._store[revoked.id] = revoked

    with pytest.raises(AuthenticationError, match="already been used or revoked"):
        await svc.preview_invite("fake-token-plaintext-1")
