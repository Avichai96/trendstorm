import { useAuth0 } from "@auth0/auth0-react";
import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";

interface RoleGuardProps {
  role: string;
  children: ReactNode;
  fallback?: ReactNode;
}

export function useRoles(): string[] {
  const { user } = useAuth0();
  const claimKey = "https://trendstorm.ai/roles";
  const roles = user?.[claimKey];
  if (Array.isArray(roles)) return roles as string[];
  return [];
}

export function RoleGuard({ role, children, fallback }: RoleGuardProps) {
  const roles = useRoles();
  if (!roles.includes(role)) {
    return fallback != null ? <>{fallback}</> : <Navigate to="/" replace />;
  }
  return <>{children}</>;
}
