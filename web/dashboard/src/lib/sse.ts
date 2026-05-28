/**
 * Browser-native SSE client using the Fetch API.
 *
 * Why not native EventSource?  EventSource doesn't support custom headers
 * (like Authorization: Bearer ...).  We use fetch() + ReadableStream instead,
 * which gives us full header control and Last-Event-ID resumption.
 */

export interface SSEFrame {
  id: string | null;
  event: string | null;
  data: string;
}

export type SSEListener = (frame: SSEFrame) => void;

export interface SSEOptions {
  url: string;
  headers: Record<string, string>;
  lastEventId?: string | null;
  heartbeatTimeoutMs?: number;
  onError?: (err: unknown) => void;
}

export class SSEConnection {
  private _abortController: AbortController | null = null;
  private _lastEventId: string | null;
  private readonly _opts: SSEOptions;
  private readonly _listeners: SSEListener[] = [];

  constructor(opts: SSEOptions) {
    this._opts = opts;
    this._lastEventId = opts.lastEventId ?? null;
  }

  onFrame(listener: SSEListener) {
    this._listeners.push(listener);
    return this;
  }

  start() {
    this._connect();
    return this;
  }

  close() {
    this._abortController?.abort();
    this._abortController = null;
  }

  get lastEventId() {
    return this._lastEventId;
  }

  private async _connect() {
    this._abortController = new AbortController();
    const { url, headers, heartbeatTimeoutMs = 30_000 } = this._opts;

    const reqHeaders: Record<string, string> = { ...headers, Accept: "text/event-stream" };
    if (this._lastEventId != null) {
      reqHeaders["Last-Event-ID"] = this._lastEventId;
    }

    try {
      const res = await fetch(url, {
        headers: reqHeaders,
        signal: this._abortController.signal,
      });

      if (!res.ok || !res.body) {
        this._opts.onError?.(new Error(`SSE ${res.status}`));
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let id: string | null = null;
      let event: string | null = null;
      const dataLines: string[] = [];

      const readWithTimeout = async () => {
        return Promise.race([
          reader.read(),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error("heartbeat timeout")), heartbeatTimeoutMs)
          ),
        ]);
      };

      while (true) {
        const { value, done } = await readWithTimeout();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (line === "") {
            if (dataLines.length > 0) {
              const frame: SSEFrame = {
                id,
                event,
                data: dataLines.join("\n"),
              };
              if (id != null) this._lastEventId = id;
              this._listeners.forEach((fn) => fn(frame));
            }
            id = null;
            event = null;
            dataLines.length = 0;
          } else if (line.startsWith(":")) {
            // comment — heartbeat; reset timeout by continuing
          } else if (line.startsWith("id:")) {
            id = line.slice(3).trim();
          } else if (line.startsWith("event:")) {
            event = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
          }
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") return;
      this._opts.onError?.(err);
    }
  }
}
