import { Button } from "@/components/ui/button";
import { ChevronDown } from "lucide-react";

interface CursorPaginationProps {
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
}

export function LoadMoreButton({ hasNextPage, isFetchingNextPage, fetchNextPage }: CursorPaginationProps) {
  if (!hasNextPage) return null;
  return (
    <div className="flex justify-center pt-4">
      <Button variant="outline" onClick={fetchNextPage} disabled={isFetchingNextPage}>
        <ChevronDown className="mr-2 h-4 w-4" />
        {isFetchingNextPage ? "Loading…" : "Load more"}
      </Button>
    </div>
  );
}
