"""Unit tests for RegistrationService.

Uses mocked IdentityProvider, EmailProvider, and in-memory repositories
(backed by dicts). No Docker required.
"""

from __future__ import annotations

import pytest

from trendstorm.domain.invites.models import Invite
from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.domain.organizations.models import Organization
from trendstorm.domain.users.models import User
from trendstorm.infrastructure.auth.identity_provider import ExternalUser
from trendstorm.shared.errors import ConflictError, NotFoundError, SignupNotAllowedError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal in-memory fakes
# ---------------------------------------------------------------------------


class _FakeUserRepo:
    def __init__(self) -> None:
        self._store: dict[str, User] = {}

    async def insert(self, user: User, *, session=None) -> None:
        if any(u.email == user.email for u in self._store.values()):
            raise ConflictError("email already used")
        self._store[user.id] = user

    async def get(self, user_id: str) -> User | None:
        return self._store.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._store.values() if u.email == email.lower()), None)

    async def get_by_subject(self, subject: str) -> User | None:
        return next((u for u in self._store.values() if u.identity_provider_subject == subject), None)

    async def update(self, user: User) -> None:
        self._store[user.id] = user

    async def tombstone(self, uid, *, deleted_at, purge_at): ...
    async def cancel_tombstone(self, uid): ...
    async def list_due_for_purge(self, *, limit=50): return []
    async def hard_delete(self, uid): ...
    async def set_email_verified(self, uid): ...


class _FakeOrgRepo:
    def __init__(self) -> None:
        self._store: dict[str, Organization] = {}

    async def insert(self, org: Organization, *, session=None) -> None:
        self._store[org.id] = org

    async def get(self, org_id: str) -> Organization | None:
        return self._store.get(org_id)

    async def get_by_slug(self, slug): return None
    async def get_by_name(self, name): return None
    async def update(self, org): self._store[org.id] = org
    async def transfer_ownership(self, oid, uid): ...
    async def mark_orphaned(self, oid): ...
    async def list_for_user(self, uid): return []


class _FakeMembershipRepo:
    def __init__(self) -> None:
        self._store: dict[str, Membership] = {}

    async def insert(self, m: Membership, *, session=None) -> None:
        self._store[m.id] = m

    async def get(self, tid, mid): return self._store.get(mid)
    async def get_for_user(self, tid, uid): return next((m for m in self._store.values() if m.tenant_id == tid and m.user_id == uid), None)
    async def list_for_tenant(self, tid): return [m for m in self._store.values() if m.tenant_id == tid]
    async def list_for_user(self, uid): return [m for m in self._store.values() if m.user_id == uid]
    async def list_admins_for_tenant(self, tid): return []
    async def update_roles(self, tid, mid, roles): ...
    async def update_last_active(self, tid, uid): ...
    async def delete(self, tid, mid, *, session=None): ...
    async def delete_all_for_user(self, uid, *, session=None): ...


class _FakeInviteRepo:
    def __init__(self) -> None:
        self._store: dict[str, Invite] = {}

    async def insert(self, i: Invite, *, session=None) -> None:
        self._store[i.id] = i

    async def get(self, tid, iid): return self._store.get(iid)

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        return next((i for i in self._store.values() if i.token_hash == token_hash), None)

    async def get_pending_for_email(self, tid, email): return None
    async def list_pending_for_tenant(self, tid, *, limit=50, before_id=None): return []
    async def accept(self, tid, iid, *, session=None): ...
    async def revoke(self, tid, iid): ...


class _FakeIdP:
    async def create_user(self, email: str, password: str) -> ExternalUser:
        return ExternalUser(subject=f"auth0|{email}", email=email, email_verified=False)

    async def authenticate(self, email, password): ...
    async def set_password(self, subject, new_password): ...
    async def mark_email_verified(self, subject): ...
    async def delete_user(self, subject): ...
    async def get_oauth_authorize_url(self, provider, state, redirect_uri): return ""
    async def exchange_oauth_code(self, provider, code, state, redirect_uri): ...


class _FakeEmail:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_templated(self, to, template, variables) -> None:
        self.sent.append({"to": to, "template": template})

    async def send_raw(self, to, subject, html, text) -> None:
        self.sent.append({"to": to, "subject": subject})


class _FakeMongoInner:
    """Inner object that exposes start_session() — mirrors MongoClient.client."""

    async def start_session(self):
        return _FakeSession()


class _FakeMongo:
    """Minimal mongo client; RegistrationService accesses self._mongo.client.start_session()."""

    def __init__(self) -> None:
        self.client = _FakeMongoInner()


class _FakeSession:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    def start_transaction(self):
        return _FakeTxn()


class _FakeTxn:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_service(signup_mode: str = "open") -> tuple:
    from trendstorm.services.auth.registration_service import RegistrationService
    from trendstorm.shared.config import EmailSettings, SignupSettings

    user_repo = _FakeUserRepo()
    org_repo = _FakeOrgRepo()
    membership_repo = _FakeMembershipRepo()
    invite_repo = _FakeInviteRepo()
    idp = _FakeIdP()
    email = _FakeEmail()

    svc = RegistrationService(
        user_repo=user_repo,
        org_repo=org_repo,
        membership_repo=membership_repo,
        invite_repo=invite_repo,
        identity_provider=idp,
        email_provider=email,
        mongo=_FakeMongo(),
        signup_settings=SignupSettings(signup_mode=signup_mode),
        email_settings=EmailSettings(app_base_url="http://localhost"),
    )
    return svc, user_repo, org_repo, membership_repo, invite_repo, idp, email


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_account_open_mode_creates_user_and_org() -> None:
    svc, user_repo, org_repo, membership_repo, *_ = _make_service("open")
    user, org = await svc.create_account("Alice@Example.com", "P@ssw0rd!")

    assert user.email == "alice@example.com"
    assert user.email_verified is False
    assert org.owner_user_id == user.id
    memberships = await membership_repo.list_for_tenant(org.id)
    assert len(memberships) == 1
    assert Role.OWNER in memberships[0].roles


@pytest.mark.asyncio
async def test_create_account_invite_only_without_token_raises() -> None:
    svc, *_ = _make_service("invite_only")
    with pytest.raises(SignupNotAllowedError):
        await svc.create_account("bob@example.com", "P@ssw0rd!")


@pytest.mark.asyncio
async def test_create_account_closed_always_raises() -> None:
    svc, *_ = _make_service("closed")
    with pytest.raises(SignupNotAllowedError):
        await svc.create_account("carol@example.com", "P@ssw0rd!")


@pytest.mark.asyncio
async def test_create_account_invite_only_with_invalid_token_raises() -> None:
    svc, *_ = _make_service("invite_only")
    with pytest.raises(NotFoundError):
        await svc.create_account("dave@example.com", "P@ssw0rd!", invite_token="bad-token")


@pytest.mark.asyncio
async def test_welcome_email_is_sent_on_success() -> None:
    svc, _, _, _, _, _, email_provider = _make_service("open")
    await svc.create_account("eve@example.com", "P@ssw0rd!")
    sent = [m for m in email_provider.sent if m["template"] == "welcome"]
    assert len(sent) == 1
    assert sent[0]["to"] == "eve@example.com"


@pytest.mark.asyncio
async def test_email_case_normalised() -> None:
    svc, user_repo, *_ = _make_service("open")
    user, _ = await svc.create_account("Frank@EXAMPLE.COM", "P@ssw0rd!")
    assert user.email == "frank@example.com"
    found = await user_repo.get_by_email("frank@example.com")
    assert found is not None
    assert found.id == user.id
