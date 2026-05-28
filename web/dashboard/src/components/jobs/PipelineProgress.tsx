import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import type { JobStatus } from "@/api/types.generated";
import { cn } from "@/lib/utils";

const STAGES: JobStatus[] = [
  "pending", "ingesting", "ingested", "embedding", "embedded",
  "retrieving", "analyzing", "awaiting_review", "publishing", "completed",
];

function stageIndex(status: JobStatus): number {
  const idx = STAGES.indexOf(status);
  return idx >= 0 ? idx : 0;
}

export function PipelineProgress({ status, refinementLoops }: { status: JobStatus; refinementLoops: number }) {
  const isTerminal = ["completed", "failed", "cancelled", "rejected"].includes(status);
  const pct = isTerminal && status === "completed"
    ? 100
    : Math.round((stageIndex(status) / (STAGES.length - 1)) * 100);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-sm">
        <span className="font-medium capitalize">{status.replace(/_/g, " ")}</span>
        {refinementLoops > 0 && (
          <Badge variant="info" className="text-xs">
            Refinement loop {refinementLoops}
          </Badge>
        )}
      </div>
      <Progress
        value={pct}
        className={cn(
          status === "failed" || status === "rejected" ? "[&>div]:bg-destructive" : "",
          status === "awaiting_review" ? "[&>div]:bg-amber-500" : "",
        )}
      />
      <div className="flex gap-1 overflow-x-auto">
        {STAGES.filter((s) => !["pending"].includes(s)).map((s) => {
          const reached = stageIndex(s) <= stageIndex(status);
          const current = s === status;
          return (
            <div
              key={s}
              className={cn(
                "h-1 flex-1 rounded-full transition-colors",
                current ? "bg-primary" : reached ? "bg-primary/40" : "bg-muted",
              )}
              title={s}
            />
          );
        })}
      </div>
    </div>
  );
}
