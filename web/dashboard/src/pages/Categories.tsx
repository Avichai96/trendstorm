import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState, useEffect } from "react";
import { categoriesListOptions } from "@/api/queries/categories";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { LoadMoreButton } from "@/components/shared/CursorPagination";
import { CategoryForm } from "@/components/categories/CategoryForm";
import { FolderOpen, Plus, Search } from "lucide-react";
import { formatRelative } from "@/lib/utils";

export default function Categories() {
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [showForm, setShowForm] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 400);
    return () => clearTimeout(t);
  }, [search]);

  const { data, isLoading, isFetching, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useInfiniteQuery(categoriesListOptions(false, debouncedSearch || undefined));

  const categories = data?.pages.flatMap((p) => p.items) ?? [];
  const totalLoaded = categories.length;

  return (
    <div className="space-y-6">
      <CategoryForm open={showForm} onOpenChange={setShowForm} />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Categories</h1>
          <p className="text-sm text-muted-foreground">
            Trend intelligence categories and their sources
          </p>
        </div>
        <div className="flex items-center gap-3">
          {!isLoading && totalLoaded > 0 && (
            <span className="text-sm text-muted-foreground">
              {totalLoaded} loaded{hasNextPage ? "+" : ""}
            </span>
          )}
          <Button size="sm" onClick={() => setShowForm(true)}>
            <Plus className="mr-1 h-4 w-4" />
            New Category
          </Button>
        </div>
      </div>

      <div className="relative max-w-sm">
        <Search
          className={`absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 transition-colors ${
            isFetching && !isLoading ? "text-primary animate-pulse" : "text-muted-foreground"
          }`}
        />
        <Input
          placeholder="Search categories…"
          className="pl-9"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search categories"
        />
      </div>

      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-36 rounded-lg" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {categories.map((cat) => (
              <Link key={cat.id} to={`/categories/${cat.id}`}>
                <Card className="h-full transition-shadow hover:shadow-md">
                  <CardHeader className="pb-2">
                    <div className="flex items-start justify-between gap-2">
                      <CardTitle className="text-base">{cat.name}</CardTitle>
                      {cat.archived && <Badge variant="muted">Archived</Badge>}
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    <div className="flex flex-wrap gap-1">
                      {cat.keywords.slice(0, 4).map((kw: string) => (
                        <Badge key={kw} variant="secondary" className="text-xs">
                          {kw}
                        </Badge>
                      ))}
                      {cat.keywords.length > 4 && (
                        <Badge variant="outline" className="text-xs">
                          +{cat.keywords.length - 4}
                        </Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Updated {formatRelative(cat.updated_at)}
                    </p>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>

          {categories.length === 0 && (
            <div className="flex flex-col items-center gap-3 py-16 text-center">
              <FolderOpen className="h-12 w-12 text-muted-foreground/40" />
              <p className="font-medium">No categories found</p>
              <p className="text-sm text-muted-foreground">
                {debouncedSearch
                  ? "Try a different search term."
                  : "Create your first category to get started."}
              </p>
              {!debouncedSearch && (
                <Button size="sm" onClick={() => setShowForm(true)}>
                  <Plus className="mr-1 h-4 w-4" />
                  New Category
                </Button>
              )}
            </div>
          )}

          <LoadMoreButton
            hasNextPage={hasNextPage}
            isFetchingNextPage={isFetchingNextPage}
            fetchNextPage={fetchNextPage}
          />
        </>
      )}
    </div>
  );
}
