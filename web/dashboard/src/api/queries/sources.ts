import { queryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { Source, RegisterSourceBody, Page } from "../types.generated";

export const sourceKeys = {
  all: ["sources"] as const,
  byCategory: (categoryId: string) => [...sourceKeys.all, "byCategory", categoryId] as const,
};

export const sourcesByCategoryOptions = (categoryId: string) =>
  queryOptions({
    queryKey: sourceKeys.byCategory(categoryId),
    queryFn: async () => {
      const resp = await api.get<{ sources: Source[] }>("/v1/sources", {
        category_id: categoryId,
        limit: 100,
      });
      return { items: resp.sources, next_cursor: null } satisfies Page<Source>;
    },
    enabled: !!categoryId,
  });

// Mutation helpers
export const registerSource = (body: RegisterSourceBody) =>
  api.post<Source>("/v1/sources", body);

export const deleteSource = (sourceId: string) =>
  api.delete<void>(`/v1/sources/${sourceId}`);
