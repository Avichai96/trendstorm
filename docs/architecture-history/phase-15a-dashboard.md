# Phase 15a — Read-Only Dashboard + HITL Review UI

## What was built

A production-grade single-page application at `web/dashboard/` serving as the primary operator interface for TrendStorm AI. The dashboard is **read-only for all data except HITL reviews** — category/source creation and API key management are deferred to Phase 15b.

### Frontend stack

| Layer | Technology | Rationale |
|---|---|---|
| Bundler | Vite 5 | Near-instant HMR, native ES modules, first-class TypeScript |
| UI framework | React 18 + TypeScript strict | Ecosystem size; strict mode catches concurrent-mode bugs early |
| Routing | React Router 6 | Code-split lazy pages; type-safe `useParams` |
| Server state | TanStack Query v5 | Cursor-based infinite scroll, polling intervals, background refetch |
| Auth | Auth0 React SDK | OAuth2 universal login; roles from JWT claims |
| Styling | Tailwind v3 + shadcn/ui | Design tokens via CSS vars; full component ownership (no version lock-in) |
| Charts | Recharts | Composable; works with Tailwind; no D3 knowledge required |
| Markdown | react-markdown + remark-gfm | SSR-safe; GFM tables in reports |
| SSE | Custom fetch-based `SSEConnection` | EventSource doesn't support `Authorization:` headers |
| Testing | Vitest + RTL + Playwright + axe | See below |

## Key architectural decisions

### Why Vite over Next.js

Next.js would add SSR complexity for what is fundamentally an auth-gated SPA. All pages require authentication, so there is no meaningful SEO benefit. Server components would add latency to what should be a pure CDN-served artifact. Vite produces a static bundle that deploys to S3 + CloudFront in under 60 seconds and is trivially revertable via CDN invalidation.

### Runtime config.json, not build-time env vars

The Helm chart injects `config.json` via a ConfigMap mount at `/config.json`. The app fetches this on first load (`loadConfig()` in `api/client.ts`). This means:
- The same Docker image can be deployed to staging and production with different Auth0 clients.
- Auth0 client IDs are not baked into the bundle (which is visible in browser source).
- Config changes only require a pod rollout, not a full rebuild.

In dev, `VITE_*` env vars serve as fallback when `/config.json` returns 404.

### Custom SSE over native EventSource

The native `EventSource` API does not support custom request headers. TrendStorm's `/v1/jobs/:id/stream` endpoint requires `Authorization: Bearer <token>` and `X-Tenant-ID`. The solution is `src/lib/sse.ts` — a `fetch()` + `ReadableStream` based SSE client that:
- Accepts an arbitrary `headers` map.
- Handles multi-line data accumulation and comment heartbeats.
- Reconnects with `Last-Event-ID` forwarded as a request header.
- Uses `sessionStorage` to persist `Last-Event-ID` across page reloads.
- Applies a per-line heartbeat timeout (`asyncio.wait_for` equivalent in JS via `Promise.race`).

### Role-based access via JWT claims

Auth0 is configured to inject a custom `https://trendstorm.ai/roles` claim into the JWT access token via an Auth0 Action. The `RoleGuard` component reads this claim. Roles used:
- `reviewer` — access to `/reviews` and the `DecisionForm` write path.
- `admin` — access to `/audit` and blob "view raw" links in the citation panel.

Multi-tenant users carry a `https://trendstorm.ai/tenants` claim (array of `{tenant_id, name}`). The `TenantSelector` component lets them switch tenants; switching clears the TanStack Query cache and updates `sessionStorage["tenant_id"]` which the `api/client.ts` attaches as `X-Tenant-ID`.

### Review resolve is the only write path

`DecisionForm.tsx` is the only component that calls a mutating API (`POST /v1/reviews/:id/resolve`). The server implements this via an outbox pattern (rule 90 in CLAUDE.md) — the UI just sends the resolve body and the server handles the Kafka handoff. The three decision types (approve / reject / request_refinement) use a shared confirmation dialog pattern with a comment box. `request_refinement` requires a non-empty comment; the confirm button is disabled until filled.

### TanStack Query polling strategy

- Job detail (`jobDetailOptions`): polls every 5 seconds while the job is non-terminal; stops polling once status is `completed | failed | cancelled | rejected`.
- Reviews list: polls every 30 seconds (SLA urgency display needs to stay fresh).
- Quota: polls every 60 seconds with a 30-second stale time.
- Categories, sources, audit: no automatic polling (user-triggered refetch via stale-time).

### Citation expansion

Citations are rendered as superscript numbers in `CitationPanel.tsx`. The panel renders a two-column layout: an index column of numbered buttons, and a detail panel showing the excerpt and source URL. Admins see the `chunk_id` for debugging.

### Accessibility

All component tests run axe-core checks. The Playwright E2E spec runs `@axe-core/playwright` on every critical page. shadcn/ui primitives inherit Radix-UI's ARIA semantics, which are WCAG 2.1 AA compliant.

## Directory layout

```
web/dashboard/
├── src/
│   ├── api/
│   │   ├── client.ts            — fetch wrapper; error hierarchy; token provider
│   │   ├── types.generated.ts   — committed baseline from codegen
│   │   └── queries/             — TanStack Query options factories (one file per resource)
│   ├── auth/
│   │   ├── AuthGuard.tsx        — redirects to Auth0 universal login
│   │   └── RoleGuard.tsx        — renders children only if roles claim matches
│   ├── components/
│   │   ├── ui/                  — shadcn/ui primitives (Button, Badge, Card, Dialog, …)
│   │   ├── layout/              — AppShell, Sidebar, TenantSelector
│   │   ├── jobs/                — PipelineProgress
│   │   ├── reviews/             — SlaCountdown, DecisionForm
│   │   ├── reports/             — MarkdownViewer, CitationPanel
│   │   └── shared/              — StatusBadge, ErrorBoundary, LoadMoreButton
│   ├── hooks/
│   │   ├── useSSE.ts            — wraps SSEConnection; handles auth token + reconnect
│   │   └── useTenant.ts         — multi-tenant state in sessionStorage
│   ├── lib/
│   │   ├── utils.ts             — cn(), formatDate, formatCurrency, slaUrgency
│   │   └── sse.ts               — fetch-based SSE client
│   └── pages/                   — one file per route (lazy-loaded)
└── tests/
    ├── unit/                    — Vitest + RTL (component + a11y)
    └── e2e/                     — Playwright (login, jobs, reviews)
```

## Deployment

The dashboard is a static SPA served via nginx. Two deployment targets:

**Kubernetes (Helm chart at `helm/dashboard/`):**
- nginx Deployment with 2 replicas.
- `config.json` injected via ConfigMap mount at `/etc/dashboard/config.json`.
- `nginx.conf` ConfigMap sets SPA fallback (`try_files $uri /index.html`) and long-lived cache for hashed assets.
- Pod checksum annotation forces rollout on ConfigMap change.

**Local Docker:**
```bash
docker build -f docker/Dockerfile.dashboard -t trendstorm/dashboard:dev .
docker run -p 3001:80 trendstorm/dashboard:dev
```

## CI (`dashboard-ci.yml`)

1. TypeScript type-check + ESLint
2. Vitest unit tests
3. Codegen gate (on `main` push only — requires running API)
4. Production build artifact upload
5. Helm lint for `helm/dashboard/`

E2E Playwright tests run via `make dashboard-test-e2e` against a locally started preview server; they skip when `PLAYWRIGHT_AUTH_TOKEN` is absent.
