import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { jobsListOptions } from "@/api/queries/jobs";
import { JobStatusBadge } from "@/components/shared/StatusBadge";
import { LoadMoreButton } from "@/components/shared/CursorPagination";
import { Skeleton } from "@/components/ui/skeleton";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatRelative, formatCurrency } from "@/lib/utils";
import type { JobStatus } from "@/api/types.generated";
import { Briefcase } from "lucide-react";

const ALL = "__all__";

const STATUS_OPTIONS: { value: JobStatus | typeof ALL; label: string }[] = [
  { value: ALL, label: "All statuses" },
  { value: "pending", label: "Pending" },
  { value: "analyzing", label: "Analyzing" },
  { value: "awaiting_review", label: "Awaiting review" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "rejected", label: "Rejected" },
];

export default function Jobs() {
  const [status, setStatus] = useState<JobStatus | typeof ALL>(ALL);
  const { data, isLoading, hasNextPage, isFetchingNextPage, fetchNextPage } = useInfiniteQuery(
    jobsListOptions(status === ALL ? undefined : status),
  );

  const jobs = data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold">Jobs</h1>
          <p className="text-sm text-muted-foreground">Pipeline execution history</p>
        </div>
        <Select value={status} onValueChange={(v) => setStatus(v as JobStatus | typeof ALL)}>
          <SelectTrigger className="w-48">
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
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-16 rounded-lg" />
          ))}
        </div>
      ) : (
        <>
          <div className="divide-y rounded-lg border bg-card">
            {jobs.map((job) => (
              <Link
                key={job.id}
                to={`/jobs/${job.job_id}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-accent"
              >
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <JobStatusBadge status={job.status} />
                    {job.refinement_loops_used > 0 && (
                      <span className="text-xs text-muted-foreground">
                        ({job.refinement_loops_used} refinement{job.refinement_loops_used > 1 ? "s" : ""})
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground font-mono">{job.job_id}</p>
                </div>
                <div className="text-right space-y-1">
                  <p className="text-sm font-medium">{formatCurrency(job.cost_usd)}</p>
                  <p className="text-xs text-muted-foreground">{formatRelative(job.created_at)}</p>
                </div>
              </Link>
            ))}
          </div>

          {jobs.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-16 text-center">
              <Briefcase className="h-12 w-12 text-muted-foreground/40" />
              <p className="font-medium">No jobs found</p>
              <p className="text-sm text-muted-foreground">
                {status !== ALL ? "Try a different status filter." : "Submit a job via the API to see it here."}
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
