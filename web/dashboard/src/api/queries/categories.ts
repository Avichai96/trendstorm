import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { Category, CreateCategoryBody, UpdateCategoryBody, Page } from "../types.generated";

export const categoryKeys = {
  all: ["categories"] as const,
  list: (archived?: boolean, search?: string) =>
    [...categoryKeys.all, "list", { archived, search }] as const,
  detail: (id: string) => [...categoryKeys.all, "detail", id] as const,
};

export const categoriesListOptions = (archived = false, search?: string) =>
  infiniteQueryOptions({
    queryKey: categoryKeys.list(archived, search),
    queryFn: async ({ pageParam }) => {
      const resp = await api.get<{ categories: Category[]; next_cursor: string | null }>(
        "/v1/categories",
        {
          include_archived: archived,
          search: search || undefined,
          limit: 25,
          cursor: pageParam as string | undefined,
        },
      );
      return { items: resp.categories, next_cursor: resp.next_cursor } satisfies Page<Category>;
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const categoryDetailOptions = (id: string) =>
  queryOptions({
    queryKey: categoryKeys.detail(id),
    queryFn: () => api.get<Category>(`/v1/categories/${id}`),
    enabled: !!id,
  });

// Mutation helpers — call these from useMutation({ mutationFn: ... })
export const createCategory = (body: CreateCategoryBody) =>
  api.post<Category>("/v1/categories", body);

export const updateCategory = (id: string, body: UpdateCategoryBody) =>
  api.patch<Category>(`/v1/categories/${id}`, body);

export const archiveCategory = (id: string) =>
  api.patch<Category>(`/v1/categories/${id}`, { archived: true } satisfies UpdateCategoryBody);

export const unarchiveCategory = (id: string) =>
  api.patch<Category>(`/v1/categories/${id}`, { archived: false } satisfies UpdateCategoryBody);
