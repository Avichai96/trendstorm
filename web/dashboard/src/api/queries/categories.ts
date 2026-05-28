import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { Category, Page } from "../types.generated";

export const categoryKeys = {
  all: ["categories"] as const,
  list: (archived?: boolean, search?: string) =>
    [...categoryKeys.all, "list", { archived, search }] as const,
  detail: (id: string) => [...categoryKeys.all, "detail", id] as const,
};

export const categoriesListOptions = (archived = false, search?: string) =>
  infiniteQueryOptions({
    queryKey: categoryKeys.list(archived, search),
    queryFn: async ({ pageParam }) =>
      api.get<Page<Category>>("/v1/categories", {
        include_archived: archived,
        search: search || undefined,
        limit: 25,
        cursor: pageParam as string | undefined,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const categoryDetailOptions = (id: string) =>
  queryOptions({
    queryKey: categoryKeys.detail(id),
    queryFn: () => api.get<Category>(`/v1/categories/${id}`),
    enabled: !!id,
  });
