/**
 * AUTO-GENERATED — run `npm run codegen` to regenerate.
 * Source: GET /v1/openapi.json
 *
 * Manual baseline committed for CI gate. Do not edit by hand.
 * Last synced: Phase 15.6 schema unification.
 */

// ─── Enums (mirrored from trendstorm-shared) ──────────────────────────────────

export type JobStatus =
  | "pending"
  | "ingesting"
  | "embedding"
  | "retrieving"
  | "analyzing"
  | "awaiting_review"
  | "publishing"
  | "memory_consolidation"
  | "completed"
  | "failed"
  | "cancelled"
  | "rejected";

export type SourceType = "http" | "rss" | "api" | "sitemap";

export type ReportFormat = "markdown" | "pdf" | "json";

export type ReviewStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "refinement_requested"
  | "timed_out";

export type ReviewDecision = "approve" | "reject" | "request_refinement";

export type FlaggingReason =
  | "always_mode"
  | "low_validator_score"
  | "refinement_budget_exhausted"
  | "cost_threshold_exceeded";

export type StreamEventType =
  | "stage_started"
  | "stage_completed"
  | "stage_failed"
  | "progress"
  | "partial_text"
  | "citation_added"
  | "report_ready"
  | "job_failed"
  | "job_rejected"
  | "review_required"
  | "review_resolved"
  | "heartbeat";

// ─── Core models ──────────────────────────────────────────────────────────────

export interface Category {
  id: string;
  name: string;
  description: string | null;
  keywords: string[];
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface CategoryList {
  categories: Category[];
  next_cursor: string | null;
}

export interface Source {
  id: string;
  category_id: string;
  url: string;
  label: string | null;
  type: SourceType;
  enabled: boolean;
  last_fetch_at: string | null;
  last_fetch_status: string | null;
  last_fetch_error: string | null;
  created_at: string;
}

export interface SourceList {
  sources: Source[];
}

export interface JobMetrics {
  documents_ingested: number;
  chunks_created: number;
  chunks_retrieved: number;
  llm_input_tokens: number;
  llm_output_tokens: number;
  duration_seconds: number | null;
}

export interface Job {
  id: string;
  status: JobStatus;
  category_id: string;
  source_ids: string[];
  note: string | null;
  analysis_id: string | null;
  report_id: string | null;
  metrics: JobMetrics;
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  stream_url: string | null;
}

export interface JobList {
  jobs: Job[];
  next_cursor: string | null;
}

export interface JobAccepted {
  job_id: string;
  status: JobStatus;
  stream_url: string;
  created_at: string;
}

export interface Review {
  id: string;
  job_id: string;
  analysis_id: string;
  stage_under_review: string;
  status: ReviewStatus;
  /** Principal who resolved the review (API key ID or JWT subject). */
  reviewer_id: string | null;
  /** Comment from the reviewer (required for request_refinement). */
  decision_comment: string | null;
  created_at: string;
  resolved_at: string | null;
  /** Absolute UTC deadline — SLA window for this review. */
  timeout_at: string;
  sla_seconds: number;
  // Fields added Phase 15.6 — populated by review_gate_node
  validator_score: number | null;
  refinement_loops_used: number;
  /** Integer cents to avoid float decimal issues. */
  cost_usd_so_far_cents: number;
  flagging_reason: FlaggingReason | null;
}

export interface ReviewList {
  // Reviews router returns array directly (no envelope)
  items: Review[];
}

export interface Analysis {
  id: string;
  job_id: string;
  summary: string;
  validator_score: number;
  validator_passed: boolean;
  refinement_loops: number;
  created_at: string;
}

export interface Memory {
  id: string;
  tenant_id: string;
  category_id: string;
  kind: string;
  source: string;
  content: string;
  confidence: number;
  is_active: boolean;
  tags: string[];
  superseded_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryList {
  items: Memory[];
  total: number;
}


export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  tenant_id: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  is_active: boolean;
}

export interface ApiKeyCreated {
  id: string;
  name: string;
  /** Plaintext key — shown once on creation, never returned again. */
  key: string;
  key_prefix: string;
  tenant_id: string;
  created_at: string;
}

export interface ApiKeyList {
  keys: ApiKey[];
}

export interface AuditLogEntry {
  id: string;
  tenant_id: string;
  event_type: "ssrf_blocked" | "url_blocked" | "pii_detected" | string;
  actor: string;
  resource_type: string;
  resource_id: string;
  action: string;
  outcome: string;
  metadata: Record<string, unknown>;
  created_at: string;
  trace_id: string | null;
  correlation_id: string | null;
}

export interface AuditLogList {
  items: AuditLogEntry[];
  next_cursor: string | null;
}

export interface StreamEvent {
  event_id: string;
  job_id: string;
  tenant_id: string;
  event_type: StreamEventType;
  seq: number;
  stage: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
}

// ─── Request bodies ───────────────────────────────────────────────────────────

export interface CreateJobBody {
  category_id: string;
  source_ids?: string[];
  note?: string | null;
}

export interface CreateCategoryBody {
  name: string;
  description?: string | null;
  keywords?: string[];
}

export interface UpdateCategoryBody {
  description?: string | null;
  keywords?: string | null;
  archived?: boolean | null;
}

export interface RegisterSourceBody {
  category_id: string;
  url: string;
  label?: string | null;
  type?: SourceType;
}

export interface ResolveReviewBody {
  decision: ReviewDecision;
  comment?: string | null;
}

export interface CreateMemoryBody {
  content: string;
  confidence?: number;
  tags?: string[];
  curated_by: string;
}

export interface CreateApiKeyBody {
  name: string;
}

// ─── Utility types ────────────────────────────────────────────────────────────

/** Generic cursor-paginated response envelope. */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

/** Quota / billing status returned by GET /v1/billing/quota. */
export interface QuotaUsage {
  allowed: boolean;
  monthly_spend_usd: number;
  monthly_limit_usd: number;
  jobs_this_month: number;
  jobs_limit: number;
  reason: string | null;
}

/** A source citation within an analysis. */
export interface Citation {
  chunk_id: string;
  source_url: string;
  excerpt: string;
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
