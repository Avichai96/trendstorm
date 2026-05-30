# Runbook: Email Deliverability

**Scope:** Transactional email delivery via Postmark (Phase 16). Covers verification emails, password resets, invitations, deletion confirmations.

---

## 1. Diagnosing a Delivery Failure

All email sending is fire-and-forget in our services — a delivery failure does not abort the user action. The token is still created; the user can request a resend.

**Step 1: Check application logs**
```bash
grep -E "email_failed|postmark|send_templated" /var/log/trendstorm/api.log | tail -50
```

Look for warnings like `password_reset.email_failed` or `invitation.email_failed` with an `error=` field.

**Step 2: Check Postmark delivery status**
Log into [Postmark account dashboard](https://account.postmark.com) → Activity. Filter by recipient address or date range. Each message shows: delivered, bounced, spam-complained, or queued.

**Step 3: Check DNS / SPF / DKIM**
Postmark provides a domain verification page. Our sending domain must have:
- SPF record including Postmark's sending servers
- DKIM TXT record from Postmark
- DMARC policy (at minimum `p=none` with `rua=` reporting address)

Run: `dig TXT yourdomain.com` to verify SPF. If SPF or DKIM is missing, emails will land in spam or be rejected by major providers.

---

## 2. Template Name → Postmark Template Alias Mapping

`PostmarkProvider` maps logical template names to Postmark aliases. If a Postmark template is renamed or deleted, the send call returns a 422.

| Logical name | Postmark alias | Sent by |
|---|---|---|
| `verify` | `trendstorm-email-verify` | EmailVerificationService |
| `invite` | `trendstorm-invite` | InvitationService |
| `reset` | `trendstorm-password-reset` | PasswordResetService |
| `welcome` | `trendstorm-welcome` | RegistrationService |
| `member_added` | `trendstorm-member-added` | InvitationService (accept) |
| `deletion_scheduled` | `trendstorm-deletion-scheduled` | AccountDeletionService |
| `deletion_cancelled` | `trendstorm-deletion-cancelled` | AccountDeletionService |

To check all templates exist: Postmark dashboard → Templates. Any missing template will cause a 422 on the first send attempt.

---

## 3. Local Dev Email

In development (`EMAIL__PROVIDER=dev`), the `DevEmailProvider` stores emails in an in-memory inbox. Nothing is actually sent to SMTP. To inspect emails in dev:

```python
from trendstorm.infrastructure.email.dev_provider import _DEV_INBOX
# or if you have the provider instance:
provider.inbox_for("user@example.com")
provider.latest_for("user@example.com", "verify")
```

**Common mistake:** `EMAIL__PROVIDER=dev` left set in staging/production. Check:
```bash
kubectl exec -n trendstorm deploy/api -- python -c \
  "from trendstorm.shared.config import get_settings; print(get_settings().email.provider)"
```

---

## 4. Resend Flows

All time-limited tokens support resending. The resend invalidates the old token (by deleting the pending record) and creates a fresh one with a new expiry.

| Token type | Resend endpoint | Rate limit |
|---|---|---|
| Email verification | `POST /v1/auth/resend-verification` | 3/hr per user |
| Password reset | `POST /v1/auth/password-reset-request` | 5/hr per email, 10/hr per IP |
| Invite | `POST /v1/invites/{id}/resend` (admin) | No rate limit (admin action) |

If a user claims they didn't receive an email but the Postmark activity log shows it delivered, it's likely in their spam folder. Advise them to add the sending domain to their contacts.

---

## 5. Bounce and Complaint Handling

**Hard bounce** (invalid address): Postmark automatically suppresses the address. Future sends to that address return a 406 `InactiveRecipientError`. You must remove the address from the suppression list in Postmark before re-enabling.

**Spam complaint**: Postmark unsubscribes the recipient. Do not send marketing email from the transactional server. Keep transactional content (verify, reset, invite) clearly transactional.

**Monitoring**: Set up a Postmark webhook to post bounce/complaint events to an internal endpoint or Slack channel. Without this, bounces are only visible by checking the dashboard manually.

---

## 6. Link Expiry

| Email | Link expiry | Token TTL in Mongo |
|---|---|---|
| Verification | 24 hours | 24h (TTL index on `email_verifications.created_at`) |
| Password reset | 1 hour | 1h (TTL index on `password_resets.created_at`) |
| Invite | 7 days | 7 days (no TTL index; handled by `expires_at` check in service) |

If a user clicks an expired link, they get a `TokenExpiredError` (→ 401). The UI should direct them to request a new token via the resend flow.
