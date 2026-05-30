import { useParams, Link } from "react-router-dom";
import { useState } from "react";
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  categoryDetailOptions,
  archiveCategory,
  unarchiveCategory,
  categoryKeys,
} from "@/api/queries/categories";
import { sourcesByCategoryOptions } from "@/api/queries/sources";
import { jobsListOptions } from "@/api/queries/jobs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { JobStatusBadge } from "@/components/shared/StatusBadge";
import { CategoryForm } from "@/components/categories/CategoryForm";
import { SourceForm, DeleteSourceConfirm } from "@/components/sources/SourceForm";
import { ArrowLeft, ExternalLink, Pencil, Plus, Archive, ArchiveRestore, Trash2 } from "lucide-react";
import { formatRelative } from "@/lib/utils";
import type { Source } from "@/api/types.generated";

function safeHostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export default function CategoryDetail() {
  const { id } = useParams<{ id: string }>();
  const [showEditForm, setShowEditForm] = useState(false);
  const [showSourceForm, setShowSourceForm] = useState(false);
  const [deletingSource, setDeletingSource] = useState<Source | null>(null);
  const qc = useQueryClient();

  const { data: category, isLoading: catLoading } = useQuery(categoryDetailOptions(id!));
  const { data: sourcesData, isLoading: srcLoading } = useQuery(sourcesByCategoryOptions(id!));
  const { data: jobsData } = useInfiniteQuery(jobsListOptions(undefined, id!));

  const archiveMutation = useMutation({
    mutationFn: () =>
      category?.archived ? unarchiveCategory(id!) : archiveCategory(id!),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: categoryKeys.all });
      void qc.invalidateQueries({ queryKey: categoryKeys.detail(id!) });
    },
  });

  if (catLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 rounded-lg" />
      </div>
    );
  }

  if (!category) return <p className="text-muted-foreground">Category not found.</p>;

  const sources = sourcesData?.items ?? [];
  const recentJobs = jobsData?.pages.flatMap((p) => p.items).slice(0, 10) ?? [];

  return (
    <div className="space-y-6">
      <CategoryForm open={showEditForm} onOpenChange={setShowEditForm} category={category} />
      <SourceForm open={showSourceForm} onOpenChange={setShowSourceForm} categoryId={id!} />
      <DeleteSourceConfirm
        source={deletingSource}
        categoryId={id!}
        onClose={() => setDeletingSource(null)}
      />

      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link to="/categories" className="text-muted-foreground hover:text-foreground">
            <ArrowLeft className="h-5 w-5" />
          </Link>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-bold">{category.name}</h1>
              {category.archived && <Badge variant="muted">Archived</Badge>}
            </div>
            {category.description && (
              <p className="mt-0.5 text-sm text-muted-foreground">{category.description}</p>
            )}
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowEditForm(true)}
          >
            <Pencil className="mr-1 h-3.5 w-3.5" />
            Edit
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => archiveMutation.mutate()}
            disabled={archiveMutation.isPending}
          >
            {category.archived ? (
              <>
                <ArchiveRestore className="mr-1 h-3.5 w-3.5" />
                Unarchive
              </>
            ) : (
              <>
                <Archive className="mr-1 h-3.5 w-3.5" />
                Archive
              </>
            )}
          </Button>
        </div>
      </div>

      {category.keywords.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {category.keywords.map((kw) => (
            <Badge key={kw} variant="secondary">{kw}</Badge>
          ))}
        </div>
      )}

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                Sources ({sources.length})
              </CardTitle>
              <Button size="sm" variant="outline" onClick={() => setShowSourceForm(true)}>
                <Plus className="mr-1 h-3.5 w-3.5" />
                Add Source
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-2">
            {srcLoading && <Skeleton className="h-20" />}
            {sources.map((src) => (
              <div
                key={src.id}
                className="flex items-start justify-between gap-2 rounded-md border p-2 text-sm"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium">{src.label ?? safeHostname(src.url)}</p>
                  <a
                    href={src.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1 truncate text-xs text-muted-foreground hover:text-primary"
                  >
                    <ExternalLink className="h-3 w-3 shrink-0" />
                    {src.url}
                  </a>
                </div>
                <div className="shrink-0 flex items-start gap-1">
                  <div className="space-y-1 text-right">
                    <Badge variant="outline" className="text-xs">{src.type}</Badge>
                    {src.last_fetch_status && (
                      <Badge
                        variant={
                          src.last_fetch_status === "ok"
                            ? "success"
                            : src.last_fetch_status === "error"
                              ? "destructive"
                              : "muted"
                        }
                        className="text-xs"
                      >
                        {src.last_fetch_status}
                      </Badge>
                    )}
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 text-muted-foreground hover:text-destructive"
                    onClick={() => setDeletingSource(src)}
                    aria-label="Disable source"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
            {!srcLoading && sources.length === 0 && (
              <div className="flex flex-col items-center gap-2 py-6 text-center">
                <p className="text-sm text-muted-foreground">No sources registered yet.</p>
                <Button size="sm" variant="outline" onClick={() => setShowSourceForm(true)}>
                  <Plus className="mr-1 h-3.5 w-3.5" />
                  Add Source
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Recent Jobs
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {recentJobs.map((job) => (
              <Link
                key={job.id}
                to={`/jobs/${job.id}`}
                className="flex items-center justify-between rounded-md border p-2 text-sm hover:bg-accent"
              >
                <div>
                  <JobStatusBadge status={job.status} />
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {formatRelative(job.created_at)}
                  </p>
                </div>
                <span className="text-xs font-mono text-muted-foreground">
                  {job.metrics.documents_ingested} docs
                </span>
              </Link>
            ))}
            {recentJobs.length === 0 && (
              <p className="text-sm text-muted-foreground">No jobs yet.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
