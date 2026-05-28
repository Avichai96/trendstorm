import { queryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { Source, Page } from "../types.generated";

export const sourceKeys = {
  all: ["sources"] as const,
  byCategory: (categoryId: string) => [...sourceKeys.all, "byCategory", categoryId] as const,
};

export const sourcesByCategoryOptions = (categoryId: string) =>
  queryOptions({
    queryKey: sourceKeys.byCategory(categoryId),
    queryFn: () => api.get<Page<Source>>("/v1/sources", { category_id: categoryId, limit: 100 }),
    enabled: !!categoryId,
  });
