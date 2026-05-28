import { useAuth0 } from "@auth0/auth0-react";
import { useState, useEffect } from "react";

const CLAIM = "https://trendstorm.ai/tenants";
const STORAGE_KEY = "tenant_id";

export interface TenantMembership {
  tenant_id: string;
  name: string;
}

export function useTenant() {
  const { user } = useAuth0();

  const tenants: TenantMembership[] = (() => {
    const raw = user?.[CLAIM];
    if (Array.isArray(raw)) return raw as TenantMembership[];
    return [];
  })();

  const [activeTenantId, setActiveTenantIdRaw] = useState<string | null>(() => {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    if (stored) return stored;
    return tenants[0]?.tenant_id ?? null;
  });

  // Sync first-available tenant if user just logged in
  useEffect(() => {
    if (!activeTenantId && tenants.length > 0) {
      setActiveTenantIdRaw(tenants[0].tenant_id);
    }
  }, [activeTenantId, tenants]);

  function setActiveTenant(id: string) {
    sessionStorage.setItem(STORAGE_KEY, id);
    setActiveTenantIdRaw(id);
  }

  const activeTenant = tenants.find((t) => t.tenant_id === activeTenantId) ?? tenants[0] ?? null;

  return { tenants, activeTenant, activeTenantId, setActiveTenant };
}
