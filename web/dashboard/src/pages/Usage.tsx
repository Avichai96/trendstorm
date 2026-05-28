import { useQuery } from "@tanstack/react-query";
import { quotaCurrentMonthOptions } from "@/api/queries/quota";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";
import { formatCurrency, formatDate } from "@/lib/utils";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { format, parseISO } from "date-fns";

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

  const pctHard = Math.min(100, (quota.current_usd / quota.hard_cap_usd) * 100);

  const chartData = quota.daily_breakdown.map((d) => ({
    date: format(parseISO(d.date), "MMM d"),
    usd: d.usd,
  }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Usage</h1>
        <p className="text-sm text-muted-foreground">
          {formatDate(quota.period_start)} → {formatDate(quota.period_end)}
        </p>
      </div>

      {quota.hard_cap_reached && (
        <div className="rounded-lg border border-destructive bg-destructive/10 p-4 text-sm text-destructive">
          Hard cap reached — new jobs are disabled until the period resets.
        </div>
      )}
      {quota.soft_cap_reached && !quota.hard_cap_reached && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800">
          Warning: current spend exceeds 80% of soft cap. Monitor closely.
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Current Spend</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatCurrency(quota.current_usd)}</p>
            <Progress value={pctHard} className="mt-2 h-2" />
            <p className="mt-1 text-xs text-muted-foreground">
              {pctHard.toFixed(1)}% of {formatCurrency(quota.hard_cap_usd)} hard cap
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Soft Cap</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatCurrency(quota.soft_cap_usd)}</p>
            <div className="mt-1">
              {quota.soft_cap_reached ? (
                <Badge variant="warning">Exceeded</Badge>
              ) : (
                <Badge variant="success">OK</Badge>
              )}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs uppercase tracking-wide text-muted-foreground">Hard Cap</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatCurrency(quota.hard_cap_usd)}</p>
            <div className="mt-1">
              {quota.hard_cap_reached ? (
                <Badge variant="destructive">Reached</Badge>
              ) : (
                <Badge variant="success">OK</Badge>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Daily Spend</CardTitle>
        </CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} />
              <YAxis tickFormatter={(v) => `$${(v as number).toFixed(3)}`} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(v) => [formatCurrency(v as number), "Spend"]} />
              <Line
                type="monotone"
                dataKey="usd"
                stroke="hsl(var(--primary))"
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">By Stage</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {Object.entries(quota.by_stage).sort(([, a], [, b]) => b - a).map(([stage, usd]) => (
              <div key={stage} className="flex justify-between text-sm">
                <span className="capitalize text-muted-foreground">{stage.replace(/_/g, " ")}</span>
                <span className="font-mono font-medium">{formatCurrency(usd)}</span>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">By Provider</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {Object.entries(quota.by_provider).sort(([, a], [, b]) => b - a).map(([provider, usd]) => (
              <div key={provider} className="flex justify-between text-sm">
                <span className="capitalize text-muted-foreground">{provider}</span>
                <span className="font-mono font-medium">{formatCurrency(usd)}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
