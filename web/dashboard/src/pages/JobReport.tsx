import { useParams, Link } from "react-router-dom";
import { useQuery, queryOptions } from "@tanstack/react-query";
import { jobDetailOptions } from "@/api/queries/jobs";
import { api } from "@/api/client";
import { MarkdownViewer } from "@/components/reports/MarkdownViewer";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ArrowLeft } from "lucide-react";
import type { Analysis } from "@/api/types.generated";

const analysisOptions = (jobId: string) =>
  queryOptions({
    queryKey: ["analysis", jobId],
    queryFn: () => api.get<Analysis>(`/v1/jobs/${jobId}/analysis`),
    enabled: !!jobId,
  });

export default function JobReport() {
  const { id } = useParams<{ id: string }>();

  const { data: job, isLoading: jobLoading } = useQuery(jobDetailOptions(id!));
  const { data: analysis, isLoading: analysisLoading } = useQuery(analysisOptions(id!));

  if (jobLoading) return <Skeleton className="h-screen rounded-lg" />;
  if (!job) return <p className="text-muted-foreground">Job not found.</p>;

  return (
    <div className="flex h-full flex-col space-y-4">
      <div className="flex items-center gap-3">
        <Link to={`/jobs/${id}`} className="text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <h1 className="text-xl font-bold">Report</h1>
        <span className="font-mono text-xs text-muted-foreground">{job.id.slice(0, 16)}…</span>
      </div>

      {analysisLoading ? (
        <Skeleton className="h-96 rounded-lg" />
      ) : analysis ? (
        <Card>
          <CardContent className="pt-6">
            <MarkdownViewer content={analysis.summary} />
          </CardContent>
        </Card>
      ) : (
        <p className="text-muted-foreground">Analysis not yet available.</p>
      )}
    </div>
  );
}
