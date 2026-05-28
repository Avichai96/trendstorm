import { useEffect, useRef, useCallback, useState } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import { SSEConnection, type SSEFrame } from "@/lib/sse";
import { api } from "@/api/client";
import type { StreamEvent } from "@/api/types.generated";

const SESSION_STORAGE_PREFIX = "sse_last_id_";
const MAX_RETRIES = 6;
const BASE_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

const TERMINAL_EVENTS = new Set(["report_ready", "job_failed", "job_rejected"]);

interface UseSSEOptions {
  jobId: string;
  enabled?: boolean;
  onEvent?: (event: StreamEvent) => void;
  onTerminal?: () => void;
}

export function useSSE({ jobId, enabled = true, onEvent, onTerminal }: UseSSEOptions) {
  const { getAccessTokenSilently } = useAuth0();
  const connRef = useRef<SSEConnection | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Keep a stable ref to the latest connect fn to use inside closures.
  const connectRef = useRef<() => Promise<void>>(async () => {});

  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const storageKey = `${SESSION_STORAGE_PREFIX}${jobId}`;

  const connect = useCallback(async () => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (connRef.current) {
      connRef.current.close();
      connRef.current = null;
    }

    let token: string;
    try {
      token = await getAccessTokenSilently();
    } catch {
      setError("Failed to get auth token");
      return;
    }

    const tenantId = sessionStorage.getItem("tenant_id");
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    if (tenantId) headers["X-Tenant-ID"] = tenantId;

    const lastEventId = sessionStorage.getItem(storageKey);
    const url = api.streamUrl(`/v1/jobs/${jobId}/stream`);

    const conn = new SSEConnection({
      url,
      headers,
      lastEventId,
      heartbeatTimeoutMs: 40_000,
      onError: (err) => {
        setConnected(false);
        setError(err instanceof Error ? err.message : "SSE connection error");

        if (retryCountRef.current < MAX_RETRIES) {
          const delay = Math.min(BASE_RETRY_MS * 2 ** retryCountRef.current, MAX_RETRY_MS);
          retryCountRef.current++;
          retryTimerRef.current = setTimeout(() => void connectRef.current(), delay);
        }
      },
    });

    conn.onFrame((frame: SSEFrame) => {
      if (!frame.data || frame.data === "") return;

      try {
        const event = JSON.parse(frame.data) as StreamEvent;

        if (frame.id) sessionStorage.setItem(storageKey, frame.id);

        // Reset backoff counter on successful frame.
        retryCountRef.current = 0;
        setError(null);

        onEvent?.(event);

        if (TERMINAL_EVENTS.has(event.event_type)) {
          conn.close();
          setConnected(false);
          onTerminal?.();
        }
      } catch {
        // malformed frame — ignore
      }
    });

    connRef.current = conn;
    conn.start();
    setConnected(true);
    setError(null);
  }, [jobId, getAccessTokenSilently, storageKey, onEvent, onTerminal]);

  // Keep connectRef in sync so retry closures always use the latest version.
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    if (!enabled) {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      connRef.current?.close();
      connRef.current = null;
      setConnected(false);
      return;
    }

    retryCountRef.current = 0;
    void connect();

    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      connRef.current?.close();
      connRef.current = null;
    };
  }, [enabled, connect]);

  return { connected, error, reconnect: connect };
}
