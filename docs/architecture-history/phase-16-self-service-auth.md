# Phase 16 — Self-service Auth & Registration

## Goals

Phase 16 replaced the placeholder auth model (API keys only, tenant IDs in headers) with a complete self-service identity system:
1. Users can sign up, verify their email, reset their password, and accept invitations without operator intervention.
2. Sessions use short-lived JWTs (15 min) and server-side refresh tokens (30 days) instead of long-lived API keys.
3. GDPR compliance: accounts can be scheduled for deletion (30-day grace) and are hard-deleted by a background sweeper.

---

## A. Domain Model

Six new domain model files, all in `src/trendstorm/domain/`:

| File | Model | Notes |
|---|---|---|
| `users/models.py` | `User` | Root entity — no `tenant_id`. Fields: email (lowercase), `deleted_at`/`purge_at` tombstone. |
| `organizations/models.py` | `Organization` | `id == tenant_id`. Uses `Collection.TENANTS` to avoid a data migration from the earlier `Tenant` model. |
| `memberships/models.py` | `Membership`, `Role` | Join table between User and Organization. Roles: OWNER, ADMIN, MEMBER, REVIEWER, VIEWER. |
| `invites/models.py` | `Invite` | 7-day token; token_hash stored, plaintext in email. |
| `email_verifications/models.py` | `EmailVerification` | 24-hour single-use token. |
| `password_resets/models.py` | `PasswordReset` | 1-hour single-use token. |
| `sessions/models.py` | `RefreshSession` | Links refresh token hash to (user_id, tenant_id). |

### Key decision: User is root-level, not tenant-scoped

`MongoUserRepository` does NOT subclass `TenantScopedRepository` and does NOT call `_tenant_query()`. This is the documented exception — users exist across all tenants. `users__email_unique` enforces global uniqueness (one account per email address, regardless of how many organizations the user belongs to).

### Key decision: Organization.id == tenant_id

The `Organization` class uses `Collection.TENANTS` (the Mongo collection name stays "tenants") to avoid a data migration from the earlier `Tenant` model. `Organization.tenant_id` is a read-only property that returns `self.id`. The old `Tenant` model in `domain/auth/models.py` is kept for backward compatibility.

### Token pattern (same as API keys)

All short-lived tokens follow the same pattern: `secrets.token_urlsafe(32)` → SHA-256 hex stored in Mongo, plaintext sent in the email link. Helper functions live in `services/auth/token_utils.py`. Nothing plaintext ever persists to the database.

---

## B. Infrastructure

### IdentityProvider Protocol (`infrastructure/auth/identity_provider.py`)

Auth0 is the implementation (`Auth0Provider`). The Protocol has 7 methods:
- `create_user(email, password)` → called at signup for email/password users
- `authenticate(email, password)` → called at login; returns an `ExternalUser` with the Auth0 `sub` claim
- `set_password(subject, new_password)` → called when consuming a password reset token
- `mark_email_verified(subject)` → called when consuming an email verification token
- `delete_user(subject)` → called during GDPR hard-delete (best-effort; failure logged)
- `get_oauth_authorize_url(provider, state, redirect_uri)` → OAuth2 redirect
- `exchange_oauth_code(provider, code, state, redirect_uri)` → OAuth2 callback

Auth0 owns password hashing. Our DB never sees a password hash.

### EmailProvider Protocol (`infrastructure/email/email_provider.py`)

Two implementations:
- `PostmarkProvider` — maps logical template names to Postmark aliases; used in production.
- `DevEmailProvider` — in-memory inbox dict; used in development and unit tests. Queryable via `inbox_for(email)` and `latest_for(email, template)`.

---

## C. Services (`services/auth/`)

### RegistrationService

Entry points: `create_account(email, password, *, invite_token, ip)` and `create_account_from_oauth(external, *, invite_token, ip)`.

**Critical ordering: IdP OUTSIDE the Mongo transaction.**

1. `IdentityProvider.create_user()` is called first — before the Mongo session opens.
2. The Mongo transaction creates User + Org + Membership (or accepts an invite + creates Membership).
3. If the transaction fails, a best-effort `IdentityProvider.delete_user()` cleans up the Auth0 account.

Why this ordering? The IdP call is not transactional. If it were inside the Mongo `start_transaction()` block and the Mongo commit failed after the IdP returned successfully, we'd leave an orphaned Auth0 account with no matching User document. The current ordering means the only orphan case is IdP success + Mongo failure — which the compensating delete handles.

### SessionService

Issues HS256 JWTs (15-min TTL) from our own `JWTSettings.secret`. Refresh tokens are stored in two places:
- **Redis** (`rt:{token_hash}`) — primary lookup for `refresh_session()` and `revoke_session()`. O(1) delete on revoke.
- **Mongo** (`refresh_sessions` collection) — audit trail; backs "list my active sessions" in the future dashboard settings page.

Token rotation: every `refresh_session()` call atomically deletes the old Redis key + stamps `revoked_at` in Mongo, then issues a completely new token pair. The refresh cookie is replaced in the HTTP response.

### InvitationService

Creates invites, validates them (expiry, pending state), and accepts them for both new users (via RegistrationService) and existing users (`accept_existing_user`). Resending revokes the old invite and creates a fresh one (resets the 7-day expiry).

### PasswordResetService

Rate-limiting uses a single Redis pipeline (4 commands: INCR + EXPIRE for email key, INCR + EXPIRE for IP key). Both limits checked atomically before any Mongo lookup. Silent on unknown email addresses (prevents email enumeration).

### EmailVerificationService

Rate-limited at 3 resend requests/hour per user. Consuming a token calls `IdentityProvider.mark_email_verified()` as a best-effort step — if the IdP call fails, the user's `email_verified` flag is still set in our DB (the failure is logged, not re-raised).

### AccountDeletionService

Three-stage lifecycle:
1. `schedule_deletion` — sets `deleted_at` + `purge_at = now + 30d`, revokes all sessions, sends confirmation email.
2. `cancel_deletion` — clears tombstone within the 30-day window.
3. `execute_purge` — called only by `account_purge_worker`. Handles sole-owner edge case: if the user is the registered owner of an org and another admin exists, ownership is transferred; if no admins exist, the org is marked orphaned (`owner_user_id` left as the purged user's id but `deleted_at` set). The org is never deleted — it may have billing history, jobs, and categories.

---

## D. API Routers

Five new routers wired into `api/main.py`:

| Router | Prefix | Notable endpoints |
|---|---|---|
| `auth.py` | `/v1/auth` | signup, login, logout, refresh, verify-email, resend-verification, password-reset-request, password-reset-confirm, oauth/{provider}/start+callback |
| `users.py` | `/v1/users/me` | GET/PATCH/DELETE profile, POST restore, GET sessions, DELETE session |
| `organizations.py` | `/v1/organizations` | POST (create), GET current, PATCH current, POST switch (re-issue JWT for new tenant) |
| `memberships.py` | `/v1/memberships` | GET list, PATCH roles (admin), DELETE member |
| `invites.py` | `/v1/invites` | POST create, GET list, DELETE revoke, POST resend; public: GET by-token, POST by-token/accept |

The refresh token travels as an `httponly; secure; samesite=lax` cookie named `ts_refresh` (max-age 30 days). The access JWT is returned in the JSON body.

---

## E. Background Worker: account_purge_worker

Located at `orchestration/workers/account_purge_worker.py`. Runs as a polling loop (not Kafka-driven):
- Default interval: 3600s (1 hour).
- Queries `list_due_for_purge()` — a cross-tenant Mongo query (the fourth documented exception to the `_tenant_query()` rule).
- Calls `AccountDeletionService.execute_purge(user)` for each result. Idempotent — `hard_delete` is a no-op if the document is already gone.
- Deploys as `strategy: Recreate` (single replica). Two replicas would double-log and double-call IdP delete, but would not corrupt data.

---

## F. Index Changes

New collections added to `INDEXES` in `infrastructure/mongo/indexes.py`:

| Collection | Notable indexes |
|---|---|
| `users` | `users__email_unique` (global unique, case-insensitive), `users__idp_subject` (global unique sparse) |
| `memberships` | `memberships__tenant_user_unique` (tenant_id + user_id unique compound) |
| `invites` | `invites__token_hash_unique` (global unique), `invites__tenant_email_pending_unique` (partial unique for pending-only constraint) |
| `email_verifications` | `email_verifications__token_hash_unique` (global unique), TTL 7 days |
| `password_resets` | `password_resets__token_hash_unique` (global unique), TTL 2 hours |
| `refresh_sessions` | `refresh_sessions__token_hash_unique` (global unique), TTL 7 days post-expiry |

The `GLOBAL_OK` set in `test_indexes.py` was extended to include `USERS`, `INVITES`, `EMAIL_VERIFICATIONS`, `PASSWORD_RESETS`, and `REFRESH_SESSIONS` — all have intentionally global unique indexes because token-hash lookups happen before the tenant is known.

---

## G. Test Coverage

51 new unit tests (no Docker required):

| File | Tests | What's covered |
|---|---|---|
| `test_registration_service.py` | 6 | open/invite_only/closed modes, invalid invite, welcome email, email normalisation |
| `test_session_service.py` | 5 | JWT+refresh issued, token rotation, revoke, invalid token, expired JWT |
| `test_invitation_service.py` | 12 | invite creation, email normalisation, conflict, preview, expired/revoked, accept, resend, revoke |
| `test_password_reset_service.py` | 10 | sends email, silent on unknown, case normalise, rate limit by email/IP, consume happy, invalid/consumed/expired, deletes old pending |
| `test_email_verification_service.py` | 9 | sends email, no-op if verified, rate limit, deletes existing, consume happy, invalid/consumed/expired, IdP failure non-fatal |
| `test_account_deletion_service.py` | 9 | schedule + cancel tombstone, execute_purge with transfer/orphan/non-owner, IdP failure non-fatal, no IdP subject |

All fakes use in-memory dicts and avoid any I/O. The `_FakeMongo` exposes a `client.start_session()` async context manager that no-ops through transactions.

---

## H. Non-obvious Decisions

**Why server-side refresh tokens instead of long-lived JWTs?**
Long-lived JWTs cannot be revoked without a blocklist (which is effectively server-side state anyway). Server-side tokens give us O(1) revocation (one Redis DEL) and an audit trail of active sessions. The JWT expiry window (15 min) limits blast radius if a token leaks in transit.

**Why Redis AND Mongo for refresh tokens?**
Redis alone: no durable audit trail. Mongo alone: O(n) revocation scan. Dual-store gives O(1) revocation (Redis) + "list my active sessions" capability (Mongo) at the cost of a second write per issuance. This is acceptable — token issuance is infrequent compared to reads.

**Why not Auth0 password reset emails?**
Auth0's built-in reset email uses Auth0's own templates and redirect URIs. We need our own branded email (Postmark) and a redirect to our dashboard SPA, not auth0.com. Our service calls `IdentityProvider.set_password()` on the Auth0 Management API after validating the token on our side.

**Why is the `Organization` class still using `Collection.TENANTS`?**
Renaming the Mongo collection would require a migration script, a coordinated deploy (old code can't read new collection name), and index recreation. The Python class is renamed (`Organization` instead of `Tenant`) as a code-quality improvement; the wire name is left unchanged.
