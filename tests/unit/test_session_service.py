"""Unit tests for SessionService.

No Docker — uses in-memory fakes for Redis, user repo, membership repo, session repo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.domain.sessions.models import RefreshSession
from trendstorm.domain.users.models import User
from trendstorm.services.auth.session_service import SessionService
from trendstorm.services.auth.token_utils import hash_token
from trendstorm.shared.errors import AuthenticationError

pytestmark = pytest.mark.unit

_JWT_SETTINGS_KWARGS = {
    "secret": "test-secret-32-chars-xxxxxxxxxxx",
    "algorithm": "HS256",
    "access_token_expire_minutes": 15,
    "refresh_token_expire_days": 30,
}


class _FakeUserRepo:
    def __init__(self, user: User) -> None:
        self._user = user

    async def get(self, uid: str) -> User | None:
        return self._user if self._user.id == uid else None

    async def get_by_email(self, email): ...
    async def get_by_subject(self, subject): ...
    async def insert(self, user, *, session=None): ...
    async def update(self, user): ...
    async def tombstone(self, uid, *, deleted_at, purge_at): ...
    async def cancel_tombstone(self, uid): ...
    async def list_due_for_purge(self, *, limit=50): return []
    async def hard_delete(self, uid): ...
    async def set_email_verified(self, uid): ...


class _FakeMembershipRepo:
    def __init__(self, membership: Membership | None = None) -> None:
        self._m = membership

    async def get_for_user(self, tid: str, uid: str) -> Membership | None:
        if self._m and self._m.tenant_id == tid and self._m.user_id == uid:
            return self._m
        return None

    async def list_for_user(self, uid): return [self._m] if self._m and self._m.user_id == uid else []
    async def insert(self, m, *, session=None): ...
    async def get(self, tid, mid): ...
    async def list_for_tenant(self, tid): ...
    async def list_admins_for_tenant(self, tid): return []
    async def update_roles(self, tid, mid, roles): ...
    async def update_last_active(self, tid, uid): ...
    async def delete(self, tid, mid, *, session=None): ...
    async def delete_all_for_user(self, uid, *, session=None): ...


class _FakeSessionRepo:
    def __init__(self) -> None:
        self._sessions: dict[str, RefreshSession] = {}
        self._by_hash: dict[str, str] = {}

    async def insert(self, s: RefreshSession) -> None:
        self._sessions[s.id] = s
        self._by_hash[s.refresh_token_hash] = s.id

    async def get(self, sid: str) -> RefreshSession | None:
        return self._sessions.get(sid)

    async def get_by_token_hash(self, h: str) -> RefreshSession | None:
        sid = self._by_hash.get(h)
        return self._sessions.get(sid) if sid else None

    async def list_active_for_user(self, uid: str) -> list[RefreshSession]:
        return [s for s in self._sessions.values() if s.user_id == uid and s.is_active]

    async def update_last_used(self, sid: str) -> None:
        if sid in self._sessions:
            s = self._sessions[sid]
            self._sessions[sid] = s.model_copy(update={"last_used_at": datetime.now(UTC)})

    async def revoke(self, sid: str) -> None:
        if sid in self._sessions:
            s = self._sessions[sid]
            self._sessions[sid] = s.model_copy(update={"revoked_at": datetime.now(UTC)})

    async def revoke_all_for_user(self, uid: str) -> None:
        now = datetime.now(UTC)
        for sid, s in self._sessions.items():
            if s.user_id == uid and s.revoked_at is None:
                self._sessions[sid] = s.model_copy(update={"revoked_at": now})


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    @property
    def client(self):
        return self

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value

    async def get(self, key: str):
        v = self._store.get(key)
        return v.encode() if v else None

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def incr(self, key: str) -> int:
        v = int(self._store.get(key, 0)) + 1
        self._store[key] = str(v)
        return v

    async def expire(self, key: str, ttl: int) -> None:
        pass  # no-op in tests


def _make_service(user: User, membership: Membership | None = None) -> tuple[SessionService, _FakeSessionRepo, _FakeRedis]:
    from trendstorm.shared.config import JWTSettings

    session_repo = _FakeSessionRepo()
    redis = _FakeRedis()
    svc = SessionService(
        session_repo=session_repo,
        user_repo=_FakeUserRepo(user),
        membership_repo=_FakeMembershipRepo(membership),
        redis=redis,
        jwt_settings=JWTSettings(**_JWT_SETTINGS_KWARGS),
    )
    return svc, session_repo, redis


@pytest.fixture
def user() -> User:
    return User(email="alice@example.com", email_verified=True)


@pytest.fixture
def membership(user: User) -> Membership:
    return Membership(
        tenant_id="org-1",
        user_id=user.id,
        roles=[Role.OWNER],
    )


@pytest.mark.asyncio
async def test_issue_session_returns_jwt_and_refresh(user, membership) -> None:
    svc, repo, redis = _make_service(user, membership)
    access, refresh = await svc.issue_session(user.id, "org-1")
    claims = svc.verify_access_jwt(access)
    assert claims["sub"] == user.id
    assert claims["tenant_id"] == "org-1"
    assert "owner" in claims["roles"]
    # Refresh token stored in Redis.
    rt_hash = hash_token(refresh)
    stored = await redis.get(f"rt:{rt_hash}")
    assert stored is not None


@pytest.mark.asyncio
async def test_refresh_session_rotates_token(user, membership) -> None:
    svc, repo, redis = _make_service(user, membership)
    access, old_refresh = await svc.issue_session(user.id, "org-1")
    new_access, new_refresh = await svc.refresh_session(old_refresh)

    # Old token no longer in Redis.
    old_hash = hash_token(old_refresh)
    assert await redis.get(f"rt:{old_hash}") is None

    # New token present.
    new_hash = hash_token(new_refresh)
    assert await redis.get(f"rt:{new_hash}") is not None

    # New JWT is valid.
    claims = svc.verify_access_jwt(new_access)
    assert claims["sub"] == user.id


@pytest.mark.asyncio
async def test_revoke_session_removes_from_redis(user, membership) -> None:
    svc, repo, redis = _make_service(user, membership)
    _, refresh = await svc.issue_session(user.id, "org-1")
    await svc.revoke_session(refresh)
    rt_hash = hash_token(refresh)
    assert await redis.get(f"rt:{rt_hash}") is None


@pytest.mark.asyncio
async def test_refresh_with_invalid_token_raises(user, membership) -> None:
    svc, *_ = _make_service(user, membership)
    with pytest.raises(AuthenticationError, match="not found or expired"):
        await svc.refresh_session("bogus-token")


@pytest.mark.asyncio
async def test_verify_expired_jwt_raises(user, membership) -> None:
    import jwt as pyjwt
    from trendstorm.shared.config import JWTSettings

    svc, *_ = _make_service(user, membership)
    expired_payload = {
        "sub": user.id,
        "tenant_id": "org-1",
        "email": user.email,
        "roles": [],
        "iat": 0,
        "exp": 1,  # epoch 1 = definitely expired
    }
    expired_token = pyjwt.encode(expired_payload, "test-secret-32-chars-xxxxxxxxxxx", algorithm="HS256")
    with pytest.raises(AuthenticationError, match="expired"):
        svc.verify_access_jwt(expired_token)
