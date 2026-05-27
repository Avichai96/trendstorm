from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, Header, HTTPException, Request

if TYPE_CHECKING:
    pass


async def require_tenant(x_tenant_id: Annotated[str, Header(alias="x-tenant-id")]) -> str:
    return x_tenant_id


def require_role(role: str) -> Callable[..., None]:
    """FastAPI dependency that enforces a specific role on the request principal.

    Works for both API key auth (roles from ApiKey.roles) and JWT auth (roles
    from the "roles" claim). AuthMiddleware populates request.state.auth_context
    before any route handler runs.

    Usage:
        @router.get("/...", dependencies=[require_role("reviewer")])
    """
    def _check(request: Request) -> None:
        ctx = getattr(request.state, "auth_context", None)
        if ctx is None or role not in ctx.roles:
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": f"Role '{role}' required."},
            )

    return Depends(_check)
