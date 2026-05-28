import { useParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, useCallback, useRef, useEffect } from "react";
import { jobDetailOptions, jobKeys } from "@/api/queries/jobs";
import { PipelineProgress } from "@/components/jobs/PipelineProgress";
import { JobStatusBadge } from "@/components/shared/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSSE } from "@/hooks/useSSE";
import { ArrowLeft, FileText, Wifi, WifiOff } from "lucide-react";
import { formatDate, formatCurrency, formatRelative } from "@/lib/utils";
import type { StreamEvent } from "@/api/types.generated";

export default function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const { data: job, isLoading } = useQuery(jobDetailOptions(id!));

  const [events, setEvents] = useState<StreamEvent[]>([]);
  const eventsBottomRef = useRef<HTMLDivElement | null>(null);

  const isTerminal = job
    ? ["completed", "failed", "cancelled", "rejected"].includes(job.status)
    : false;

  const onEvent = useCallback(
    (event: StreamEvent) => {
      setEvents((prev) => [...prev.slice(-99), event]);
      if (event.event_type === "stage_changed") {
        void qc.invalidateQueries({ queryKey: jobKeys.detail(id!) });
      }
    },
    [id, qc],
  );

  // Scroll live-events list to bottom whenever a new event arrives.
  useEffect(() => {
    eventsBottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  const onTerminal = useCallback(() => {
    void qc.invalidateQueries({ queryKey: jobKeys.detail(id!) });
  }, [id, qc]);

  const { connected, error: sseError, reconnect } = useSSE({
    jobId: id!,
    enabled: !isTerminal,
    onEvent,
    onTerminal,
  });

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-48 rounded-lg" />
      </div>
    );
  }

  if (!job) return <p className="text-muted-foreground">Job not found.</p>;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/jobs" className="text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <div>
          <h1 className="font-mono text-lg font-bold">{job.job_id}</h1>
          <p className="text-xs text-muted-foreground">{formatDate(job.created_at)}</p>
        </div>
        <JobStatusBadge status={job.status} />
        {!isTerminal && (
          <div className="flex items-center gap-1.5 ml-auto">
            {connected ? (
              <Badge variant="success" className="flex items-center gap-1">
                <Wifi className="h-3 w-3" />
                Live
              </Badge>
            ) : (
              <Button variant="outline" size="sm" onClick={() => void reconnect()}>
                <WifiOff className="mr-1 h-3 w-3" />
                Reconnect
              </Button>
            )}
          </div>
        )}
      </div>

      {sseError && (
        <p className="text-sm text-destructive">SSE: {sseError}</p>
      )}

      <Card>
        <CardContent className="pt-6">
          <PipelineProgress status={job.status} refinementLoops={job.refinement_loops_used} />
        </CardContent>
      </Card>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Cost</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatCurrency(job.cost_usd)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Refinements</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{job.refinement_loops_used}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Updated</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm font-medium">{formatRelative(job.updated_at)}</p>
          </CardContent>
        </Card>
      </div>

      {job.status === "completed" && job.report_id && (
        <div className="flex items-center gap-3">
          <Link to={`/jobs/${job.job_id}/report`}>
            <Button>
              <FileText className="mr-2 h-4 w-4" />
              View Report
            </Button>
          </Link>
          {job.pdf_report_url && (
            <a href={job.pdf_report_url} target="_blank" rel="noopener noreferrer">
              <Button variant="outline">Download PDF</Button>
            </a>
          )}
          {job.json_report_url && (
            <a href={job.json_report_url} target="_blank" rel="noopener noreferrer">
              <Button variant="outline">Download JSON</Button>
            </a>
          )}
        </div>
      )}

      {job.error_message && (
        <Card className="border-destructive">
          <CardHeader>
            <CardTitle className="text-sm text-destructive">Error</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs text-muted-foreground whitespace-pre-wrap">{job.error_message}</pre>
          </CardContent>
        </Card>
      )}

      {events.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Live Events ({events.length})</CardTitle>
          </CardHeader>
          <CardContent className="max-h-64 overflow-y-auto space-y-1">
            {events.map((ev, i) => (
              <div key={i} className="flex items-center gap-2 text-xs font-mono">
                <span className="text-muted-foreground">#{ev.seq}</span>
                <Badge variant="secondary" className="text-xs">{ev.event_type}</Badge>
                <span className="truncate text-muted-foreground">
                  {JSON.stringify(ev.payload).slice(0, 80)}
                </span>
              </div>
            ))}
            <div ref={eventsBottomRef} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
