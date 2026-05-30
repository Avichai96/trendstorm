"""Postmark implementation of EmailProvider.

Postmark API docs: https://postmarkapp.com/developer/api/email-api
Templates are defined in the Postmark dashboard; we reference them by alias.
Alias → Postmark template alias mapping lives in _TEMPLATE_ALIASES.

SPF, DKIM, and DMARC records must be configured on the sending domain
before production use. See ops/runbooks/email-deliverability.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from trendstorm.shared.errors import ExternalServiceError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.shared.config import EmailSettings

logger = get_logger(__name__)

_POSTMARK_API = "https://api.postmarkapp.com"

# Logical template name → Postmark template alias.
# Create these aliases in the Postmark dashboard before deploying.
_TEMPLATE_ALIASES: dict[str, str] = {
    "verify": "trendstorm-verify-email",
    "invite": "trendstorm-invite",
    "reset": "trendstorm-password-reset",
    "welcome": "trendstorm-welcome",
    "member_added": "trendstorm-member-added",
    "deletion_scheduled": "trendstorm-deletion-scheduled",
    "deletion_cancelled": "trendstorm-deletion-cancelled",
}


class PostmarkProvider:
    """Postmark-backed EmailProvider."""

    def __init__(self, settings: EmailSettings) -> None:
        self._token = settings.postmark_server_token.get_secret_value()
        self._from_email = settings.from_email
        self._from_name = settings.from_name

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self._token,
        }

    async def send_templated(
        self, to: str, template: str, variables: dict[str, Any]
    ) -> None:
        alias = _TEMPLATE_ALIASES.get(template, template)
        payload = {
            "From": f"{self._from_name} <{self._from_email}>",
            "To": to,
            "TemplateAlias": alias,
            "TemplateModel": variables,
            "MessageStream": "outbound",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_POSTMARK_API}/email/withTemplate",
                headers=self._headers(),
                json=payload,
            )
        if not resp.is_success:
            raise ExternalServiceError(
                f"Postmark send_templated failed: HTTP {resp.status_code}",
                code="email_send_failed",
                context={"template": template, "to": to, "status": resp.status_code},
            )
        logger.info("email.sent", template=template, to=to)

    async def send_raw(self, to: str, subject: str, html: str, text: str) -> None:
        payload = {
            "From": f"{self._from_name} <{self._from_email}>",
            "To": to,
            "Subject": subject,
            "HtmlBody": html,
            "TextBody": text,
            "MessageStream": "outbound",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_POSTMARK_API}/email",
                headers=self._headers(),
                json=payload,
            )
        if not resp.is_success:
            raise ExternalServiceError(
                f"Postmark send_raw failed: HTTP {resp.status_code}",
                code="email_send_failed",
                context={"subject": subject, "to": to, "status": resp.status_code},
            )
        logger.info("email.sent_raw", subject=subject, to=to)
