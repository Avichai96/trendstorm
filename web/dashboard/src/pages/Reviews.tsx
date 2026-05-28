import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { reviewsListOptions } from "@/api/queries/reviews";
import { ReviewStatusBadge } from "@/components/shared/StatusBadge";
import { SlaCountdown } from "@/components/reviews/SlaCountdown";
import { LoadMoreButton } from "@/components/shared/CursorPagination";
import { Skeleton } from "@/components/ui/skeleton";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { formatRelative, formatCurrency } from "@/lib/utils";
import type { ReviewStatus } from "@/api/types.generated";
import { ClipboardList } from "lucide-react";

const ALL = "__all__";

const STATUS_OPTIONS: { value: ReviewStatus | typeof ALL; label: string }[] = [
  { value: ALL, label: "All statuses" },
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "refinement_requested", label: "Refinement requested" },
  { value: "timed_out", label: "Timed out" },
];

export default function Reviews() {
  const [status, setStatus] = useState<ReviewStatus | typeof ALL>(ALL);
  const { data, isLoading, hasNextPage, isFetchingNextPage, fetchNextPage } = useInfiniteQuery(
    reviewsListOptions(status === ALL ? undefined : status),
  );

  const reviews = data?.pages.flatMap((p) => p.items) ?? [];
  const pendingCount = reviews.filter((r) => r.status === "pending").length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">Reviews</h1>
          {pendingCount > 0 && (
            <Badge variant="warning">{pendingCount} pending</Badge>
          )}
        </div>
        <Select value={status} onValueChange={(v) => setStatus(v as ReviewStatus | typeof ALL)}>
          <SelectTrigger className="w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-20 rounded-lg" />
          ))}
        </div>
      ) : (
        <>
          <div className="divide-y rounded-lg border bg-card">
            {reviews.map((review) => (
              <Link
                key={review.id}
                to={`/reviews/${review.id}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-accent"
              >
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    <ReviewStatusBadge status={review.status} />
                    {review.status === "pending" && (
                      <SlaCountdown deadline={review.sla_deadline} />
                    )}
                    {review.flagging_reason && (
                      <Badge variant="outline" className="text-xs">{review.flagging_reason}</Badge>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground font-mono">
                    job: {review.job_id.slice(0, 16)}…
                  </p>
                </div>
                <div className="text-right space-y-1">
                  <p className="text-sm font-medium">{formatCurrency(review.cost_usd_so_far)}</p>
                  <p className="text-xs text-muted-foreground">{formatRelative(review.created_at)}</p>
                </div>
              </Link>
            ))}
          </div>

          {reviews.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-16 text-center">
              <ClipboardList className="h-12 w-12 text-muted-foreground/40" />
              <p className="font-medium">No reviews found</p>
              <p className="text-sm text-muted-foreground">
                Reviews appear when jobs are flagged for human evaluation.
              </p>
            </div>
          )}

          <LoadMoreButton
            hasNextPage={hasNextPage}
            isFetchingNextPage={isFetchingNextPage}
            fetchNextPage={fetchNextPage}
          />
        </>
      )}
    </div>
  );
}
