import { Badge } from "@/components/ui/badge";
import type { JobStatus, ReviewStatus } from "@/api/types.generated";

const JOB_STATUS_VARIANT: Record<JobStatus, "default" | "success" | "destructive" | "warning" | "info" | "muted" | "secondary" | "outline"> = {
  pending: "muted",
  ingesting: "info",
  ingested: "info",
  embedding: "info",
  embedded: "info",
  retrieving: "info",
  analyzing: "info",
  awaiting_review: "warning",
  publishing: "info",
  completed: "success",
  failed: "destructive",
  cancelled: "muted",
  rejected: "destructive",
};

const REVIEW_STATUS_VARIANT: Record<ReviewStatus, "default" | "success" | "destructive" | "warning" | "info" | "muted" | "secondary" | "outline"> = {
  pending: "warning",
  approved: "success",
  rejected: "destructive",
  refinement_requested: "info",
  timed_out: "muted",
};

export function JobStatusBadge({ status }: { status: JobStatus }) {
  return (
    <Badge variant={JOB_STATUS_VARIANT[status]}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

export function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  return (
    <Badge variant={REVIEW_STATUS_VARIANT[status]}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}
