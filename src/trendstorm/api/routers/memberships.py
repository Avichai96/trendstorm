"""Memberships router.

GET    /v1/memberships              — list members of the active org
PATCH  /v1/memberships/{id}/roles   — update a member's roles (admin only)
DELETE /v1/memberships/{id}         — remove a member (admin only; cannot remove owner)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from trendstorm.api.deps import MongoDep
from trendstorm.domain.auth.models import AuthContext
from trendstorm.domain.memberships.models import Membership, Role
from trendstorm.infrastructure.mongo.repositories.membership_repository import (
    MongoMembershipRepository,
)
from trendstorm.shared.errors import AuthorizationError, BusinessRuleError, NotFoundError
from trendstorm.shared.logging import get_logger
from trendstorm.utils.headers_docs import require_tenant

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/memberships",
    tags=["memberships"],
    dependencies=[Depends(require_tenant)],
)


class MembershipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    user_id: str
    tenant_id: str
    roles: list[str]


class UpdateRolesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[Role]


def _to_response(m: Membership) -> MembershipResponse:
    return MembershipResponse(
        id=m.id,
        user_id=m.user_id,
        tenant_id=m.tenant_id,
        roles=[r.value for r in m.roles],
    )


def _auth_context(request: Request) -> AuthContext:
    ctx = getattr(request.state, "auth_context", None)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    return ctx  # type: ignore[no-any-return]


@router.get("")
async def list_members(request: Request, mongo: MongoDep) -> list[MembershipResponse]:
    ctx = _auth_context(request)
    repo = MongoMembershipRepository(mongo)
    members = await repo.list_for_tenant(ctx.tenant_id)
    return [_to_response(m) for m in members]


@router.patch("/{membership_id}/roles")
async def update_roles(
    membership_id: str, body: UpdateRolesRequest, request: Request, mongo: MongoDep
) -> MembershipResponse:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot modify memberships.")
    repo = MongoMembershipRepository(mongo)
    caller = await repo.get_for_user(ctx.tenant_id, ctx.user_id)
    if caller is None or not caller.is_admin_or_above:
        raise AuthorizationError("Admin role required.")
    if Role.OWNER in body.roles and not caller.is_owner:
        raise AuthorizationError("Only owners can grant the owner role.")

    target = await repo.get(ctx.tenant_id, membership_id)
    if target is None:
        raise NotFoundError("Membership not found.")
    if target.is_owner and caller.user_id != ctx.user_id:
        raise BusinessRuleError("Cannot change the owner's roles.", code="cannot_change_owner")

    updated = await repo.update_roles(ctx.tenant_id, membership_id, body.roles)
    if updated is None:
        raise NotFoundError("Membership not found.")
    return _to_response(updated)


@router.delete("/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(membership_id: str, request: Request, mongo: MongoDep) -> None:
    ctx = _auth_context(request)
    if ctx.user_id is None:
        raise AuthorizationError("API key auth cannot remove members.")
    repo = MongoMembershipRepository(mongo)
    caller = await repo.get_for_user(ctx.tenant_id, ctx.user_id)
    if caller is None or not caller.is_admin_or_above:
        raise AuthorizationError("Admin role required.")
    target = await repo.get(ctx.tenant_id, membership_id)
    if target is None:
        raise NotFoundError("Membership not found.")
    if target.is_owner:
        raise BusinessRuleError(
            "Cannot remove the organization owner. Transfer ownership first.",
            code="cannot_remove_owner",
        )
    await repo.delete(ctx.tenant_id, membership_id)
    logger.info("membership.removed", membership_id=membership_id, removed_by=ctx.user_id)
