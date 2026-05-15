import { connect } from "net";
import { spawn, type Subprocess } from "bun";

const _UID = process.getuid?.() ?? process.pid;
const SOCKET_PATH = `/tmp/b12-embed-${_UID}.sock`;

const REQUEST_TIMEOUT = 4000;
const BATCH_TIMEOUT = 15000;
const BUFFER_SIZE = 1024 * 1024;

interface DaemonResponse {
  ok: boolean;
  error?: string;
  results?: Array<{ id: number; display: string; score: number }>;
  ranked_ids?: number[];
  embeddings?: string[];
  type?: string;
  confidence?: number;
  uptime?: number;
  requests_served?: number;
}

function socketRequest<T>(payload: object, timeout: number): Promise<T | null> {
  return new Promise((resolve) => {
    const socket = connect(SOCKET_PATH);
    let buffer = Buffer.alloc(0);
    let settled = false;

    const timer = setTimeout(() => {
      settled = true;
      socket.destroy();
      resolve(null);
    }, timeout);

    socket.on("data", (data: Buffer) => {
      buffer = Buffer.concat([buffer, data]);
      if (buffer.length > BUFFER_SIZE) {
        settled = true;
        clearTimeout(timer);
        socket.destroy();
        resolve(null);
        return;
      }

      const nlIdx = buffer.indexOf(0x0a);
      if (nlIdx === -1) return;

      settled = true;
      clearTimeout(timer);
      socket.destroy();

      const line = buffer.subarray(0, nlIdx).toString("utf-8");
      try {
        resolve(JSON.parse(line) as T);
      } catch {
        resolve(null);
      }
    });

    socket.on("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(null);
      }
    });

    socket.on("close", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(null);
      }
    });

    socket.write(JSON.stringify(payload) + "\n");
  });
}

export async function health(): Promise<{
  alive: boolean;
  uptime?: number;
  requests_served?: number;
}> {
  const res = await socketRequest<DaemonResponse>(
    { op: "health" },
    REQUEST_TIMEOUT,
  );
  if (!res || !res.ok) return { alive: false };
  return {
    alive: true,
    uptime: res.uptime,
    requests_served: res.requests_served,
  };
}

export async function semanticSearch(
  query: string,
  dbPath: string,
  limit: number = 10,
): Promise<Array<{ id: number; display: string; score: number }>> {
  const res = await socketRequest<DaemonResponse>(
    { op: "semantic_search", query, db_path: dbPath, limit },
    BATCH_TIMEOUT,
  );
  if (!res?.ok || !Array.isArray(res.results)) return [];
  return res.results;
}

export async function rerank(
  query: string,
  dbPath: string,
  ids: number[],
): Promise<number[]> {
  const res = await socketRequest<DaemonResponse>(
    { op: "rerank", query, db_path: dbPath, ids },
    REQUEST_TIMEOUT,
  );
  if (!res?.ok || !Array.isArray(res.ranked_ids)) return [];
  return res.ranked_ids;
}

export async function encodeBatch(
  texts: string[],
): Promise<string[]> {
  const res = await socketRequest<DaemonResponse>(
    { op: "encode_batch", texts },
    BATCH_TIMEOUT,
  );
  if (!res?.ok || !Array.isArray(res.embeddings)) return [];
  return res.embeddings;
}

export async function classify(
  text: string,
): Promise<{ type: string; confidence: number } | null> {
  const res = await socketRequest<DaemonResponse>(
    { op: "classify", text },
    REQUEST_TIMEOUT,
  );
  if (!res?.ok || !res.type) return null;
  return { type: res.type, confidence: res.confidence ?? 0 };
}

let _daemonProcess: Subprocess | null = null;

export async function startDaemon(
  venvPython: string,
  scriptPath: string,
): Promise<boolean> {
  const h = await health();
  if (h.alive) return true;

  try {
    _daemonProcess = spawn({
      cmd: [venvPython, scriptPath],
      stdout: "ignore",
      stderr: "ignore",
      detached: true,
    });

    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 500));
      const check = await health();
      if (check.alive) return true;
    }

    return false;
  } catch {
    return false;
  }
}
