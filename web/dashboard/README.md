# TrendStorm Dashboard

Read-only operator dashboard + HITL review interface for TrendStorm AI.

## Quick start (local dev)

### Prerequisites
- Node.js 20+
- TrendStorm API running on `http://localhost:8080` (`make up && make up-app`)
- Auth0 application configured (see below)

### 1 — Install
```bash
cd web/dashboard
npm install
```

### 2 — Configure Auth0

Create a Single Page Application in your Auth0 tenant. Add the following allowed origins/callbacks:
```
Allowed Callback URLs:     http://localhost:5173
Allowed Logout URLs:       http://localhost:5173
Allowed Web Origins:       http://localhost:5173
```

Create an API in Auth0 with identifier `https://api.trendstorm.ai`.

Add an Auth0 Action on "Post Login" flow to inject custom claims:
```js
exports.onExecutePostLogin = async (event, api) => {
  api.accessToken.setCustomClaim("https://trendstorm.ai/roles", event.user.app_metadata.roles ?? []);
  api.accessToken.setCustomClaim("https://trendstorm.ai/tenants", event.user.app_metadata.tenants ?? []);
};
```

### 3 — Configure env
```bash
cp .env.example .env.local
# Fill in VITE_AUTH0_DOMAIN and VITE_AUTH0_CLIENT_ID
```

### 4 — Run
```bash
npm run dev
# → http://localhost:5173
```

### 5 — Test
```bash
npm test              # unit tests (Vitest)
npm run test:e2e      # Playwright E2E (requires preview server)
```

## Available commands

| Command | Description |
|---|---|
| `npm run dev` | Start dev server with HMR (port 5173) |
| `npm run build` | Production build → `dist/` |
| `npm run preview` | Serve production build locally |
| `npm run codegen` | Re-generate `src/api/types.generated.ts` from live API |
| `npm run codegen:check` | CI gate — fails if generated types diverge |
| `npm test` | Vitest unit tests |
| `npm run test:watch` | Vitest in watch mode |
| `npm run test:e2e` | Playwright E2E tests |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint |

## Roles

| Role | Access |
|---|---|
| (any authenticated user) | Categories, Jobs, Usage |
| `reviewer` | + Reviews queue and decision form |
| `admin` | + Audit log and chunk raw links |

Set roles via Auth0 user `app_metadata`:
```json
{ "roles": ["reviewer", "admin"], "tenants": [{ "tenant_id": "t_xxx", "name": "Acme Corp" }] }
```

## Architecture notes

See [docs/architecture-history/phase-15a-dashboard.md](../../docs/architecture-history/phase-15a-dashboard.md) for full design decisions (Vite vs Next.js, runtime config, custom SSE, Auth0 claims).

## Build + deploy

```bash
# Production build
npm run build

# Docker image
docker build -f ../../docker/Dockerfile.dashboard -t trendstorm/dashboard:$(git rev-parse --short HEAD) ../../

# Helm deploy
helm upgrade --install trendstorm-dashboard ../../helm/dashboard \
  --set runtimeConfig.auth0Domain=myapp.auth0.com \
  --set runtimeConfig.auth0ClientId=XXXX \
  --set image.tag=$(git rev-parse --short HEAD)
```
