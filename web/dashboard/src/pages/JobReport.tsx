import { useParams, Link } from "react-router-dom";
import { useQuery, queryOptions } from "@tanstack/react-query";
import { useState } from "react";
import { jobDetailOptions } from "@/api/queries/jobs";
import { api } from "@/api/client";
import { MarkdownViewer } from "@/components/reports/MarkdownViewer";
import { CitationPanel } from "@/components/reports/CitationPanel";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { ArrowLeft, Columns2, AlertTriangle } from "lucide-react";
import type { Analysis } from "@/api/types.generated";

const analysisOptions = (jobId: string) =>
  queryOptions({
    queryKey: ["analysis", jobId],
    queryFn: () => api.get<Analysis>(`/v1/jobs/${jobId}/analysis`),
    enabled: !!jobId,
  });

const reportContentOptions = (url: string | null) =>
  queryOptions({
    queryKey: ["report-content", url],
    queryFn: async () => {
      if (!url) return null;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Failed to load report (${res.status})`);
      return res.text();
    },
    enabled: !!url,
    staleTime: Infinity,
    retry: 1,
  });

export default function JobReport() {
  const { id } = useParams<{ id: string }>();
  const [citationPanelOpen, setCitationPanelOpen] = useState(false);

  const { data: job, isLoading: jobLoading } = useQuery(jobDetailOptions(id!));
  const { data: analysis } = useQuery(analysisOptions(id!));
  const {
    data: reportContent,
    isLoading: reportLoading,
    error: reportError,
  } = useQuery(reportContentOptions(job?.report_url ?? null));

  if (jobLoading) return <Skeleton className="h-screen rounded-lg" />;
  if (!job) return <p className="text-muted-foreground">Job not found.</p>;

  return (
    <div className="flex h-full flex-col space-y-4">
      <div className="flex items-center gap-3">
        <Link to={`/jobs/${id}`} className="text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <h1 className="text-xl font-bold">Report</h1>
        <span className="font-mono text-xs text-muted-foreground">{job.job_id}</span>
        {analysis && analysis.citations.length > 0 && (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto"
            onClick={() => setCitationPanelOpen((p) => !p)}
          >
            <Columns2 className="mr-1 h-4 w-4" />
            Citations ({analysis.citations.length})
          </Button>
        )}
      </div>

      <div className={`flex flex-1 gap-4 overflow-hidden ${citationPanelOpen ? "flex-row" : ""}`}>
        <div className="flex-1 overflow-y-auto">
          <Tabs defaultValue="markdown">
            <TabsList>
              <TabsTrigger value="markdown">Markdown</TabsTrigger>
              <TabsTrigger value="insights">Insights</TabsTrigger>
            </TabsList>

            <TabsContent value="markdown" className="mt-4">
              {reportLoading ? (
                <Skeleton className="h-96 rounded-lg" />
              ) : reportError ? (
                <div className="flex items-center gap-2 rounded-lg border border-destructive p-4 text-sm text-destructive">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  {reportError instanceof Error
                    ? reportError.message
                    : "Failed to load report content."}
                </div>
              ) : reportContent ? (
                <Card>
                  <CardContent className="pt-6">
                    <MarkdownViewer content={reportContent} />
                  </CardContent>
                </Card>
              ) : (
                <p className="text-muted-foreground">Report content not available.</p>
              )}
            </TabsContent>

            <TabsContent value="insights" className="mt-4 space-y-4">
              {analysis?.insights.map((insight, i) => (
                <Card key={insight.id}>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">
                      {i + 1}. {insight.headline}
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-muted-foreground">{insight.detail}</p>
                  </CardContent>
                </Card>
              ))}
              {!analysis && <Skeleton className="h-32 rounded-lg" />}
            </TabsContent>
          </Tabs>
        </div>

        {citationPanelOpen && analysis && (
          <div className="w-80 shrink-0 overflow-y-auto rounded-lg border bg-card p-4">
            <h3 className="mb-3 text-sm font-semibold">Citations</h3>
            <CitationPanel citations={analysis.citations} />
          </div>
        )}
      </div>
    </div>
  );
}
