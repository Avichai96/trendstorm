"""API keys resource — provision and revoke per-tenant API keys."""

from __future__ import annotations

from trendstorm_shared.models import (
    ApiKeyCreatedResponse,
    ApiKeyListResponse,
)

from ._base import AsyncAPIResource


class ApiKeysResource(AsyncAPIResource):
    """Manage API keys for the tenant.

    Examples::

        # Create a new key (plaintext returned ONCE — store it immediately)
        created = await ts.api_keys.create(name="ci-worker")
        secret = created.key  # save this; never returned again

        # List all keys
        keys = await ts.api_keys.list()
        for k in keys.keys:
            print(k.key_prefix, k.is_active)

        # Revoke a key
        await ts.api_keys.revoke(key_id)
    """

    async def create(self, *, name: str) -> ApiKeyCreatedResponse:
        """Create a new API key. The plaintext ``key`` field is returned ONCE."""
        data = await self._post("/v1/api-keys", {"name": name})
        return ApiKeyCreatedResponse.model_validate(data)

    async def list(self) -> ApiKeyListResponse:
        """List all API keys (active and revoked) for the tenant."""
        data = await self._get("/v1/api-keys")
        return ApiKeyListResponse.model_validate(data)

    async def revoke(self, key_id: str) -> None:
        """Revoke an API key immediately."""
        await self._delete(f"/v1/api-keys/{key_id}")
