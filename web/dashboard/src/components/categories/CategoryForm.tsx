import { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { createCategory, updateCategory, categoryKeys } from "@/api/queries/categories";
import type { Category } from "@/api/types.generated";

interface CategoryFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  category?: Category;
}

function extractErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "An unexpected error occurred.";
}

export function CategoryForm({ open, onOpenChange, category }: CategoryFormProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [keywords, setKeywords] = useState("");
  const qc = useQueryClient();
  const isEdit = !!category;

  useEffect(() => {
    if (open) {
      setName(category?.name ?? "");
      setDescription(category?.description ?? "");
      setKeywords(category?.keywords.join(", ") ?? "");
    }
  }, [open, category]);

  const parsedKeywords = keywords
    .split(",")
    .map((k) => k.trim())
    .filter(Boolean);

  const createMutation = useMutation({
    mutationFn: () =>
      createCategory({
        name: name.trim(),
        description: description.trim() || null,
        keywords: parsedKeywords,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: categoryKeys.all });
      onOpenChange(false);
    },
  });

  const updateMutation = useMutation({
    mutationFn: () =>
      updateCategory(category!.id, {
        description: description.trim() || null,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        keywords: parsedKeywords as any,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: categoryKeys.all });
      void qc.invalidateQueries({ queryKey: categoryKeys.detail(category!.id) });
      onOpenChange(false);
    },
  });

  const mutation = isEdit ? updateMutation : createMutation;

  function handleClose(nextOpen: boolean) {
    if (!mutation.isPending) {
      onOpenChange(nextOpen);
      mutation.reset();
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    mutation.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit Category" : "New Category"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update the category's description and keywords."
              : "Create a new trend intelligence category."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          {!isEdit && (
            <div className="space-y-1">
              <Label htmlFor="cat-name">Name *</Label>
              <Input
                id="cat-name"
                placeholder="e.g. AI Safety"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                disabled={mutation.isPending}
              />
            </div>
          )}
          <div className="space-y-1">
            <Label htmlFor="cat-description">Description</Label>
            <Textarea
              id="cat-description"
              placeholder="What trends should this category track?"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={mutation.isPending}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="cat-keywords">Keywords</Label>
            <Input
              id="cat-keywords"
              placeholder="LLM safety, alignment, red-teaming"
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              disabled={mutation.isPending}
            />
            <p className="text-xs text-muted-foreground">Comma-separated keywords for retrieval.</p>
          </div>
          {mutation.isError && (
            <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {extractErrorMessage(mutation.error)}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => handleClose(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={mutation.isPending || (!isEdit && !name.trim())}
            >
              {mutation.isPending
                ? isEdit
                  ? "Saving…"
                  : "Creating…"
                : isEdit
                  ? "Save"
                  : "Create"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
