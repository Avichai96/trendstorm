import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiKeysListOptions, apiKeyKeys, createApiKey, revokeApiKey } from "@/api/queries/api_keys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Key, Plus, Copy, Check, Trash2 } from "lucide-react";
import { formatRelative } from "@/lib/utils";
import type { ApiKeyCreated } from "@/api/types.generated";

function extractErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "An unexpected error occurred.";
}

// ─── One-time key reveal dialog ───────────────────────────────────────────────

function NewKeyReveal({
  created,
  onClose,
}: {
  created: ApiKeyCreated | null;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  function copyKey() {
    if (!created) return;
    void navigator.clipboard.writeText(created.key).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <Dialog open={created !== null} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>API Key Created</DialogTitle>
          <DialogDescription>
            Copy your new key now. It will <strong>not</strong> be shown again.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>Key name</Label>
            <p className="text-sm font-medium">{created?.name}</p>
          </div>
          <div className="space-y-1">
            <Label>Secret key</Label>
            <div className="flex items-center gap-2">
              <code className="flex-1 rounded-md bg-muted px-3 py-2 text-xs font-mono break-all select-all">
                {created?.key}
              </code>
              <Button variant="outline" size="icon" onClick={copyKey} aria-label="Copy key">
                {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
          </div>
          <Button className="w-full" onClick={onClose}>
            Done — I've saved the key
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ─── Create key form ──────────────────────────────────────────────────────────

function CreateKeyForm({ onCreated }: { onCreated: (k: ApiKeyCreated) => void }) {
  const [name, setName] = useState("");
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => createApiKey({ name: name.trim() }),
    onSuccess: (data) => {
      void qc.invalidateQueries({ queryKey: apiKeyKeys.all });
      onCreated(data);
      setName("");
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Create New Key
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) mutation.mutate();
          }}
          className="flex items-end gap-3"
        >
          <div className="flex-1 space-y-1">
            <Label htmlFor="key-name">Key name</Label>
            <Input
              id="key-name"
              placeholder="e.g. CI pipeline"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={mutation.isPending}
            />
          </div>
          <Button type="submit" disabled={mutation.isPending || !name.trim()}>
            <Plus className="mr-1 h-4 w-4" />
            {mutation.isPending ? "Creating…" : "Create"}
          </Button>
        </form>
        {mutation.isError && (
          <p className="mt-2 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {extractErrorMessage(mutation.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Revoke confirmation dialog ───────────────────────────────────────────────

function RevokeConfirm({
  keyId,
  keyName,
  onClose,
}: {
  keyId: string | null;
  keyName: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => revokeApiKey(keyId!),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: apiKeyKeys.all });
      onClose();
    },
  });

  return (
    <Dialog
      open={keyId !== null}
      onOpenChange={(o) => {
        if (!o && !mutation.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Revoke API key?</DialogTitle>
          <DialogDescription>
            <strong>{keyName}</strong> will be permanently revoked. Any services using this key will
            lose access immediately.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError && (
          <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {extractErrorMessage(mutation.error)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? "Revoking…" : "Revoke"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function ApiKeys() {
  const [newKey, setNewKey] = useState<ApiKeyCreated | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [revokingName, setRevokingName] = useState("");

  const { data: keys = [], isLoading } = useQuery(apiKeysListOptions());
  const activeKeys = keys.filter((k) => k.is_active);
  const revokedKeys = keys.filter((k) => !k.is_active);

  function startRevoke(id: string, name: string) {
    setRevokingId(id);
    setRevokingName(name);
  }

  return (
    <div className="space-y-6">
      <NewKeyReveal created={newKey} onClose={() => setNewKey(null)} />
      <RevokeConfirm
        keyId={revokingId}
        keyName={revokingName}
        onClose={() => setRevokingId(null)}
      />

      <div>
        <h1 className="text-2xl font-bold">API Keys</h1>
        <p className="text-sm text-muted-foreground">
          Manage API keys for programmatic access to TrendStorm.
        </p>
      </div>

      <CreateKeyForm onCreated={setNewKey} />

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Active Keys ({activeKeys.length})
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {isLoading && <Skeleton className="h-24" />}
          {!isLoading && activeKeys.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <Key className="h-8 w-8 text-muted-foreground/40" />
              <p className="text-sm text-muted-foreground">No active API keys.</p>
            </div>
          )}
          {activeKeys.map((key) => (
            <div
              key={key.id}
              className="flex items-center justify-between rounded-md border p-3 text-sm"
            >
              <div className="space-y-0.5">
                <p className="font-medium">{key.name}</p>
                <code className="text-xs text-muted-foreground font-mono">
                  {key.key_prefix}••••••••
                </code>
                <p className="text-xs text-muted-foreground">
                  Created {formatRelative(key.created_at)}
                  {key.last_used_at && ` · Last used ${formatRelative(key.last_used_at)}`}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant="success" className="text-xs">Active</Badge>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-muted-foreground hover:text-destructive"
                  onClick={() => startRevoke(key.id, key.name)}
                  aria-label="Revoke key"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {revokedKeys.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Revoked Keys ({revokedKeys.length})
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {revokedKeys.map((key) => (
              <div
                key={key.id}
                className="flex items-center justify-between rounded-md border p-3 text-sm opacity-60"
              >
                <div className="space-y-0.5">
                  <p className="font-medium line-through">{key.name}</p>
                  <code className="text-xs text-muted-foreground font-mono">
                    {key.key_prefix}••••••••
                  </code>
                  <p className="text-xs text-muted-foreground">
                    Revoked {key.revoked_at ? formatRelative(key.revoked_at) : ""}
                  </p>
                </div>
                <Badge variant="muted" className="text-xs">Revoked</Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
