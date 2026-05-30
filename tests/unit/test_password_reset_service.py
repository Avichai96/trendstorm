"""Unit tests for PasswordResetService.

No Docker — uses in-memory fakes for all dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trendstorm.domain.password_resets.models import PasswordReset
from trendstorm.domain.users.models import User
from trendstorm.services.auth.token_utils import hash_token
from trendstorm.shared.errors import NotFoundError, RateLimitError, TokenUsedError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakePasswordResetRepo:
    def __init__(self) -> None:
        self._store: dict[str, PasswordReset] = {}

    async def insert(self, r: PasswordReset, *, session=None) -> None:
        self._store[r.id] = r

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None:
        return next((r for r in self._store.values() if r.token_hash == token_hash), None)

    async def delete_pending_for_user(self, user_id: str) -> None:
        ids = [rid for rid, r in self._store.items()
               if r.user_id == user_id and r.consumed_at is None]
        for rid in ids:
            del self._store[rid]

    async def consume(self, reset_id: str) -> None:
        r = self._store.get(reset_id)
        if r:
            self._store[reset_id] = r.model_copy(update={"consumed_at": datetime.now(UTC)})


class _FakeUserRepo:
    def __init__(self, user: User | None = None) -> None:
        self._store: dict[str, User] = {}
        if user:
            self._store[user.id] = user

    async def get(self, uid: str) -> User | None:
        return self._store.get(uid)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._store.values() if u.email == email.lower()), None)

    async def get_by_subject(self, subject): ...
    async def insert(self, user, *, session=None): self._store[user.id] = user
    async def update(self, user): self._store[user.id] = user
    async def tombstone(self, uid, *, deleted_at, purge_at): ...
    async def cancel_tombstone(self, uid): ...
    async def list_due_for_purge(self, *, limit=50): return []
    async def hard_delete(self, uid): ...
    async def set_email_verified(self, uid): ...


class _FakeIdP:
    def __init__(self) -> None:
        self.password_updates: list[tuple[str, str]] = []

    async def set_password(self, subject: str, new_password: str) -> None:
        self.password_updates.append((subject, new_password))

    async def create_user(self, email, password): ...
    async def authenticate(self, email, password): ...
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
        self.sent.append({"to": to})


class _FakeSessionSvc:
    def __init__(self) -> None:
        self.revoked_users: list[str] = []

    async def revoke_all_for_user(self, user_id: str) -> None:
        self.revoked_users.append(user_id)


class _FakePipeline:
    """Synchronous-command-collecting async pipeline."""

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store
        self._cmds: list[tuple] = []

    def incr(self, key: str) -> "_FakePipeline":
        self._cmds.append(("incr", key))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._cmds.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        results = []
        for cmd in self._cmds:
            if cmd[0] == "incr":
                key = cmd[1]
                v = int(self._store.get(key, 0)) + 1
                self._store[key] = str(v)
                results.append(v)
            elif cmd[0] == "expire":
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    @property
    def client(self) -> "_FakeRedis":
        return self

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self._store)

    async def incr(self, key: str) -> int:
        v = int(self._store.get(key, 0)) + 1
        self._store[key] = str(v)
        return v

    async def expire(self, key: str, ttl: int) -> None:
        pass

    async def get(self, key: str):
        v = self._store.get(key)
        return v.encode() if v else None

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_service(user: User | None = None) -> tuple:
    from trendstorm.services.auth.password_reset_service import PasswordResetService
    from trendstorm.shared.config import EmailSettings

    reset_repo = _FakePasswordResetRepo()
    user_repo = _FakeUserRepo(user)
    idp = _FakeIdP()
    email = _FakeEmail()
    session_svc = _FakeSessionSvc()
    redis = _FakeRedis()

    svc = PasswordResetService(
        reset_repo=reset_repo,
        user_repo=user_repo,
        identity_provider=idp,
        email_provider=email,
        session_service=session_svc,  # type: ignore[arg-type]
        redis=redis,
        email_settings=EmailSettings(app_base_url="http://localhost"),
    )
    return svc, reset_repo, user_repo, idp, email, session_svc, redis


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


# ---------------------------------------------------------------------------
# Tests — request_reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_reset_sends_email_for_known_user(active_user: User) -> None:
    svc, reset_repo, *_, email, session_svc, _ = _make_service(active_user)
    await svc.request_reset(active_user.email, ip="1.2.3.4")

    # A reset record was created.
    assert len(reset_repo._store) == 1

    # Reset email was dispatched.
    sent = [m for m in email.sent if m["template"] == "reset"]
    assert len(sent) == 1
    assert sent[0]["to"] == active_user.email


@pytest.mark.asyncio
async def test_request_reset_silent_for_unknown_email() -> None:
    svc, reset_repo, *_, email, session_svc, _ = _make_service()
    # No exception; no email sent; no reset record.
    await svc.request_reset("nobody@example.com", ip="1.2.3.4")
    assert len(reset_repo._store) == 0
    assert len(email.sent) == 0


@pytest.mark.asyncio
async def test_request_reset_normalises_email_case(active_user: User) -> None:
    svc, reset_repo, *_ = _make_service(active_user)
    await svc.request_reset("ALICE@EXAMPLE.COM", ip="1.2.3.4")
    assert len(reset_repo._store) == 1


@pytest.mark.asyncio
async def test_request_reset_rate_limited_by_email(active_user: User) -> None:
    svc, *_ = _make_service(active_user)
    # Exhaust the 5-per-hour email limit.
    for _ in range(5):
        await svc.request_reset(active_user.email, ip="1.2.3.4")
    with pytest.raises(RateLimitError):
        await svc.request_reset(active_user.email, ip="1.2.3.4")


@pytest.mark.asyncio
async def test_request_reset_rate_limited_by_ip(active_user: User) -> None:
    svc, *_ = _make_service(active_user)
    # Use 10 different emails from same IP to hit the IP limit.
    other_users = [User(email=f"user{i}@example.com") for i in range(10)]
    # Pre-populate the user repo.
    _, _, user_repo, *_ = _make_service(active_user)
    svc2, _, user_repo2, *_ = _make_service(active_user)
    # Simpler: directly inject the IP counter.
    svc._redis._store["pw_reset:ip:9.9.9.9"] = "10"  # type: ignore[attr-defined]
    with pytest.raises(RateLimitError):
        await svc.request_reset(active_user.email, ip="9.9.9.9")


# ---------------------------------------------------------------------------
# Tests — consume_reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_reset_updates_password_and_revokes_sessions(active_user: User) -> None:
    svc, reset_repo, _, idp, _, session_svc, _ = _make_service(active_user)

    plaintext = "my-reset-token-abc"
    reset = PasswordReset(
        user_id=active_user.id,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        requested_from_ip="1.2.3.4",
    )
    reset_repo._store[reset.id] = reset

    await svc.consume_reset(plaintext, "NewPass123!")

    # Password updated on IdP.
    assert len(idp.password_updates) == 1
    assert idp.password_updates[0] == (active_user.identity_provider_subject, "NewPass123!")
    # Token consumed.
    assert reset_repo._store[reset.id].consumed_at is not None
    # Sessions revoked.
    assert active_user.id in session_svc.revoked_users


@pytest.mark.asyncio
async def test_consume_reset_raises_on_invalid_token() -> None:
    svc, *_ = _make_service()
    with pytest.raises(NotFoundError, match="Reset token not found"):
        await svc.consume_reset("bogus-token", "NewPass!")


@pytest.mark.asyncio
async def test_consume_reset_raises_on_already_consumed(active_user: User) -> None:
    svc, reset_repo, *_ = _make_service(active_user)

    plaintext = "my-used-token"
    reset = PasswordReset(
        user_id=active_user.id,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        requested_from_ip="1.2.3.4",
        consumed_at=datetime.now(UTC),  # already consumed
    )
    reset_repo._store[reset.id] = reset

    with pytest.raises(TokenUsedError):
        await svc.consume_reset(plaintext, "NewPass!")


@pytest.mark.asyncio
async def test_consume_reset_raises_on_expired_token(active_user: User) -> None:
    from trendstorm.shared.errors import TokenExpiredError

    svc, reset_repo, *_ = _make_service(active_user)

    plaintext = "my-expired-token"
    reset = PasswordReset(
        user_id=active_user.id,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),  # expired
        requested_from_ip="1.2.3.4",
    )
    reset_repo._store[reset.id] = reset

    with pytest.raises(TokenExpiredError):
        await svc.consume_reset(plaintext, "NewPass!")


@pytest.mark.asyncio
async def test_request_reset_deletes_existing_pending(active_user: User) -> None:
    svc, reset_repo, *_ = _make_service(active_user)

    # Pre-existing reset record.
    old = PasswordReset(
        user_id=active_user.id,
        token_hash=hash_token("old-token"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        requested_from_ip="1.2.3.4",
    )
    reset_repo._store[old.id] = old

    await svc.request_reset(active_user.email, ip="1.2.3.4")

    # Old record gone, new one in its place.
    assert old.id not in reset_repo._store
    assert len(reset_repo._store) == 1
