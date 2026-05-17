from typing import Annotated

from fastapi import Header


async def require_tenant(x_tenant_id: Annotated[str, Header(alias="x-tenant-id")]) -> str:
    return x_tenant_id
