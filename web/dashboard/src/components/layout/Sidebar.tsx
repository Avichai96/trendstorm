import { NavLink } from "react-router-dom";
import { useAuth0 } from "@auth0/auth0-react";
import { useRoles } from "@/auth/RoleGuard";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard,
  FolderOpen,
  Briefcase,
  ClipboardList,
  DollarSign,
  ShieldAlert,
  Key,
  Settings,
  LogOut,
} from "lucide-react";
import { TenantSelector } from "./TenantSelector";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  role?: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/categories", label: "Categories", icon: FolderOpen },
  { to: "/jobs", label: "Jobs", icon: Briefcase },
  { to: "/reviews", label: "Reviews", icon: ClipboardList, role: "reviewer" },
  { to: "/usage", label: "Usage", icon: DollarSign },
  { to: "/audit", label: "Audit Log", icon: ShieldAlert, role: "admin" },
  { to: "/api-keys", label: "API Keys", icon: Key },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const { logout, user } = useAuth0();
  const roles = useRoles();

  const visibleItems = NAV_ITEMS.filter((item) => !item.role || roles.includes(item.role));

  return (
    <aside className="flex h-screen w-56 flex-col border-r bg-card">
      <div className="flex h-14 items-center gap-2 border-b px-4">
        <LayoutDashboard className="h-5 w-5 text-primary" />
        <span className="font-bold tracking-tight">TrendStorm</span>
      </div>

      <div className="px-3 py-2">
        <TenantSelector />
      </div>

      <nav className="flex-1 space-y-1 px-2 py-2">
        {visibleItems.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="border-t p-3">
        <div className="mb-1 truncate text-xs text-muted-foreground">{user?.email}</div>
        <button
          onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}
          className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground"
        >
          <LogOut className="h-3.5 w-3.5" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
