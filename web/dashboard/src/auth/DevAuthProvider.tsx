import { Auth0Context, initialContext } from "@auth0/auth0-react";
import type { ReactNode } from "react";

const DEV_USER = {
  email: "dev@trendstorm.local",
  name: "Dev User",
  sub: "dev|local",
  "https://trendstorm.ai/roles": ["admin", "reviewer"],
  "https://trendstorm.ai/tenants": [
    { tenant_id: "dev-tenant-01", name: "Dev Tenant" },
  ],
};

const devContext = {
  ...initialContext,
  isAuthenticated: true,
  isLoading: false,
  user: DEV_USER,
  getAccessTokenSilently: () => Promise.resolve("dev-token"),
  loginWithRedirect: () => Promise.resolve(),
  logout: () => Promise.resolve(),
};

export function DevAuthProvider({ children }: { children: ReactNode }) {
  return (
    <Auth0Context.Provider value={devContext as unknown as typeof initialContext}>
      {children}
    </Auth0Context.Provider>
  );
}
