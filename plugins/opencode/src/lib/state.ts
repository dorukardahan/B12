import { renameSync, existsSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { randomUUID } from "node:crypto";

const MAX_ACTIVE_FILES = 20;
const MAX_MODIFIED_FILES = 15;
const MAX_SEARCH_PATTERNS = 10;
const FEEDBACK_MAX_LINES = 5000;
const FEEDBACK_TRIM_TO = 2500;

export interface WorkingMemory {
  active_files: string[];
  modified_files: string[];
  search_patterns: string[];
  session_id: string;
  updated_at: number;
}

export interface SessionState {
  startTime: number;
  project: string;
  cwd: string;
  setupContext: string;
  callCount: number;
  lastCheckpoint: number;
}

export interface FeedbackEntry {
  timestamp: number;
  session_id: string;
  type: string;
  data: Record<string, unknown>;
}

async function atomicWrite(filePath: string, content: string): Promise<void> {
  const dir = dirname(filePath);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.${randomUUID()}.tmp`;
  await Bun.write(tmpPath, content);
  renameSync(tmpPath, filePath);
}

async function atomicWriteJSON(filePath: string, data: unknown): Promise<void> {
  await atomicWrite(filePath, JSON.stringify(data, null, 2) + "\n");
}

export function createWorkingMemory(sessionId: string): WorkingMemory {
  return {
    active_files: [],
    modified_files: [],
    search_patterns: [],
    session_id: sessionId,
    updated_at: Date.now(),
  };
}

export function createSessionState(
  project: string,
  cwd: string,
  setupContext: string,
): SessionState {
  return {
    startTime: Date.now(),
    project,
    cwd,
    setupContext,
    callCount: 0,
    lastCheckpoint: Date.now(),
  };
}

export function addActiveFile(memory: WorkingMemory, filePath: string): WorkingMemory {
  const filtered = memory.active_files.filter((f) => f !== filePath);
  filtered.unshift(filePath);
  return {
    ...memory,
    active_files: filtered.slice(0, MAX_ACTIVE_FILES),
    updated_at: Date.now(),
  };
}

export function removeActiveFile(memory: WorkingMemory, filePath: string): WorkingMemory {
  return {
    ...memory,
    active_files: memory.active_files.filter((f) => f !== filePath),
    updated_at: Date.now(),
  };
}

export function addModifiedFile(memory: WorkingMemory, filePath: string): WorkingMemory {
  const filtered = memory.modified_files.filter((f) => f !== filePath);
  filtered.unshift(filePath);
  return {
    ...memory,
    modified_files: filtered.slice(0, MAX_MODIFIED_FILES),
    updated_at: Date.now(),
  };
}

export function addSearchPattern(memory: WorkingMemory, pattern: string): WorkingMemory {
  const filtered = memory.search_patterns.filter((p) => p !== pattern);
  filtered.unshift(pattern);
  return {
    ...memory,
    search_patterns: filtered.slice(0, MAX_SEARCH_PATTERNS),
    updated_at: Date.now(),
  };
}

export function bumpCallCount(session: SessionState): SessionState {
  return {
    ...session,
    callCount: session.callCount + 1,
    lastCheckpoint: Date.now(),
  };
}

export async function loadWorkingMemory(stagingDir: string): Promise<WorkingMemory | null> {
  const filePath = join(stagingDir, "working-memory.json");
  if (!existsSync(filePath)) return null;
  try {
    const file = Bun.file(filePath);
    const text = await file.text();
    return JSON.parse(text) as WorkingMemory;
  } catch {
    return null;
  }
}

export async function saveWorkingMemory(
  stagingDir: string,
  memory: WorkingMemory,
): Promise<void> {
  const filePath = join(stagingDir, "working-memory.json");
  await atomicWriteJSON(filePath, memory);
}

export async function appendFeedback(
  stagingDir: string,
  entry: FeedbackEntry,
): Promise<void> {
  const filePath = join(stagingDir, "feedback.jsonl");
  if (!existsSync(stagingDir)) {
    mkdirSync(stagingDir, { recursive: true });
  }
  const line = JSON.stringify(entry) + "\n";
  const file = Bun.file(filePath);
  let existing = "";
  if (existsSync(filePath)) {
    existing = await file.text();
  }
  const combined = existing + line;
  const lines = combined.split("\n").filter((l) => l.trim().length > 0);
  if (lines.length > FEEDBACK_MAX_LINES) {
    const trimmed = lines.slice(-FEEDBACK_TRIM_TO);
    await atomicWrite(filePath, trimmed.join("\n") + "\n");
  } else {
    await Bun.write(filePath, line, { create: true, append: true });
  }
}

export async function loadFeedback(
  stagingDir: string,
  limit?: number,
): Promise<FeedbackEntry[]> {
  const filePath = join(stagingDir, "feedback.jsonl");
  if (!existsSync(filePath)) return [];
  const file = Bun.file(filePath);
  const text = await file.text();
  const lines = text.split("\n").filter((l) => l.trim().length > 0);
  const selected = limit ? lines.slice(-limit) : lines;
  const entries: FeedbackEntry[] = [];
  for (const line of selected) {
    try {
      entries.push(JSON.parse(line) as FeedbackEntry);
    } catch {}
  }
  return entries;
}

export function getSessionDuration(session: SessionState): number {
  return Date.now() - session.startTime;
}

export function getSessionDurationFormatted(session: SessionState): string {
  const ms = getSessionDuration(session);
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

export function isMemoryStale(memory: WorkingMemory, thresholdMs: number): boolean {
  return Date.now() - memory.updated_at > thresholdMs;
}

export function clearStalePatterns(
  memory: WorkingMemory,
  maxAgeMs: number,
): WorkingMemory {
  const now = Date.now();
  return {
    ...memory,
    search_patterns: memory.search_patterns.filter(() => now - memory.updated_at < maxAgeMs),
    updated_at: memory.updated_at,
  };
}

export function mergeWorkingMemory(
  existing: WorkingMemory,
  incoming: WorkingMemory,
): WorkingMemory {
  const mergedFiles = [...new Set([...incoming.active_files, ...existing.active_files])];
  const mergedModified = [...new Set([...incoming.modified_files, ...existing.modified_files])];
  const mergedPatterns = [...new Set([...incoming.search_patterns, ...existing.search_patterns])];
  return {
    active_files: mergedFiles.slice(0, MAX_ACTIVE_FILES),
    modified_files: mergedModified.slice(0, MAX_MODIFIED_FILES),
    search_patterns: mergedPatterns.slice(0, MAX_SEARCH_PATTERNS),
    session_id: incoming.session_id,
    updated_at: Date.now(),
  };
}
