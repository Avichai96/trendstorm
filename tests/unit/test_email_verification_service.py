"""Unit tests for EmailVerificationService.

No Docker — uses in-memory fakes for all dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trendstorm.domain.email_verifications.models import EmailVerification
from trendstorm.domain.users.models import User
from trendstorm.services.auth.token_utils import hash_token
from trendstorm.shared.errors import NotFoundError, RateLimitError, TokenUsedError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeVerificationRepo:
    def __init__(self) -> None:
        self._store: dict[str, EmailVerification] = {}

    async def insert(self, v: EmailVerification, *, session=None) -> None:
        self._store[v.id] = v

    async def get_by_token_hash(self, token_hash: str) -> EmailVerification | None:
        return next((v for v in self._store.values() if v.token_hash == token_hash), None)

    async def delete_pending_for_user(self, user_id: str) -> None:
        ids = [vid for vid, v in self._store.items()
               if v.user_id == user_id and v.consumed_at is None]
        for vid in ids:
            del self._store[vid]

    async def consume(self, verification_id: str) -> None:
        v = self._store.get(verification_id)
        if v:
            self._store[verification_id] = v.model_copy(update={"consumed_at": datetime.now(UTC)})


class _FakeUserRepo:
    def __init__(self, user: User) -> None:
        self._store: dict[str, User] = {user.id: user}

    async def get(self, uid: str) -> User | None:
        return self._store.get(uid)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._store.values() if u.email == email.lower()), None)

    async def set_email_verified(self, uid: str) -> User | None:
        u = self._store.get(uid)
        if u:
            self._store[uid] = u.model_copy(update={"email_verified": True})
        return self._store.get(uid)

    async def get_by_subject(self, subject): ...
    async def insert(self, user, *, session=None): self._store[user.id] = user
    async def update(self, user): self._store[user.id] = user
    async def tombstone(self, uid, *, deleted_at, purge_at): ...
    async def cancel_tombstone(self, uid): ...
    async def list_due_for_purge(self, *, limit=50): return []
    async def hard_delete(self, uid): ...


class _FakeIdP:
    def __init__(self) -> None:
        self.verified_subjects: list[str] = []

    async def mark_email_verified(self, subject: str) -> None:
        self.verified_subjects.append(subject)

    async def create_user(self, email, password): ...
    async def authenticate(self, email, password): ...
    async def set_password(self, subject, new_password): ...
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


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    @property
    def client(self) -> "_FakeRedis":
        return self

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


def _make_service(user: User) -> tuple:
    from trendstorm.services.auth.email_verification_service import EmailVerificationService
    from trendstorm.shared.config import EmailSettings

    verification_repo = _FakeVerificationRepo()
    user_repo = _FakeUserRepo(user)
    idp = _FakeIdP()
    email = _FakeEmail()
    redis = _FakeRedis()

    svc = EmailVerificationService(
        verification_repo=verification_repo,
        user_repo=user_repo,
        identity_provider=idp,
        email_provider=email,
        redis=redis,
        email_settings=EmailSettings(app_base_url="http://localhost"),
    )
    return svc, verification_repo, user_repo, idp, email, redis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def unverified_user() -> User:
    return User(
        email="alice@example.com",
        email_verified=False,
        identity_provider_subject="auth0|alice",
    )


@pytest.fixture
def verified_user() -> User:
    return User(
        email="bob@example.com",
        email_verified=True,
        identity_provider_subject="auth0|bob",
    )


# ---------------------------------------------------------------------------
# Tests — request_verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_verification_sends_email(unverified_user: User) -> None:
    svc, verif_repo, _, _, email, _ = _make_service(unverified_user)
    await svc.request_verification(unverified_user)

    assert len(verif_repo._store) == 1
    sent = [m for m in email.sent if m["template"] == "verify"]
    assert len(sent) == 1
    assert sent[0]["to"] == unverified_user.email


@pytest.mark.asyncio
async def test_request_verification_no_op_if_already_verified(verified_user: User) -> None:
    svc, verif_repo, _, _, email, _ = _make_service(verified_user)
    await svc.request_verification(verified_user)

    # No record created, no email sent.
    assert len(verif_repo._store) == 0
    assert len(email.sent) == 0


@pytest.mark.asyncio
async def test_request_verification_rate_limited(unverified_user: User) -> None:
    svc, *_, redis = _make_service(unverified_user)
    # Exhaust the 3-per-hour limit.
    for _ in range(3):
        await svc.request_verification(unverified_user)
    with pytest.raises(RateLimitError):
        await svc.request_verification(unverified_user)


@pytest.mark.asyncio
async def test_request_verification_deletes_existing_pending(unverified_user: User) -> None:
    svc, verif_repo, *_ = _make_service(unverified_user)
    # Pre-existing pending verification.
    old = EmailVerification(
        user_id=unverified_user.id,
        email=unverified_user.email,
        token_hash=hash_token("old-verify-token"),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    verif_repo._store[old.id] = old

    await svc.request_verification(unverified_user)

    assert old.id not in verif_repo._store
    assert len(verif_repo._store) == 1


# ---------------------------------------------------------------------------
# Tests — consume_verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_verification_marks_email_verified(unverified_user: User) -> None:
    svc, verif_repo, user_repo, idp, _, _ = _make_service(unverified_user)

    plaintext = "my-verify-token"
    verification = EmailVerification(
        user_id=unverified_user.id,
        email=unverified_user.email,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    verif_repo._store[verification.id] = verification

    result = await svc.consume_verification(plaintext)

    assert result.email_verified is True
    # Verification consumed in repo.
    assert verif_repo._store[verification.id].consumed_at is not None
    # IdP notified.
    assert unverified_user.identity_provider_subject in idp.verified_subjects


@pytest.mark.asyncio
async def test_consume_verification_raises_on_invalid_token(unverified_user: User) -> None:
    svc, *_ = _make_service(unverified_user)
    with pytest.raises(NotFoundError, match="Verification token not found"):
        await svc.consume_verification("nonexistent")


@pytest.mark.asyncio
async def test_consume_verification_raises_on_already_consumed(unverified_user: User) -> None:
    svc, verif_repo, *_ = _make_service(unverified_user)

    plaintext = "my-used-verify-token"
    verification = EmailVerification(
        user_id=unverified_user.id,
        email=unverified_user.email,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        consumed_at=datetime.now(UTC),
    )
    verif_repo._store[verification.id] = verification

    with pytest.raises(TokenUsedError):
        await svc.consume_verification(plaintext)


@pytest.mark.asyncio
async def test_consume_verification_raises_on_expired(unverified_user: User) -> None:
    from trendstorm.shared.errors import TokenExpiredError

    svc, verif_repo, *_ = _make_service(unverified_user)

    plaintext = "my-expired-verify-token"
    verification = EmailVerification(
        user_id=unverified_user.id,
        email=unverified_user.email,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    verif_repo._store[verification.id] = verification

    with pytest.raises(TokenExpiredError):
        await svc.consume_verification(plaintext)


@pytest.mark.asyncio
async def test_consume_verification_idp_failure_is_non_fatal(unverified_user: User) -> None:
    """IdP mark-verified failure must not abort the consume flow."""
    svc, verif_repo, user_repo, idp, *_ = _make_service(unverified_user)

    # Make IdP blow up.
    async def _fail(subject: str) -> None:
        raise RuntimeError("IdP down")

    idp.mark_email_verified = _fail  # type: ignore[method-assign]

    plaintext = "my-verify-token-2"
    verification = EmailVerification(
        user_id=unverified_user.id,
        email=unverified_user.email,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    verif_repo._store[verification.id] = verification

    # Should not raise; user should still be marked verified in our DB.
    result = await svc.consume_verification(plaintext)
    assert result.email_verified is True
