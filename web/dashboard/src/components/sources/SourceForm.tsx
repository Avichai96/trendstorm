import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { registerSource, deleteSource, sourceKeys } from "@/api/queries/sources";
import type { Source, SourceType } from "@/api/types.generated";

function extractErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "An unexpected error occurred.";
}

// ─── Register source dialog ───────────────────────────────────────────────────

interface SourceFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  categoryId: string;
}

export function SourceForm({ open, onOpenChange, categoryId }: SourceFormProps) {
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [type, setType] = useState<SourceType>("http");
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      registerSource({
        category_id: categoryId,
        url: url.trim(),
        label: label.trim() || null,
        type,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: sourceKeys.byCategory(categoryId) });
      setUrl("");
      setLabel("");
      setType("http");
      onOpenChange(false);
    },
  });

  function handleClose(nextOpen: boolean) {
    if (!mutation.isPending) {
      onOpenChange(nextOpen);
      mutation.reset();
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Register Source</DialogTitle>
          <DialogDescription>Add a new URL source to this category.</DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (url.trim()) mutation.mutate();
          }}
          className="space-y-4"
        >
          <div className="space-y-1">
            <Label htmlFor="src-url">URL *</Label>
            <Input
              id="src-url"
              type="url"
              placeholder="https://example.com/feed.rss"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              disabled={mutation.isPending}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="src-label">Label</Label>
            <Input
              id="src-label"
              placeholder="Optional display name"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              disabled={mutation.isPending}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="src-type">Type</Label>
            <Select
              value={type}
              onValueChange={(v) => setType(v as SourceType)}
              disabled={mutation.isPending}
            >
              <SelectTrigger id="src-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="http">HTTP</SelectItem>
                <SelectItem value="rss">RSS</SelectItem>
                <SelectItem value="api">API</SelectItem>
                <SelectItem value="sitemap">Sitemap</SelectItem>
              </SelectContent>
            </Select>
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
            <Button type="submit" disabled={mutation.isPending || !url.trim()}>
              {mutation.isPending ? "Registering…" : "Register"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ─── Delete / disable confirmation dialog ────────────────────────────────────

interface DeleteSourceConfirmProps {
  source: Source | null;
  categoryId: string;
  onClose: () => void;
}

export function DeleteSourceConfirm({ source, categoryId, onClose }: DeleteSourceConfirmProps) {
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => deleteSource(source!.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: sourceKeys.byCategory(categoryId) });
      onClose();
    },
  });

  const displayLabel = source?.label ?? source?.url ?? "";

  return (
    <Dialog
      open={source !== null}
      onOpenChange={(o) => {
        if (!o && !mutation.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Disable source?</DialogTitle>
          <DialogDescription>
            <strong>{displayLabel}</strong> will be disabled and excluded from future jobs.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError && (
          <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {extractErrorMessage(mutation.error)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? "Disabling…" : "Disable"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
