import { useTenant } from "@/hooks/useTenant";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useQueryClient } from "@tanstack/react-query";

export function TenantSelector() {
  const { tenants, activeTenant, setActiveTenant } = useTenant();
  const qc = useQueryClient();

  if (tenants.length <= 1) return null;

  return (
    <Select
      value={activeTenant?.tenant_id}
      onValueChange={(id) => {
        setActiveTenant(id);
        // Invalidate all queries so they re-fetch with the new X-Tenant-ID header.
        // invalidateQueries is safe for in-flight requests; qc.clear() is not.
        void qc.invalidateQueries();
      }}
    >
      <SelectTrigger className="h-8 w-48 text-xs">
        <SelectValue placeholder="Select tenant" />
      </SelectTrigger>
      <SelectContent>
        {tenants.map((t) => (
          <SelectItem key={t.tenant_id} value={t.tenant_id}>
            {t.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
