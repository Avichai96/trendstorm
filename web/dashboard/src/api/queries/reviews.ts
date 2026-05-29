import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { Review, Page, ReviewStatus, ResolveReviewBody } from "../types.generated";

export const reviewKeys = {
  all: ["reviews"] as const,
  list: (status?: ReviewStatus) => [...reviewKeys.all, "list", { status }] as const,
  detail: (id: string) => [...reviewKeys.all, "detail", id] as const,
};

export const reviewsListOptions = (status?: ReviewStatus) =>
  infiniteQueryOptions({
    queryKey: reviewKeys.list(status),
    queryFn: async ({ pageParam }) => {
      // Reviews endpoint returns a plain array (no envelope).
      const items = await api.get<Review[]>("/v1/reviews", {
        status,
        limit: 20,
        before_id: pageParam as string | undefined,
      });
      const next_cursor = items.length === 20 ? items[items.length - 1]?.id ?? null : null;
      return { items, next_cursor } satisfies Page<Review>;
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 30_000,
  });

export const reviewDetailOptions = (id: string) =>
  queryOptions({
    queryKey: reviewKeys.detail(id),
    queryFn: () => api.get<Review>(`/v1/reviews/${id}`),
    enabled: !!id,
  });

export async function resolveReview(id: string, body: ResolveReviewBody): Promise<Review> {
  return api.post<Review>(`/v1/reviews/${id}/resolve`, body);
}
