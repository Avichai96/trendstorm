import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { ErrorBoundary } from "@/components/shared/ErrorBoundary";

export function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <Sidebar />
      <main className="flex-1 overflow-y-auto p-6">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
