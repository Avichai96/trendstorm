import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { Job, Page, JobStatus, Analysis } from "../types.generated";

export const jobKeys = {
  all: ["jobs"] as const,
  list: (status?: JobStatus, categoryId?: string) =>
    [...jobKeys.all, "list", { status, categoryId }] as const,
  detail: (id: string) => [...jobKeys.all, "detail", id] as const,
  analysis: (id: string) => [...jobKeys.all, "analysis", id] as const,
};

export const jobsListOptions = (status?: JobStatus, categoryId?: string) =>
  infiniteQueryOptions({
    queryKey: jobKeys.list(status, categoryId),
    queryFn: async ({ pageParam }) => {
      const resp = await api.get<{ jobs: Job[]; next_cursor: string | null }>("/v1/jobs", {
        status,
        category_id: categoryId,
        limit: 20,
        cursor: pageParam as string | undefined,
      });
      return { items: resp.jobs, next_cursor: resp.next_cursor } satisfies Page<Job>;
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const jobDetailOptions = (id: string) =>
  queryOptions({
    queryKey: jobKeys.detail(id),
    queryFn: () => api.get<Job>(`/v1/jobs/${id}`),
    enabled: !!id,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      const terminal: JobStatus[] = ["completed", "failed", "cancelled", "rejected"];
      return status && terminal.includes(status) ? false : 5_000;
    },
  });

export const jobAnalysisOptions = (id: string) =>
  queryOptions({
    queryKey: jobKeys.analysis(id),
    queryFn: () => api.get<Analysis>(`/v1/jobs/${id}/analysis`),
    enabled: !!id,
  });
