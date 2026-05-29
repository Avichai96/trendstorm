import { useQuery } from "@tanstack/react-query";
import { quotaCurrentMonthOptions } from "@/api/queries/quota";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";
import { formatCurrency } from "@/lib/utils";

export default function Usage() {
  const { data: quota, isLoading } = useQuery(quotaCurrentMonthOptions());

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 rounded-lg" />
      </div>
    );
  }

  if (!quota) return <p className="text-muted-foreground">No quota data available.</p>;

  const spendPct = Math.min(100, (quota.monthly_spend_usd / quota.monthly_limit_usd) * 100);
  const jobsPct = Math.min(100, (quota.jobs_this_month / quota.jobs_limit) * 100);
  const hardCapReached = !quota.allowed && quota.reason === "spend_limit";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Usage</h1>
        <p className="text-sm text-muted-foreground">Current billing period</p>
      </div>

      {hardCapReached && (
        <div className="rounded-lg border border-destructive bg-destructive/10 p-4 text-sm text-destructive">
          Spend limit reached — new jobs are disabled until the period resets.
        </div>
      )}
      {!quota.allowed && !hardCapReached && quota.reason && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800">
          Job creation paused: {quota.reason.replace(/_/g, " ")}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">
              Monthly Spend
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatCurrency(quota.monthly_spend_usd)}</p>
            <Progress value={spendPct} className="mt-2 h-2" />
            <p className="mt-1 text-xs text-muted-foreground">
              {spendPct.toFixed(1)}% of {formatCurrency(quota.monthly_limit_usd)} limit
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">
              Jobs This Month
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{quota.jobs_this_month}</p>
            <Progress value={jobsPct} className="mt-2 h-2" />
            <p className="mt-1 text-xs text-muted-foreground">
              {jobsPct.toFixed(1)}% of {quota.jobs_limit} job limit
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-1">
          <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Status</CardTitle>
        </CardHeader>
        <CardContent>
          {quota.allowed ? (
            <Badge variant="success">Allowed — jobs can be created</Badge>
          ) : (
            <Badge variant="destructive">Blocked — {quota.reason ?? "limit reached"}</Badge>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
