import { useParams, Link, Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { reviewDetailOptions } from "@/api/queries/reviews";
import { jobDetailOptions } from "@/api/queries/jobs";
import { ReviewStatusBadge } from "@/components/shared/StatusBadge";
import { SlaCountdown } from "@/components/reviews/SlaCountdown";
import { DecisionForm } from "@/components/reviews/DecisionForm";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ArrowLeft } from "lucide-react";
import { formatDate, formatCurrency, formatRelative } from "@/lib/utils";
import { RoleGuard } from "@/auth/RoleGuard";

export default function ReviewDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: review, isLoading } = useQuery(reviewDetailOptions(id!));
  // Only fetch the job once the review has loaded and we have the job_id.
  const { data: job } = useQuery({
    ...jobDetailOptions(review?.job_id ?? ""),
    enabled: !!review?.job_id,
  });

  if (isLoading) return <Skeleton className="h-screen rounded-lg" />;
  if (!review) return <Navigate to="/reviews" replace />;

  const isPending = review.status === "pending";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3 flex-wrap">
        <Link to="/reviews" className="text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <h1 className="text-xl font-bold">Review</h1>
        <ReviewStatusBadge status={review.status} />
        {isPending && <SlaCountdown deadline={review.timeout_at} />}
      </div>

      {/* Metadata */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          {
            label: "Cost so far",
            value: formatCurrency((review.cost_usd_so_far_cents ?? 0) / 100),
          },
          {
            label: "Refinement loops",
            value: String(review.refinement_loops_used ?? 0),
          },
          {
            label: "Validator score",
            value:
              review.validator_score != null
                ? `${(review.validator_score * 100).toFixed(0)}%`
                : "—",
          },
          { label: "SLA deadline", value: formatDate(review.timeout_at) },
        ].map(({ label, value }) => (
          <Card key={label}>
            <CardHeader className="pb-1">
              <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">
                {label}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-lg font-bold">{value}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {review.flagging_reason && (
        <Card className="border-amber-300">
          <CardHeader>
            <CardTitle className="text-sm text-amber-700">Flagging reason</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm">{review.flagging_reason}</p>
          </CardContent>
        </Card>
      )}

      {/* Job pipeline link */}
      {job && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Associated Job</CardTitle>
          </CardHeader>
          <CardContent className="text-sm space-y-1">
            <p className="font-mono text-xs text-muted-foreground">{job.id}</p>
            <p>
              Status: <Badge variant="secondary">{job.status}</Badge>
            </p>
            <p className="text-xs text-muted-foreground">
              Created {formatRelative(job.created_at)}
            </p>
            <Link
              to={`/jobs/${job.id}`}
              className="text-xs text-primary hover:underline"
            >
              View pipeline →
            </Link>
          </CardContent>
        </Card>
      )}

      {/* Reviewer note (if resolved) */}
      {review.decision_comment && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Review Note</CardTitle>
          </CardHeader>
          <CardContent>
            <blockquote className="border-l-2 border-muted pl-3 text-sm text-muted-foreground">
              {review.decision_comment}
            </blockquote>
            {review.reviewer_id && (
              <p className="mt-2 text-xs text-muted-foreground">
                — {review.reviewer_id},{" "}
                {review.resolved_at ? formatRelative(review.resolved_at) : ""}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Decision form — reviewer role only */}
      {isPending && (
        <RoleGuard role="reviewer">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Make Decision</CardTitle>
            </CardHeader>
            <CardContent>
              <DecisionForm reviewId={review.id} jobId={review.job_id} />
            </CardContent>
          </Card>
        </RoleGuard>
      )}
    </div>
  );
}
