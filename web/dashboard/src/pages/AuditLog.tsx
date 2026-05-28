import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { auditListOptions } from "@/api/queries/audit";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadMoreButton } from "@/components/shared/CursorPagination";
import { formatDate } from "@/lib/utils";
import { ShieldAlert } from "lucide-react";
import type { AuditLogEntry } from "@/api/types.generated";

const ALL = "__all__";
const EVENT_TYPES = [ALL, "ssrf_blocked", "url_blocked", "pii_detected"];

const EVENT_BADGE: Record<string, "destructive" | "warning" | "info"> = {
  ssrf_blocked: "destructive",
  url_blocked: "warning",
  pii_detected: "info",
};

function EntryDetail({ entry }: { entry: AuditLogEntry }) {
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      {entry.resource_type && <p>Resource: {entry.resource_type} / {entry.resource_id}</p>}
      {entry.actor && <p>Actor: {entry.actor}</p>}
      {Object.keys(entry.metadata).length > 0 && (
        <pre className="mt-1 rounded bg-muted p-2 text-xs overflow-x-auto">
          {JSON.stringify(entry.metadata, null, 2)}
        </pre>
      )}
    </div>
  );
}

export default function AuditLog() {
  const [eventType, setEventType] = useState(ALL);
  const [actor, setActor] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const { data, isLoading, hasNextPage, isFetchingNextPage, fetchNextPage } = useInfiniteQuery(
    auditListOptions(eventType === ALL ? undefined : eventType, actor || undefined),
  );

  const entries = data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Audit Log</h1>
        <p className="text-sm text-muted-foreground">Security events — 365-day retention</p>
      </div>

      <div className="flex flex-wrap gap-3">
        <Select value={eventType} onValueChange={setEventType}>
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {EVENT_TYPES.map((t) => (
              <SelectItem key={t} value={t}>{t === ALL ? "All event types" : t}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          placeholder="Filter by actor…"
          className="max-w-xs"
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        />
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 10 }).map((_, i) => (
            <Skeleton key={i} className="h-14 rounded-lg" />
          ))}
        </div>
      ) : (
        <>
          <div className="divide-y rounded-lg border bg-card">
            {entries.map((entry) => (
              <div key={entry.id}>
                <button
                  className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-accent"
                  onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <Badge
                      variant={EVENT_BADGE[entry.event_type] ?? "secondary"}
                      className="shrink-0"
                    >
                      {entry.event_type}
                    </Badge>
                    <span className="truncate text-xs font-mono text-muted-foreground">
                      {entry.actor ?? "system"}
                    </span>
                  </div>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatDate(entry.created_at)}
                  </span>
                </button>
                {expanded === entry.id && (
                  <div className="border-t bg-muted/30 px-4 py-3">
                    <EntryDetail entry={entry} />
                  </div>
                )}
              </div>
            ))}
          </div>

          {entries.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-16 text-center">
              <ShieldAlert className="h-12 w-12 text-muted-foreground/40" />
              <p className="font-medium">No audit events</p>
              <p className="text-sm text-muted-foreground">Security events appear here when they occur.</p>
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
