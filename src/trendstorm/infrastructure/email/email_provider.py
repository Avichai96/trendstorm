"""EmailProvider Protocol.

Concrete implementations: PostmarkProvider (production), DevProvider (local).
All templates are identified by name; variables are substituted by the
concrete provider using its own template engine or Postmark template IDs.
"""

from __future__ import annotations

from typing import Any, Protocol


class EmailProvider(Protocol):
    async def send_templated(
        self,
        to: str,
        template: str,
        variables: dict[str, Any],
    ) -> None:
        """Send a pre-defined template email.

        template: logical name — "verify", "invite", "reset", "welcome",
                  "member_added", "deletion_scheduled", "deletion_cancelled".
        variables: substitution context for the template.
        """
        ...

    async def send_raw(
        self,
        to: str,
        subject: str,
        html: str,
        text: str,
    ) -> None:
        """Send a raw email with HTML + plain-text body."""
        ...
