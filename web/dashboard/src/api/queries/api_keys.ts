import { queryOptions } from "@tanstack/react-query";
import { api } from "../client";
import type { ApiKey, ApiKeyCreated, CreateApiKeyBody } from "../types.generated";

export const apiKeyKeys = {
  all: ["api_keys"] as const,
  list: () => [...apiKeyKeys.all, "list"] as const,
};

export const apiKeysListOptions = () =>
  queryOptions({
    queryKey: apiKeyKeys.list(),
    queryFn: () => api.get<{ keys: ApiKey[] }>("/v1/api-keys").then((r) => r.keys),
  });

// Mutation helpers
export const createApiKey = (body: CreateApiKeyBody) =>
  api.post<ApiKeyCreated>("/v1/api-keys", body);

export const revokeApiKey = (keyId: string) =>
  api.delete<void>(`/v1/api-keys/${keyId}`);
