# Runbook: Auth Incident

**Scope:** Self-service auth system (Phase 16) — signup failures, login outages, token revocation, session anomalies, GDPR purge failures.

---

## 1. Signup / Login Completely Down

**Symptoms:** `POST /v1/auth/signup` or `/v1/auth/login` returning 5xx. User-facing login page broken.

**Root causes (check in order):**

1. **Auth0 outage** — check [Auth0 Status](https://status.auth0.com/). If Auth0 is down, the IdP Protocol calls (`create_user`, `authenticate`) will fail. Mitigation: display a maintenance banner. There is no hot standby for Auth0.

2. **Mongo unavailable** — the Mongo transaction that creates User + Org + Membership fails. Check `make logs | grep "startup_failed"` or the Mongo pod health.

3. **Redis unavailable** — `SessionService.issue_session()` writes the refresh token to Redis. Check Redis connectivity. If Redis is down but Mongo is up, issue_session will fail after the Mongo transaction commits — the user is created but can't log in. Manual intervention: the user can try again once Redis recovers (refresh token issuance is idempotent at the user level).

4. **Misconfigured `JWT__SECRET`** — if the secret changed between deploys, old refresh tokens become invalid but new JWTs sign correctly. Watch for a spike in `AuthenticationError` on `/v1/auth/refresh`. Fix: rolling restart of all API replicas with the correct secret.

**Escalation:** If Auth0 is down for > 15 min, open a Priority 1 with Auth0 support. Tag @oncall-infra.

---

## 2. JWT Refresh Token Loop — Users Getting Logged Out

**Symptoms:** Users report being repeatedly logged out. `POST /v1/auth/refresh` returns 401 for valid sessions.

**Diagnosis:**
```bash
# Check Redis for the failing token hash
redis-cli get "rt:<sha256_of_refresh_token>"

# If key is missing, check Mongo for the RefreshSession document
mongosh trendstorm --eval "db.refresh_sessions.findOne({refresh_token_hash: '<hash>'})"
```

**Common causes:**

- **Redis eviction** — if Redis maxmemory policy is `allkeys-lru`, it will evict refresh token keys under memory pressure. Switch to `noeviction` or increase Redis memory. The Mongo record survives but the session can't be refreshed (design: Redis is the source of truth for active sessions).

- **Double-rotation race** — two concurrent requests using the same refresh token. The first call succeeds and rotates the token; the second call fails because the Redis key is already gone. This is correct behavior — tell the user to log in again.

- **Secret rotation** — if `JWT__SECRET` was rotated, the access token verification fails. The refresh token is unaffected (it's an opaque random value, not a JWT), but the new access JWT uses the new secret. If old access JWTs from before the rotation are still in use, they'll fail verification. Access tokens expire in 15 min, so this self-heals.

---

## 3. Email Not Delivered (Verification / Password Reset / Invite)

**Symptoms:** Users report not receiving verification, reset, or invite emails.

**Diagnosis:**
```bash
# Check Postmark delivery logs in the dashboard: https://account.postmark.com
# Or check the application logs for email failures:
grep "email_failed\|postmark" /var/log/trendstorm/api.log
```

**Common causes:**

- **Wrong `EMAIL__POSTMARK_SERVER_TOKEN`** — Postmark returns 401. Check `EMAIL__PROVIDER=postmark` is set and the token is correct.

- **Spam/deliverability** — check the recipient's spam folder. Postmark provides per-message delivery receipts. If bouncing: verify the `FROM` domain has SPF/DKIM records for the Postmark sending server.

- **`EMAIL__PROVIDER=dev` in production** — dev provider only logs to stdout; nothing is actually sent. Check `get_settings().email.provider`.

- **`EMAIL__APP_BASE_URL` wrong** — links in emails point to the wrong host. Fix the env var and redeploy.

**Note:** Email delivery failures are fire-and-forget in all services — the token is created and stored even if the email fails. Users can request a resend.

---

## 4. GDPR Purge Worker Not Running

**Symptoms:** Users who scheduled deletion 30+ days ago are not being hard-deleted. Mongo `users` collection accumulates tombstoned docs with `purge_at <= now`.

**Diagnosis:**
```bash
# Check if the sweeper pod is running
kubectl get pods -n trendstorm -l app=account-purge-worker

# Check logs for last sweep
kubectl logs -n trendstorm account-purge-worker-xxx | grep "account_purge_sweeper"

# Count pending purges
mongosh trendstorm --eval "db.users.countDocuments({deleted_at: {\$ne: null}, purge_at: {\$lte: new Date()}})"
```

**Recovery:**
1. If the pod is crashlooping, check for Mongo/Redis connectivity issues.
2. If the pod was OOMKilled, increase the memory limit in the Helm chart.
3. To manually trigger a sweep without waiting for the 1-hour interval: restart the pod (`kubectl rollout restart deployment account-purge-worker`). The sweeper runs immediately on startup.
4. The `execute_purge` method is idempotent — running it twice on the same user is safe. `hard_delete` is a no-op if the document is already gone; Auth0 `delete_user` returns 404 gracefully.

**Strategy:** Deploy `strategy: Recreate` — only one replica at a time. If you see two pods, one is terminating; wait for the old one to stop.

---

## 5. Leaked or Compromised Refresh Token

**Symptoms:** Suspicious login activity for a specific user. Token from an unexpected IP or user-agent.

**Immediate action — revoke all sessions for the user:**
```bash
# Via API (requires a service token or admin session):
# DELETE all sessions by revoking in Redis:
redis-cli keys "rt:*" | xargs redis-cli del  # ⚠️ LAST RESORT — revokes ALL users

# Better: revoke sessions for one specific user
# Get user_id from Mongo:
mongosh trendstorm --eval "db.users.findOne({email: 'victim@example.com'}, {_id: 0, id: 1})"
# Revoke all refresh sessions for that user:
mongosh trendstorm --eval "db.refresh_sessions.updateMany({user_id: '<id>', revoked_at: null}, {\$set: {revoked_at: new Date()}})"
# Delete Redis keys for that user's sessions:
mongosh trendstorm --eval "db.refresh_sessions.find({user_id: '<id>'}, {refresh_token_hash: 1})" | jq -r '.[].refresh_token_hash' | xargs -I{} redis-cli del "rt:{}"
```

**Notify the user** — send a security alert email explaining what happened and prompting a password reset.

**Post-incident:**
- Review `audit_log` collection for the affected user's activity.
- Check whether the leaked token was obtained via XSS (audit the dashboard CSP headers) or session fixation.

---

## 6. Signup Mode Misconfiguration

**Symptoms:** All signups returning `SignupNotAllowedError` (403), or signups open when they should be invite-only.

**Check:**
```bash
kubectl exec -n trendstorm deploy/api -- python -c "from trendstorm.shared.config import get_settings; s=get_settings(); print(s.signup.signup_mode)"
```

**Fix:** Update `SIGNUP__SIGNUP_MODE` in the ConfigMap and do a rolling restart of the API. No downtime required — the setting is read at startup (`get_settings()` is cached).

**Modes:**
- `open` — anyone can create an account
- `invite_only` — must have an invite token or an email matching `SIGNUP__ALLOWLIST_DOMAINS`
- `closed` — no new signups (maintenance windows, sunset)

---

## 7. Auth0 Management API Rate Limit

**Symptoms:** Password reset or email verification calls returning 429 from the Auth0 Management API. Affects `set_password` and `mark_email_verified` operations.

**Auth0 Management API rate limits:** 2 req/sec per endpoint by default. For bulk operations (e.g., a large account purge run) this can be hit.

**Mitigation:**
- The account purge sweeper processes at most `batch_size=50` users per sweep. If the Auth0 rate limit is hit during a purge batch, the failure is caught and logged; the user document is still hard-deleted from Mongo. The Auth0 account becomes an orphan — clean it up manually via the Auth0 dashboard or Management API.
- For password reset / email verification: these are user-driven, low-frequency operations. If they hit rate limits, it means unusually high volume — investigate for abuse (bot signup flood).
