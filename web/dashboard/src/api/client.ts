import type { ApiErrorEnvelope } from "./types.generated";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly correlationId: string,
    public readonly context?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class NotFoundError extends ApiError {}
export class UnauthorizedError extends ApiError {}
export class ForbiddenError extends ApiError {}
export class RateLimitedError extends ApiError {
  constructor(
    status: number,
    code: string,
    message: string,
    correlationId: string,
    public readonly retryAfter: number | null,
    context?: Record<string, unknown>,
  ) {
    super(status, code, message, correlationId, context);
    this.name = "RateLimitedError";
  }
}
export class ServerError extends ApiError {}

interface RuntimeConfig {
  apiBaseUrl: string;
  auth0Domain: string;
  auth0ClientId: string;
  auth0Audience: string;
}

let _config: RuntimeConfig | null = null;

export async function loadConfig(): Promise<RuntimeConfig> {
  if (_config) return _config;
  // In production: fetch /config.json (set at deploy time via CDN headers).
  // In dev: fall back to Vite env vars.
  try {
    const res = await fetch("/config.json");
    if (res.ok) {
      _config = await res.json();
      return _config!;
    }
  } catch {
    // dev fallback
  }
  _config = {
    apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8080",
    auth0Domain: import.meta.env.VITE_AUTH0_DOMAIN ?? "",
    auth0ClientId: import.meta.env.VITE_AUTH0_CLIENT_ID ?? "",
    auth0Audience: import.meta.env.VITE_AUTH0_AUDIENCE ?? "https://api.trendstorm.ai",
  };
  return _config;
}

export function getConfig(): RuntimeConfig {
  if (!_config) throw new Error("Config not loaded. Call loadConfig() first.");
  return _config;
}

let _getToken: (() => Promise<string>) | null = null;

export function setTokenProvider(fn: () => Promise<string>) {
  _getToken = fn;
}

async function authHeaders(): Promise<Record<string, string>> {
  if (!_getToken) return {};
  const token = await _getToken();
  return { Authorization: `Bearer ${token}` };
}

async function request<T>(
  method: string,
  path: string,
  opts: { params?: Record<string, string | number | boolean | undefined | null>; body?: unknown; tenantId?: string | null } = {},
): Promise<T> {
  const { apiBaseUrl } = getConfig();
  const url = new URL(`${apiBaseUrl}${path}`);

  if (opts.params) {
    for (const [k, v] of Object.entries(opts.params)) {
      if (v != null && v !== "") url.searchParams.set(k, String(v));
    }
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(await authHeaders()),
  };

  // Tenant header — required by the server's TenantMiddleware.
  // In JWT mode the server can derive tenant from the token; still send it
  // so Swagger proxy paths work during local dev.
  const tenantId = opts.tenantId ?? sessionStorage.getItem("tenant_id");
  if (tenantId) headers["X-Tenant-ID"] = tenantId;

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: opts.body != null ? JSON.stringify(opts.body) : undefined,
  });

  if (res.status === 204) return undefined as unknown as T;

  let data: unknown = {};
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    data = await res.json().catch(() => ({}));
  }

  if (!res.ok) {
    const envelope = data as Partial<ApiErrorEnvelope>;
    const code = envelope.error?.code ?? "unknown";
    const message =
      envelope.error?.message ?? (res.statusText || `HTTP ${res.status} error`);
    const correlationId = envelope.correlation_id ?? "";
    const context = envelope.error?.context;

    switch (res.status) {
      case 401:
        throw new UnauthorizedError(res.status, code, message, correlationId, context);
      case 403:
        throw new ForbiddenError(res.status, code, message, correlationId, context);
      case 404:
        throw new NotFoundError(res.status, code, message, correlationId, context);
      case 429:
        throw new RateLimitedError(
          res.status, code, message, correlationId,
          res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : null,
          context,
        );
      default:
        if (res.status >= 500) throw new ServerError(res.status, code, message, correlationId, context);
        throw new ApiError(res.status, code, message, correlationId, context);
    }
  }

  return data as T;
}

export const api = {
  get: <T>(path: string, params?: Record<string, string | number | boolean | undefined | null>, tenantId?: string | null) =>
    request<T>("GET", path, { params, tenantId }),

  post: <T>(path: string, body?: unknown, tenantId?: string | null) =>
    request<T>("POST", path, { body, tenantId }),

  patch: <T>(path: string, body?: unknown, tenantId?: string | null) =>
    request<T>("PATCH", path, { body, tenantId }),

  delete: <T>(path: string, tenantId?: string | null) =>
    request<T>("DELETE", path, { tenantId }),

  streamUrl: (path: string): string => {
    const { apiBaseUrl } = getConfig();
    return `${apiBaseUrl}${path}`;
  },
};
