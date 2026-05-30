import { Routes, Route, Navigate } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { AuthGuard } from "@/auth/AuthGuard";
import { RoleGuard } from "@/auth/RoleGuard";
import { lazy, Suspense } from "react";
import { Skeleton } from "@/components/ui/skeleton";

const Categories = lazy(() => import("@/pages/Categories"));
const CategoryDetail = lazy(() => import("@/pages/CategoryDetail"));
const Jobs = lazy(() => import("@/pages/Jobs"));
const JobDetail = lazy(() => import("@/pages/JobDetail"));
const JobReport = lazy(() => import("@/pages/JobReport"));
const Reviews = lazy(() => import("@/pages/Reviews"));
const ReviewDetail = lazy(() => import("@/pages/ReviewDetail"));
const Usage = lazy(() => import("@/pages/Usage"));
const AuditLog = lazy(() => import("@/pages/AuditLog"));
const ApiKeys = lazy(() => import("@/pages/ApiKeys"));
const Settings = lazy(() => import("@/pages/Settings"));

function PageFallback() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-64 rounded-lg" />
    </div>
  );
}

export default function App() {
  return (
    <AuthGuard>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/categories" replace />} />
          <Route
            path="categories"
            element={
              <Suspense fallback={<PageFallback />}>
                <Categories />
              </Suspense>
            }
          />
          <Route
            path="categories/:id"
            element={
              <Suspense fallback={<PageFallback />}>
                <CategoryDetail />
              </Suspense>
            }
          />
          <Route
            path="jobs"
            element={
              <Suspense fallback={<PageFallback />}>
                <Jobs />
              </Suspense>
            }
          />
          <Route
            path="jobs/:id"
            element={
              <Suspense fallback={<PageFallback />}>
                <JobDetail />
              </Suspense>
            }
          />
          <Route
            path="jobs/:id/report"
            element={
              <Suspense fallback={<PageFallback />}>
                <JobReport />
              </Suspense>
            }
          />
          <Route
            path="reviews"
            element={
              <RoleGuard role="reviewer">
                <Suspense fallback={<PageFallback />}>
                  <Reviews />
                </Suspense>
              </RoleGuard>
            }
          />
          <Route
            path="reviews/:id"
            element={
              <RoleGuard role="reviewer">
                <Suspense fallback={<PageFallback />}>
                  <ReviewDetail />
                </Suspense>
              </RoleGuard>
            }
          />
          <Route
            path="usage"
            element={
              <Suspense fallback={<PageFallback />}>
                <Usage />
              </Suspense>
            }
          />
          <Route
            path="audit"
            element={
              <RoleGuard role="admin">
                <Suspense fallback={<PageFallback />}>
                  <AuditLog />
                </Suspense>
              </RoleGuard>
            }
          />
          <Route
            path="api-keys"
            element={
              <Suspense fallback={<PageFallback />}>
                <ApiKeys />
              </Suspense>
            }
          />
          <Route
            path="settings"
            element={
              <Suspense fallback={<PageFallback />}>
                <Settings />
              </Suspense>
            }
          />
          <Route path="*" element={<Navigate to="/categories" replace />} />
        </Route>
      </Routes>
    </AuthGuard>
  );
}
