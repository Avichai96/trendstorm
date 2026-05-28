import { useAuth0 } from "@auth0/auth0-react";
import { type ReactNode, useEffect } from "react";
import { setTokenProvider } from "@/api/client";

interface AuthGuardProps {
  children: ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps) {
  const { isLoading, isAuthenticated, loginWithRedirect, getAccessTokenSilently } = useAuth0();

  useEffect(() => {
    setTokenProvider(getAccessTokenSilently);
  }, [getAccessTokenSilently]);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      void loginWithRedirect({ appState: { returnTo: window.location.pathname } });
    }
  }, [isLoading, isAuthenticated, loginWithRedirect]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (!isAuthenticated) return null;

  return <>{children}</>;
}
