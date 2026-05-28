import { queryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { QuotaUsage } from "../types.generated";

export const quotaKeys = {
  all: ["quota"] as const,
  currentMonth: () => [...quotaKeys.all, "currentMonth"] as const,
};

export const quotaCurrentMonthOptions = () =>
  queryOptions({
    queryKey: quotaKeys.currentMonth(),
    queryFn: () => api.get<QuotaUsage>("/v1/billing/quota"),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
