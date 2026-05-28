/**
 * AUTO-GENERATED — run `npm run codegen` to regenerate.
 * Source: GET /v1/openapi.json
 *
 * Manual baseline committed for CI gate. Do not edit by hand.
 */

// ─── Enums (mirrored from trendstorm-shared) ──────────────────────────────────

export type JobStatus =
  | "pending"
  | "ingesting"
  | "ingested"
  | "embedding"
  | "embedded"
  | "retrieving"
  | "analyzing"
  | "awaiting_review"
  | "publishing"
  | "completed"
  | "failed"
  | "cancelled"
  | "rejected";

export type SourceType = "rss" | "web" | "json_api" | "sitemap";

export type ReportFormat = "markdown" | "pdf" | "json";

export type ReviewStatus = "pending" | "approved" | "rejected" | "refinement_requested" | "timed_out";

export type ReviewDecision = "approve" | "reject" | "request_refinement";

export type StreamEventType =
  | "stage_changed"
  | "chunk_delta"
  | "report_ready"
  | "job_failed"
  | "job_rejected"
  | "heartbeat";

// ─── Core models ──────────────────────────────────────────────────────────────

export interface Category {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  keywords: string[];
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface Source {
  id: string;
  tenant_id: string;
  category_id: string;
  url: string;
  label: string | null;
  type: SourceType;
  enabled: boolean;
  last_fetch_status: "ok" | "error" | "pending" | null;
  last_fetched_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Job {
  id: string;
  job_id: string;
  tenant_id: string;
  category_id: string;
  source_ids: string[];
  status: JobStatus;
  stage: string;
  refinement_loops_used: number;
  cost_usd: number;
  report_id: string | null;
  report_url: string | null;
  pdf_report_url: string | null;
  json_report_url: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface Review {
  id: string;
  tenant_id: string;
  job_id: string;
  analysis_id: string;
  status: ReviewStatus;
  flagging_reason: string | null;
  validator_score: number | null;
  refinement_loops_used: number;
  cost_usd_so_far: number;
  sla_deadline: string;
  review_note: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Chunk {
  id: string;
  chunk_id: string;
  document_id: string;
  source_id: string;
  source_url: string;
  text: string;
  created_at: string;
}

export interface Analysis {
  id: string;
  job_id: string;
  insights: Insight[];
  summary: string;
  citations: Citation[];
  validator_score: number | null;
  refinement_loop: number;
  created_at: string;
}

export interface Insight {
  id: string;
  headline: string;
  detail: string;
  supporting_chunk_ids: string[];
}

export interface Citation {
  chunk_id: string;
  excerpt: string;
  source_url: string;
}

export interface QuotaUsage {
  tenant_id: string;
  period_start: string;
  period_end: string;
  current_usd: number;
  soft_cap_usd: number;
  hard_cap_usd: number;
  soft_cap_reached: boolean;
  hard_cap_reached: boolean;
  daily_breakdown: DailySpend[];
  by_stage: Record<string, number>;
  by_provider: Record<string, number>;
}

export interface DailySpend {
  date: string;
  usd: number;
}

export interface ApiKey {
  id: string;
  tenant_id: string;
  name: string;
  key_prefix: string;
  roles: string[];
  created_at: string;
  last_used_at: string | null;
}

export interface AuditLogEntry {
  id: string;
  tenant_id: string;
  event_type: "ssrf_blocked" | "url_blocked" | "pii_detected" | string;
  actor: string | null;
  resource_type: string | null;
  resource_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface StreamEvent {
  seq: number;
  event_type: StreamEventType;
  job_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

// ─── Paginated response envelope ─────────────────────────────────────────────

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total: number | null;
}

// ─── Request bodies ───────────────────────────────────────────────────────────

export interface ResolveReviewBody {
  decision: ReviewDecision;
  comment: string | null;
}

export interface CreateJobBody {
  category_id: string;
  source_ids: string[];
  note?: string | null;
}

// ─── Error envelope ───────────────────────────────────────────────────────────

export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    context?: Record<string, unknown>;
  };
  correlation_id: string;
}
