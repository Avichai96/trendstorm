import { infiniteQueryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { AuditLogEntry, Page } from "../types.generated";

export const auditKeys = {
  all: ["audit"] as const,
  list: (eventType?: string, actor?: string) =>
    [...auditKeys.all, "list", { eventType, actor }] as const,
};

export const auditListOptions = (eventType?: string, actor?: string) =>
  infiniteQueryOptions({
    queryKey: auditKeys.list(eventType, actor),
    queryFn: async ({ pageParam }) =>
      api.get<Page<AuditLogEntry>>("/v1/audit", {
        event_type: eventType,
        actor,
        limit: 50,
        cursor: pageParam as string | undefined,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
