import "@testing-library/jest-dom";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(cleanup);

// Stub out Auth0 to avoid provider setup in unit tests
vi.mock("@auth0/auth0-react", () => ({
  useAuth0: () => ({
    isAuthenticated: true,
    isLoading: false,
    user: { email: "test@example.com", "https://trendstorm.ai/roles": ["reviewer", "admin"], "https://trendstorm.ai/tenants": [{ tenant_id: "t1", name: "Test Tenant" }] },
    loginWithRedirect: vi.fn(),
    logout: vi.fn(),
    getAccessTokenSilently: vi.fn().mockResolvedValue("test-token"),
  }),
  Auth0Provider: ({ children }: { children: React.ReactNode }) => children,
}));

// Stub sessionStorage
Object.defineProperty(window, "sessionStorage", {
  value: {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  },
  writable: true,
});
