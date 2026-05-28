import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { Auth0Provider } from "@auth0/auth0-react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import App from "./App";
import { DevAuthProvider } from "./auth/DevAuthProvider";
import { loadConfig } from "./api/client";
import "./index.css";

const PLACEHOLDER_DOMAINS = ["your-tenant.auth0.com", "", undefined];

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
});

async function bootstrap() {
  const config = await loadConfig();

  const root = document.getElementById("root");
  if (!root) throw new Error("#root element not found");

  const isDevBypass = import.meta.env.DEV && PLACEHOLDER_DOMAINS.includes(config.auth0Domain);

  const authWrapper = isDevBypass
    ? (children: React.ReactNode) => <DevAuthProvider>{children}</DevAuthProvider>
    : (children: React.ReactNode) => (
        <Auth0Provider
          domain={config.auth0Domain}
          clientId={config.auth0ClientId}
          authorizationParams={{
            redirect_uri: window.location.origin,
            audience: config.auth0Audience,
            scope: "openid profile email",
          }}
          onRedirectCallback={(appState) => {
            window.history.replaceState({}, document.title, appState?.returnTo ?? "/");
          }}
        >
          {children}
        </Auth0Provider>
      );

  ReactDOM.createRoot(root).render(
    <React.StrictMode>
      {authWrapper(
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
          {import.meta.env.DEV && <ReactQueryDevtools initialIsOpen={false} />}
        </QueryClientProvider>,
      )}
    </React.StrictMode>,
  );
}

void bootstrap();
