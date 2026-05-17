#!/usr/bin/env python3
"""Bootstrap CLI: create a tenant + first API key.

Use this ONCE to set up the first tenant so that real API requests can be
made. Subsequent tenants and keys are created via the REST API itself.

Usage:
    uv run python scripts/generate_api_key.py --name "Acme Corp"

Options:
    --name      Tenant display name (required)
    --plan      Tenant plan: free | pro | enterprise  (default: free)
    --key-name  Label for the API key                 (default: "default")
    --key-env   Key environment: live | test          (default: live)

Output:
    Prints the tenant ID and raw API key to stdout. The key is shown ONCE
    and cannot be recovered — store it immediately (e.g. in 1Password or
    your CI secret store).

Exit codes:
    0   success
    1   configuration or connectivity error
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import motor.motor_asyncio as motor

from trendstorm.domain.auth.models import ApiKey, Tenant
from trendstorm.infrastructure.auth.api_key import generate_api_key, hash_key, key_prefix
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories.api_key_repository import MongoApiKeyRepository
from trendstorm.infrastructure.mongo.repositories.tenant_repository import MongoTenantRepository
from trendstorm.shared.config import get_settings
from trendstorm.shared.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def _run(name: str, plan: str, key_name: str, key_env: str) -> int:
    settings = get_settings()
    mongo = MongoClient(settings.mongo)
    await mongo.connect()

    try:
        tenant_repo = MongoTenantRepository(mongo)
        key_repo = MongoApiKeyRepository(mongo)

        # Create tenant
        tenant = Tenant(name=name, plan=plan)  # type: ignore[arg-type]
        await tenant_repo.insert(tenant)
        logger.info("tenant_created", tenant_id=tenant.id, name=name, plan=plan)

        # Create first API key
        raw = generate_api_key(key_env)  # type: ignore[arg-type]
        api_key = ApiKey(
            tenant_id=tenant.id,
            name=key_name,
            key_hash=hash_key(raw),
            key_prefix=key_prefix(raw),
        )
        await key_repo.insert(api_key)
        logger.info("api_key_created", key_id=api_key.id, tenant_id=tenant.id)

        print("\n" + "=" * 60)
        print(f"  Tenant ID : {tenant.id}")
        print(f"  Tenant    : {name} ({plan})")
        print(f"  Key ID    : {api_key.id}")
        print(f"  Key prefix: ts_{key_env}_{api_key.key_prefix}…")
        print(f"\n  API KEY (save this — shown once):\n")
        print(f"    {raw}")
        print("=" * 60 + "\n")
        return 0

    except Exception as exc:
        logger.exception("bootstrap_failed", error=str(exc))
        return 1
    finally:
        await mongo.close()


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(description="Bootstrap first TrendStorm tenant + API key.")
    parser.add_argument("--name", required=True, help="Tenant display name")
    parser.add_argument("--plan", default="free", choices=["free", "pro", "enterprise"])
    parser.add_argument("--key-name", default="default", help="Label for the API key")
    parser.add_argument("--key-env", default="live", choices=["live", "test"])
    args = parser.parse_args()

    code = asyncio.run(_run(
        name=args.name,
        plan=args.plan,
        key_name=args.key_name,
        key_env=args.key_env,
    ))
    sys.exit(code)


if __name__ == "__main__":
    main()
