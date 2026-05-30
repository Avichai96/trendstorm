"""Dev EmailProvider — writes emails to stdout and an in-memory inbox.

Wired up when EMAIL__PROVIDER=dev (local development and integration tests).
Never used in production — PostmarkProvider is the production impl.

The in-memory inbox is queryable from tests:
    from trendstorm.infrastructure.email.dev_provider import DevEmailProvider
    provider = DevEmailProvider(...)
    # ... trigger some flow ...
    msgs = provider.inbox_for("user@example.com")
    token = msgs[0]["variables"]["token"]
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class DevEmailProvider:
    """Local dev EmailProvider. Thread-safe for single-process async use."""

    def __init__(self, from_email: str = "dev@trendstorm.local") -> None:
        self._from = from_email
        # inbox: {recipient: [message_dict, ...]}
        self._inbox: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def send_templated(
        self, to: str, template: str, variables: dict[str, Any]
    ) -> None:
        msg = {"to": to, "template": template, "variables": variables}
        self._inbox[to].append(msg)
        logger.info(
            "dev_email.sent",
            to=to,
            template=template,
            variables=json.dumps(variables, default=str),
        )

    async def send_raw(self, to: str, subject: str, html: str, text: str) -> None:
        msg = {"to": to, "subject": subject, "html": html, "text": text}
        self._inbox[to].append(msg)
        logger.info("dev_email.sent_raw", to=to, subject=subject)

    # ------------------------------------------------------------------ #
    # Test helpers                                                         #
    # ------------------------------------------------------------------ #

    def inbox_for(self, email: str) -> list[dict[str, Any]]:
        """Return all messages sent to this recipient (latest last)."""
        return list(self._inbox.get(email, []))

    def latest_for(self, email: str, template: str | None = None) -> dict[str, Any] | None:
        """Return the most recent message for this recipient, optionally filtered by template."""
        msgs = self._inbox.get(email, [])
        if template is not None:
            msgs = [m for m in msgs if m.get("template") == template]
        return msgs[-1] if msgs else None

    def clear(self) -> None:
        """Reset inbox between tests."""
        self._inbox.clear()
