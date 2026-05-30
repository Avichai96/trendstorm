import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ApiKeys from "@/pages/ApiKeys";
import * as apiKeyQueries from "@/api/queries/api_keys";
import type { ApiKey, ApiKeyCreated } from "@/api/types.generated";

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const MOCK_ACTIVE_KEY: ApiKey = {
  id: "01HTEST00000000000000000010",
  name: "CI Pipeline",
  key_prefix: "ts_abc123",
  tenant_id: "01HTEST00000000000000000000",
  created_at: new Date().toISOString(),
  last_used_at: null,
  revoked_at: null,
  is_active: true,
};

const MOCK_REVOKED_KEY: ApiKey = {
  ...MOCK_ACTIVE_KEY,
  id: "01HTEST00000000000000000011",
  name: "Old Key",
  revoked_at: new Date().toISOString(),
  is_active: false,
};

const MOCK_CREATED: ApiKeyCreated = {
  id: "01HTEST00000000000000000012",
  name: "New Key",
  key: "ts_supersecretplaintext1234567890",
  key_prefix: "ts_super",
  tenant_id: "01HTEST00000000000000000000",
  created_at: new Date().toISOString(),
};

describe("ApiKeys page", () => {
  beforeEach(() => {
    vi.spyOn(apiKeyQueries, "apiKeysListOptions").mockReturnValue({
      queryKey: ["api_keys", "list"],
      queryFn: async () => [MOCK_ACTIVE_KEY, MOCK_REVOKED_KEY],
    } as ReturnType<typeof apiKeyQueries.apiKeysListOptions>);
  });

  it("renders page header", async () => {
    render(
      <Wrapper>
        <ApiKeys />
      </Wrapper>,
    );
    expect(screen.getByText("API Keys")).toBeInTheDocument();
  });

  it("shows active and revoked keys", async () => {
    render(
      <Wrapper>
        <ApiKeys />
      </Wrapper>,
    );
    await waitFor(() => {
      expect(screen.getByText("CI Pipeline")).toBeInTheDocument();
      expect(screen.getByText("Old Key")).toBeInTheDocument();
    });
  });

  it("opens revoke confirmation when trash icon clicked", async () => {
    render(
      <Wrapper>
        <ApiKeys />
      </Wrapper>,
    );
    await waitFor(() => screen.getByText("CI Pipeline"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));
    });
    await waitFor(() => {
      expect(screen.getByText("Revoke API key?")).toBeInTheDocument();
    });
  });

  it("shows plaintext key in reveal dialog after creation", async () => {
    const mockCreate = vi.fn().mockResolvedValue(MOCK_CREATED);
    vi.spyOn(apiKeyQueries, "createApiKey").mockImplementation(mockCreate);

    render(
      <Wrapper>
        <ApiKeys />
      </Wrapper>,
    );
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "New Key" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => {
      expect(screen.getByText("API Key Created")).toBeInTheDocument();
      expect(screen.getByText(MOCK_CREATED.key)).toBeInTheDocument();
    });
  });
});
