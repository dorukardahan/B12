import Database from "better-sqlite3";
import { effectiveSearchMode, type SearchMode } from "./search-mode.js";
import { createHash } from "crypto";
import { homedir } from "os";
import { join } from "path";
import { platform } from "process";
import { readdirSync, existsSync, mkdirSync, readFileSync, appendFileSync } from "fs";

export interface MemoryRow {
  id: number;
  content: string;
  content_hash: string;
  memory_type: string;
  tags: string;
  metadata: string;
  created_at: number;
  updated_at: number;
  created_at_iso: string | null;
  updated_at_iso: string | null;
  deleted_at: number | null;
  strength: number;
  last_accessed_at: number | null;
  valid_until: string | null;
  difficulty: number;
  due_date: string | null;
}

export interface ScoredMemory {
  row: MemoryRow;
  score: number;
}

export interface GraphEdge {
  source_hash: string;
  target_hash: string;
  similarity: number;
  connection_types: string;
  metadata: string | null;
  created_at: number;
  relationship_type: string;
}

export interface SearchResult {
  id: number;
  display: string;
  score: number;
}

export interface StoreOptions {
  content: string;
  tags?: string | string[];
  memory_type?: string;
  metadata?: Record<string, unknown>;
  embedding?: string | Uint8Array | null;
  valid_until?: string | null;
}

export interface SearchOptions {
  query?: string;
  mode?: SearchMode;
  tags?: string | string[];
  limit?: number;
  after?: string | null;
  before?: string | null;
  stemmed?: boolean;
  maxResponseChars?: number;
  boost?: boolean;
}

export interface SessionContextResult {
  projectMemories: Array<{ content: string; memory_type: string }>;
  universalMemories: Array<{ content: string; memory_type: string }>;
  lastSessionSummary: string | null;
  userProfile: string | null;
}

export function getDbPath(): string {
  const home = homedir();
  if (platform === "darwin") {
    return join(home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db");
  }
  if (platform === "win32") {
    return join(home, "AppData", "Local", "mcp-memory", "sqlite_vec.db");
  }
  return join(home, ".local", "share", "mcp-memory", "sqlite_vec.db");
}

export function computeContentHash(content: string): string {
  return createHash("sha256").update(content.trim().toLowerCase()).digest("hex");
}

export function normalizeTags(tags: string | string[] | null | undefined): string {
  if (tags == null) return "";
  if (Array.isArray(tags)) return tags.filter((t) => t).join(",");
  return String(tags);
}

function escapeLike(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_");
}

function tagPredicate(column: string): string {
  const normalized = `replace(replace(COALESCE(${column}, ''), ', ', ','), ' ,', ',')`;
  return `(',' || ${normalized} || ',') LIKE ? ESCAPE '\\'`;
}

function tagParam(tag: string): string {
  return `%,${escapeLike(tag.trim())},%`;
}

function nowTs(): [number, string] {
  const ts = Math.floor(Date.now() / 1000);
  const iso = new Date(ts * 1000).toISOString();
  return [ts, iso];
}

function isExpiredValidUntil(value: string | null | undefined): boolean {
  if (!value) return false;
  const ts = Date.parse(value);
  return !Number.isNaN(ts) && ts <= Date.now();
}

function activeValidUntilPredicate(column: string = "valid_until"): string {
  return `(${column} IS NULL OR datetime(${column}) > datetime('now'))`;
}

function validateMetadata(value: unknown): string {
  if (value == null) return "{}";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "string") {
    const s = value.trim();
    if (!s) return "{}";
    try {
      JSON.parse(s);
      return s;
    } catch {
      return "{}";
    }
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "{}";
  }
}

function unifiedScore(row: MemoryRow, relevance: number): number {
  const nowMs = Date.now();
  const nowTs = Math.floor(nowMs / 1000);
  const accessed = row.last_accessed_at ?? row.created_at ?? nowTs;
  const ageDays = Math.max((nowTs - accessed) / 86400.0, 0.001);
  let strength = row.strength ?? 1.0;
  if (strength <= 0) strength = 0.01;
  const decay = Math.max(1 / (1 + ageDays / (9 * strength)), 0.01);

  let meta: Record<string, unknown> = {};
  try {
    meta = JSON.parse(row.metadata || "{}");
  } catch {
    // leave empty
  }
  const importance = Math.min(
    (Number(meta.importance_score) || 1.0) / 2.0,
    1.0,
  );

  return 0.3 * decay + 0.3 * importance + 0.4 * relevance;
}

function formatMemory(row: MemoryRow, score?: number): string {
  const parts = [
    `[${row.memory_type || "general"}] ${row.content.slice(0, 500)}`,
  ];
  if (row.tags) parts.push(`  Tags: ${row.tags}`);
  parts.push(
    `  Hash: ${row.content_hash}  Created: ${row.created_at_iso || "?"}`,
  );
  if (score !== undefined) parts.push(`  Score: ${score.toFixed(3)}`);
  return parts.join("\n");
}

export class B12Database {
  private db: Database.Database;

  constructor(dbPath?: string) {
    const path = dbPath || getDbPath();
    const dir = join(path, "..");
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });

    this.db = new Database(path, { timeout: 30000 });
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("busy_timeout = 30000");
    this.db.pragma("wal_autocheckpoint = 100");
  }

  close(): void {
    this.db.close();
  }

  get raw(): Database.Database {
    return this.db;
  }

  store(options: StoreOptions): { hash: string; id: number } {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = this.storeLocked(options);
      this.db.exec("COMMIT");
      return result;
    } catch (err) {
      try {
        this.db.exec("ROLLBACK");
      } catch {}
      throw err;
    }
  }

  private storeLocked(options: StoreOptions): { hash: string; id: number } {
    const { content, valid_until } = options;
    const tags = normalizeTags(options.tags);
    const requestedMemoryType = options.memory_type;
    const memoryType = requestedMemoryType || "general";
    const contentHash = computeContentHash(content);
    const [ts, iso] = nowTs();

    const defaultMeta: Record<string, unknown> = {
      quality_score: 0.5,
      quality_provider: "implicit",
      access_count: 0,
      source_type: "user",
      credibility: 1.0,
    };
    const explicitMeta = options.metadata || {};
    const metaJson = validateMetadata({ ...defaultMeta, ...explicitMeta });

    const existing = this.db
      .prepare("SELECT id, deleted_at, tags, metadata, valid_until FROM memories WHERE content_hash = ?")
      .get(contentHash) as { id: number; deleted_at: number | null; tags?: string; metadata?: string; valid_until?: string | null } | undefined;

    if (existing && existing.deleted_at !== null) {
      this.db
        .prepare(
          `UPDATE memories SET deleted_at = NULL, strength = 1.0,
           tags = ?, memory_type = ?, metadata = ?,
           updated_at = ?, updated_at_iso = ?, valid_until = ?
           WHERE content_hash = ?`,
        )
        .run(
          tags,
          memoryType,
          metaJson,
          ts,
          iso,
          valid_until ?? null,
          contentHash,
        );
    } else if (existing) {
      const mergedTags = normalizeTags(Array.from(new Set([
        ...normalizeTags(existing.tags).split(","),
        ...tags.split(","),
      ])))
      let existingMeta: Record<string, unknown> = {}
      try {
        existingMeta = existing.metadata ? JSON.parse(existing.metadata) : {}
      } catch {}
      const mergedMeta = validateMetadata({ ...defaultMeta, ...existingMeta, ...explicitMeta })
      const nextValidUntil =
        valid_until !== undefined
          ? valid_until
          : isExpiredValidUntil(existing.valid_until)
            ? null
            : existing.valid_until ?? null
      this.db
        .prepare(
          `UPDATE memories SET tags = ?, metadata = ?,
           memory_type = COALESCE(?, memory_type),
           updated_at = ?, updated_at_iso = ?, valid_until = ?
           WHERE content_hash = ?`,
        )
        .run(mergedTags, mergedMeta, requestedMemoryType ?? null, ts, iso, nextValidUntil, contentHash);
    } else {
      this.db
        .prepare(
          `INSERT OR IGNORE INTO memories
           (content_hash, content, tags, memory_type, metadata,
            strength, created_at, created_at_iso, updated_at, updated_at_iso,
            valid_until)
           VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)`,
        )
        .run(
          contentHash,
          content,
          tags,
          memoryType,
          metaJson,
          ts,
          iso,
          ts,
          iso,
          valid_until ?? null,
        );
    }

    const row = this.db
      .prepare("SELECT id FROM memories WHERE content_hash = ?")
      .get(contentHash) as { id: number } | undefined;

    if (!row) {
      throw new Error("memory store failed: row was not created");
    }
    if (row && options.embedding) this.storeEmbedding(row.id, options.embedding);
    return { hash: contentHash, id: row.id };
  }

  storeEmbedding(memoryId: number, embedding: string | Uint8Array): void {
    const blob = typeof embedding === "string" ? Buffer.from(embedding, "base64") : embedding;
    try {
      const exists = this.db
        .prepare("SELECT 1 FROM memory_embeddings WHERE rowid = ? LIMIT 1")
        .get(memoryId);
      if (exists) {
        this.db
          .prepare("UPDATE memory_embeddings SET content_embedding = ? WHERE rowid = ?")
          .run(blob, memoryId);
      } else {
        this.db
          .prepare("INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)")
          .run(memoryId, blob);
      }
    } catch {
      // Lightweight installs may not have the vec table yet; backfill can handle it.
    }
  }

  search(options: SearchOptions = {}): SearchResult[] {
    const {
      query = "",
      mode = "hybrid",
      limit = 10,
      stemmed = false,
      maxResponseChars = 40000,
    } = options;
    const boost = options.boost ?? true;
    const tagList = normalizeTags(options.tags)
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

    const wheres: string[] = [
      "m.deleted_at IS NULL",
      activeValidUntilPredicate("m.valid_until"),
    ];
    const params: unknown[] = [];

    for (const t of tagList) {
      wheres.push(tagPredicate("m.tags"));
      params.push(tagParam(t));
    }

    if (options.after) {
      const ts = Math.floor(new Date(options.after).getTime() / 1000);
      if (!isNaN(ts)) {
        wheres.push("m.created_at >= ?");
        params.push(ts);
      }
    }
    if (options.before) {
      const ts = Math.floor(new Date(options.before).getTime() / 1000);
      if (!isNaN(ts)) {
        wheres.push("m.created_at <= ?");
        params.push(ts);
      }
    }

    const whereSql = wheres.join(" AND ");
    const results = new Map<string, { row: MemoryRow; score: number }>();

    const searchMode = effectiveSearchMode(mode);

    if (searchMode === "exact" && query) {
      const rows = this.db
        .prepare(
          `SELECT * FROM memories m
           WHERE m.content LIKE ? ESCAPE '\\' AND ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`,
        )
        .all(`%${escapeLike(query)}%`, ...params, limit) as MemoryRow[];

      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: unifiedScore(r, 0.9) });
      }
    }

    if (searchMode === "hybrid" && query) {
      const ftsTable = stemmed
        ? "memory_fts_stemmed"
        : "memory_content_fts";

      for (const ftsAttempt of ["phrase", "or"] as const) {
        try {
          let ftsQuery: string;
          if (ftsAttempt === "phrase") {
            ftsQuery = '"' + query.replace(/"/g, '""') + '"';
          } else {
            const words = query
              .split(/\s+/)
              .map((w) => w.trim())
              .filter((w) => w.length > 1);
            if (!words.length) break;
            ftsQuery = words
              .map((w) => '"' + w.replace(/"/g, '""') + '"')
              .join(" OR ");
          }

          const ftsRows = this.db
            .prepare(
              `SELECT m.*, rank
               FROM ${ftsTable} fts
               JOIN memories m ON m.id = fts.rowid
               WHERE fts.content MATCH ? AND ${whereSql}
               ORDER BY rank LIMIT ?`,
            )
            .all(ftsQuery, ...params, limit) as (MemoryRow & { rank: number })[];

          for (const r of ftsRows) {
            const bonus = ftsAttempt === "phrase" ? 0.1 : 0.0;
            const rawRelevance = Math.min(Math.abs(r.rank) / 20.0, 1.0) + bonus;
            const score = unifiedScore(r, rawRelevance);
            const existing = results.get(r.content_hash);
            if (!existing || existing.score < score) {
              results.set(r.content_hash, { row: r, score });
            }
          }
        } catch {
          continue;
        }
      }
    }

    if (!query) {
      const rows = this.db
        .prepare(
          `SELECT * FROM memories m
           WHERE ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`,
        )
        .all(...params, limit) as MemoryRow[];
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: 0.5 });
      }
    }

    const sorted = [...results.values()]
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);

    if (boost && query && sorted.length > 0) {
      this.boostStrength(
        sorted.map((s) => s.row.id),
      );
    }

    return sorted.map(({ row, score }) => ({
      id: row.id,
      display: `[${row.memory_type || "general"}] ${row.content.slice(0, 300).replace(/\n/g, " ")}`,
      score,
    }));
  }

  searchFormatted(options: SearchOptions = {}): string {
    const { limit = 10, maxResponseChars = 40000 } = options;
    const boost = options.boost ?? true;
    const tagList = normalizeTags(options.tags)
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

    const wheres: string[] = [
      "m.deleted_at IS NULL",
      activeValidUntilPredicate("m.valid_until"),
    ];
    const params: unknown[] = [];

    for (const t of tagList) {
      wheres.push(tagPredicate("m.tags"));
      params.push(tagParam(t));
    }
    if (options.after) {
      const ts = Math.floor(new Date(options.after).getTime() / 1000);
      if (!isNaN(ts)) {
        wheres.push("m.created_at >= ?");
        params.push(ts);
      }
    }
    if (options.before) {
      const ts = Math.floor(new Date(options.before).getTime() / 1000);
      if (!isNaN(ts)) {
        wheres.push("m.created_at <= ?");
        params.push(ts);
      }
    }

    const whereSql = wheres.join(" AND ");
    const results = new Map<string, { row: MemoryRow; score: number }>();
    const query = options.query || "";
    const mode = effectiveSearchMode(options.mode || "hybrid");
    const stemmed = options.stemmed ?? false;

    if (mode === "exact" && query) {
      const rows = this.db
        .prepare(
          `SELECT * FROM memories m
           WHERE m.content LIKE ? ESCAPE '\\' AND ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`,
        )
        .all(`%${escapeLike(query)}%`, ...params, limit) as MemoryRow[];
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: unifiedScore(r, 0.9) });
      }
    }

    if (mode === "hybrid" && query) {
      const ftsTable = stemmed
        ? "memory_fts_stemmed"
        : "memory_content_fts";
      for (const ftsAttempt of ["phrase", "or"] as const) {
        try {
          let ftsQuery: string;
          if (ftsAttempt === "phrase") {
            ftsQuery = '"' + query.replace(/"/g, '""') + '"';
          } else {
            const words = query
              .split(/\s+/)
              .map((w) => w.trim())
              .filter((w) => w.length > 1);
            if (!words.length) break;
            ftsQuery = words
              .map((w) => '"' + w.replace(/"/g, '""') + '"')
              .join(" OR ");
          }
          const ftsRows = this.db
            .prepare(
              `SELECT m.*, rank
               FROM ${ftsTable} fts
               JOIN memories m ON m.id = fts.rowid
               WHERE fts.content MATCH ? AND ${whereSql}
               ORDER BY rank LIMIT ?`,
            )
            .all(ftsQuery, ...params, limit) as (MemoryRow & { rank: number })[];
          for (const r of ftsRows) {
            const bonus = ftsAttempt === "phrase" ? 0.1 : 0.0;
            const rawRel = Math.min(Math.abs(r.rank) / 20.0, 1.0) + bonus;
            const score = unifiedScore(r, rawRel);
            const existing = results.get(r.content_hash);
            if (!existing || existing.score < score) {
              results.set(r.content_hash, { row: r, score });
            }
          }
        } catch {
          continue;
        }
      }
    }

    if (!query) {
      const rows = this.db
        .prepare(
          `SELECT * FROM memories m
           WHERE ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`,
        )
        .all(...params, limit) as MemoryRow[];
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: 0.5 });
      }
    }

    const sorted = [...results.values()]
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);

    if (boost && query && sorted.length > 0) {
      this.boostStrength(sorted.map((s) => s.row.id));
    }

    if (!sorted.length) return "No memories found.";

    const outputParts = [`Found ${sorted.length} memories:\n`];
    let totalChars = 0;
    for (const { row, score } of sorted) {
      const entry = formatMemory(row, score) + "\n";
      if (totalChars + entry.length > maxResponseChars) {
        outputParts.push(`\n... truncated (${sorted.length} total)`);
        break;
      }
      outputParts.push(entry);
      totalChars += entry.length;
    }
    return outputParts.join("\n");
  }

  getByTags(tags: string | string[], limit: number = 10): MemoryRow[] {
    const tagList = normalizeTags(tags)
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (!tagList.length) return [];

    const wheres = [
      "deleted_at IS NULL",
      activeValidUntilPredicate("valid_until"),
    ];
    const params: unknown[] = [];
    for (const t of tagList) {
      wheres.push(tagPredicate("tags"));
      params.push(tagParam(t));
    }

    return this.db
      .prepare(
        `SELECT * FROM memories
         WHERE ${wheres.join(" AND ")}
         ORDER BY created_at DESC LIMIT ?`,
      )
      .all(...params, limit) as MemoryRow[];
  }

  filterSearchResultsByTags(
    results: SearchResult[],
    tags: string | string[],
  ): SearchResult[] {
    if (!results.length) return [];
    const tagList = normalizeTags(tags)
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (!tagList.length) return results;
    const ids = results.map((r) => r.id);
    const idPlaceholders = ids.map(() => "?").join(",");
    const wheres = [
      `id IN (${idPlaceholders})`,
      "deleted_at IS NULL",
      activeValidUntilPredicate("valid_until"),
    ];
    const params: unknown[] = [...ids];
    for (const tag of tagList) {
      wheres.push(tagPredicate("tags"));
      params.push(tagParam(tag));
    }
    const rows = this.db
      .prepare(`SELECT id FROM memories WHERE ${wheres.join(" AND ")}`)
      .all(...params) as Array<{ id: number }>;
    const allowed = new Set(rows.map((row) => row.id));
    return results.filter((result) => allowed.has(result.id));
  }

  getUniversalKnowledge(limit: number = 5): MemoryRow[] {
    return this.db
      .prepare(
        `SELECT * FROM memories
           WHERE deleted_at IS NULL
           AND ${activeValidUntilPredicate("valid_until")}
           AND (tags NOT LIKE '%proj:%' OR tags IS NULL OR tags = '')
           AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
         ORDER BY COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') END, 1.0)
                  * COALESCE(strength, 1.0) DESC
         LIMIT ?`,
      )
      .all(limit) as MemoryRow[];
  }

  getByHash(contentHash: string): MemoryRow | undefined {
    return this.db
      .prepare(
        `SELECT * FROM memories
         WHERE content_hash = ? AND deleted_at IS NULL
           AND ${activeValidUntilPredicate("valid_until")}`,
      )
      .get(contentHash) as MemoryRow | undefined;
  }

  getById(id: number): MemoryRow | undefined {
    return this.db
      .prepare(
        `SELECT * FROM memories
         WHERE id = ? AND deleted_at IS NULL
           AND ${activeValidUntilPredicate("valid_until")}`,
      )
      .get(id) as MemoryRow | undefined;
  }

  update(
    contentHash: string,
    updates: {
      tags?: string | string[];
      memory_type?: string;
      metadata?: Record<string, unknown>;
      strength?: number;
      valid_until?: string | null;
      deleted_at?: number | null;
    },
  ): boolean {
    const row = this.db
      .prepare(
        "SELECT * FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
      )
      .get(contentHash) as MemoryRow | undefined;
    if (!row) return false;

    const protectedFields = new Set(["content", "content_hash", "embedding", "id"]);
    for (const key of Object.keys(updates)) {
      if (protectedFields.has(key)) return false;
    }

    const sets: string[] = [];
    const vals: unknown[] = [];

    if (updates.tags !== undefined) {
      sets.push("tags = ?");
      vals.push(normalizeTags(updates.tags));
    }
    if (updates.memory_type !== undefined) {
      sets.push("memory_type = ?");
      vals.push(updates.memory_type);
    }
    if (updates.metadata !== undefined) {
      let existing: Record<string, unknown> = {};
      try {
        existing = JSON.parse(row.metadata || "{}");
      } catch {
        // leave empty
      }
      Object.assign(existing, updates.metadata);
      sets.push("metadata = ?");
      vals.push(validateMetadata(existing));
    }
    if (updates.strength !== undefined) {
      sets.push("strength = ?");
      vals.push(Math.max(0.3, Math.min(5.0, updates.strength)));
    }
    if (updates.valid_until !== undefined) {
      sets.push("valid_until = ?");
      vals.push(updates.valid_until);
    }
    if (updates.deleted_at !== undefined) {
      sets.push("deleted_at = ?");
      vals.push(updates.deleted_at);
    }

    if (!sets.length) return false;

    const [ts, iso] = nowTs();
    sets.push("updated_at = ?");
    vals.push(ts);
    sets.push("updated_at_iso = ?");
    vals.push(iso);

    vals.push(contentHash);
    this.db
      .prepare(`UPDATE memories SET ${sets.join(", ")} WHERE content_hash = ?`)
      .run(...vals);
    return true;
  }

  rateQuality(
    contentHash: string,
    rating: "1" | "0" | "-1",
    feedback?: string,
  ): number | null {
    const row = this.db
      .prepare(
        "SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
      )
      .get(contentHash) as { metadata: string } | undefined;
    if (!row) return null;

    let meta: Record<string, unknown> = {};
    try {
      meta = JSON.parse(row.metadata || "{}");
    } catch {
      // leave empty
    }

    const userScore: Record<string, number> = { "1": 1.0, "0": 0.5, "-1": 0.0 };
    const parsedExisting = Number(meta.quality_score);
    const existing = Number.isFinite(parsedExisting) ? parsedExisting : 0.5;
    const newScore = Math.round(
      (0.6 * (userScore[rating] ?? 0.5) + 0.4 * existing) * 10000,
    ) / 10000;

    meta.quality_score = newScore;
    meta.quality_provider = "user";
    if (feedback) meta.quality_feedback = feedback;

    const [ts, iso] = nowTs();
    this.db
      .prepare(
        "UPDATE memories SET metadata = ?, updated_at = ?, updated_at_iso = ? WHERE content_hash = ?",
      )
      .run(JSON.stringify(meta), ts, iso, contentHash);

    return newScore;
  }

  boostStrength(ids: number[]): void {
    if (!ids.length) return;
    const nowTs = Math.floor(Date.now() / 1000);
    const stmt = this.db.prepare(
      `UPDATE memories
       SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
           last_accessed_at = ?,
           metadata = json_set(COALESCE(metadata, '{}'),
             '$.access_count',
             COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.access_count') END, 0) + 1)
       WHERE id = ?`,
    );
    const tx = this.db.transaction((ids: number[]) => {
      for (const id of ids) {
        try {
          stmt.run(nowTs, id);
        } catch {
          // non-critical
        }
      }
    });
    tx(ids);
  }

  boostStrengthFSRS(ids: number[]): void {
    if (!ids.length) return;
    const nowTs = Math.floor(Date.now() / 1000);

    const selectStmt = this.db.prepare(
      "SELECT strength, difficulty, due_date, metadata FROM memories WHERE id = ?",
    );
    const updateStmt = this.db.prepare(
      `UPDATE memories
       SET strength = ?, difficulty = ?, due_date = ?,
           last_accessed_at = unixepoch('now'),
           metadata = ?
       WHERE id = ?`,
    );

    const tx = this.db.transaction((ids: number[]) => {
      for (const id of ids) {
        const row = selectStmt.get(id) as {
          strength: number;
          difficulty: number;
          due_date: string | null;
          metadata: string;
        } | undefined;
        if (!row) continue;

        const strength = row.strength || 1.0;
        const difficulty = row.difficulty || 5.0;
        let accessCount = 0;
        try {
          const meta = JSON.parse(row.metadata || "{}");
          accessCount = Number(meta.access_count) || 0;
        } catch {
          // leave 0
        }

        const newStrength = Math.min(strength + 0.2, 5.0);

        let meta: Record<string, unknown> = {};
        try {
          meta = JSON.parse(row.metadata || "{}");
        } catch {
          // leave empty
        }
        meta.access_count = accessCount + 1;

        updateStmt.run(
          newStrength,
          difficulty,
          row.due_date,
          JSON.stringify(meta),
          id,
        );
      }
    });
    tx(ids);
  }

  getSessionContext(projectName: string): SessionContextResult {
    const nowTs = Math.floor(Date.now() / 1000);
    const result: SessionContextResult = {
      projectMemories: [],
      universalMemories: [],
      lastSessionSummary: null,
      userProfile: null,
    };

    if (projectName) {
      const projRows = this.db
        .prepare(
          `SELECT id, content, memory_type, tags, metadata, strength
           FROM memories
           WHERE deleted_at IS NULL
             AND ${activeValidUntilPredicate("valid_until")}
             AND ${tagPredicate("tags")}
             AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
           ORDER BY COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') END, 1.0)
                    * COALESCE(strength, 1.0) DESC
           LIMIT 3`,
        )
        .all(tagParam(`proj:${projectName}`)) as Array<{
        id: number;
        content: string;
        memory_type: string;
      }>;

      result.projectMemories = projRows.map((m) => ({
        content: m.content.slice(0, 300),
        memory_type: m.memory_type,
      }));

      if (projRows.length > 0) {
        const boostIds = projRows.map((m) => m.id);
        this.boostStrength(boostIds);
      }
    }

    const universalRows = this.db
      .prepare(
        `SELECT content, memory_type FROM memories
         WHERE deleted_at IS NULL
           AND ${activeValidUntilPredicate("valid_until")}
           AND (tags NOT LIKE '%proj:%' OR tags IS NULL OR tags = '')
           AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
         ORDER BY COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') END, 1.0)
                  * COALESCE(strength, 1.0) DESC
         LIMIT 2`,
      )
      .all() as Array<{ content: string; memory_type: string }>;

    result.universalMemories = universalRows.map((m) => ({
      content: m.content.slice(0, 300),
      memory_type: m.memory_type,
    }));

    if (projectName) {
      const summaryRow = this.db
        .prepare(
          `SELECT content FROM memories
             WHERE memory_type = 'session_summary'
             AND deleted_at IS NULL
             AND ${tagPredicate("tags")}
           ORDER BY created_at DESC LIMIT 1`,
        )
        .get(tagParam(`proj:${projectName}`)) as { content: string } | undefined;

      if (summaryRow) {
        result.lastSessionSummary = summaryRow.content.slice(0, 800);
      }
    }

    const profilePath = join(
      process.env.B12_DATA_DIR || join(homedir(), ".B12"),
      "user-profile.md",
    );
    if (existsSync(profilePath)) {
      try {
        const profile = readFileSync(profilePath, "utf-8").trim();
        if (profile) result.userProfile = profile.slice(0, 500);
      } catch {
        // leave null
      }
    }

    return result;
  }

  getContentGuardrails(): string[] {
    const rows = this.db
      .prepare(
        `SELECT content FROM memories
         WHERE deleted_at IS NULL
           AND ${activeValidUntilPredicate("valid_until")}
           AND memory_type = 'guardrail'
         ORDER BY COALESCE(strength, 1.0) DESC`,
      )
      .all() as Array<{ content: string }>;
    return rows.map((r) => r.content);
  }

  expandGraph(ids: number[], excludeIds: number[] = [], limit: number = 2): SearchResult[] {
    if (!ids.length) return [];

    const topIds = ids.slice(0, 3);
    const allIds = ids.length > 0 ? ids : topIds;

    const topPlaceholders = topIds.map(() => "?").join(",");
    const allPlaceholders = allIds.map(() => "?").join(",");

    const rows = this.db
      .prepare(
        `SELECT DISTINCT m2.id,
                '[' || m2.memory_type || '] ' || replace(substr(m2.content, 1, 200), char(10), ' ') as display,
                edges.similarity
         FROM (
           SELECT mg.target_hash AS neighbor_hash, mg.similarity
           FROM memory_graph mg
           JOIN memories m ON m.content_hash = mg.source_hash
           WHERE m.id IN (${topPlaceholders})
             AND mg.relationship_type IN ('related', 'supports')
             AND mg.similarity > 0.6
           UNION
           SELECT mg.source_hash AS neighbor_hash, mg.similarity
           FROM memory_graph mg
           JOIN memories m ON m.content_hash = mg.target_hash
           WHERE m.id IN (${topPlaceholders})
             AND mg.relationship_type IN ('related', 'supports')
             AND mg.similarity > 0.6
         ) edges
         JOIN memories m2 ON m2.content_hash = edges.neighbor_hash
         WHERE m2.id NOT IN (${allPlaceholders})
           AND m2.deleted_at IS NULL
         ORDER BY edges.similarity DESC
         LIMIT ?`,
      )
      .all(...topIds, ...topIds, ...allIds, limit) as Array<{
      id: number;
      display: string;
      similarity: number;
    }>;

    return rows.map((r) => ({
      id: r.id,
      display: r.display,
      score: r.similarity,
    }));
  }

  getGraphNeighbors(contentHash: string): GraphEdge[] {
    return this.db
      .prepare(
        `SELECT * FROM memory_graph
         WHERE source_hash = ? OR target_hash = ?
         ORDER BY similarity DESC`,
      )
      .all(contentHash, contentHash) as GraphEdge[];
  }

  logFeedback(
    feedbackDir: string,
    data: {
      query: string;
      keywords: string;
      resultCount: number;
      reranked: boolean;
      queryMode: string;
      skipReason: string;
      searchSource: string;
      latencyMs: number;
      project: string;
    },
  ): void {
    if (!existsSync(feedbackDir)) mkdirSync(feedbackDir, { recursive: true });
    const feedbackFile = join(feedbackDir, "feedback.jsonl");
    const entry = {
      ts: Math.floor(Date.now() / 1000),
      type: "plugin_retrieval",
      ...data,
    };
    appendFileSync(feedbackFile, JSON.stringify(entry) + "\n");
  }

  storeWorkingMemory(
    content: string,
    project: string,
    extraTags: string[] = [],
  ): void {
    const tags = [
      `proj:${project}`,
      "type:working_memory",
      "source:opencode",
      ...extraTags,
    ].join(",");
    const contentHash = computeContentHash(content);
    const [ts, iso] = nowTs();

    this.db
      .prepare(
        `INSERT OR IGNORE INTO memories
         (content_hash, content, tags, memory_type, metadata,
          strength, created_at, created_at_iso, updated_at, updated_at_iso)
         VALUES (?, ?, ?, 'working_memory', '{}', 1.0, ?, ?, ?, ?)`,
      )
      .run(contentHash, content, tags, ts, iso, ts, iso);
  }

  getStats(): {
    active: number;
    deleted: number;
    edges: number;
    types: Array<{ type: string; count: number }>;
  } {
    const active = (
      this.db
        .prepare("SELECT COUNT(*) as c FROM memories WHERE deleted_at IS NULL")
        .get() as { c: number }
    ).c;
    const deleted = (
      this.db
        .prepare(
          "SELECT COUNT(*) as c FROM memories WHERE deleted_at IS NOT NULL",
        )
        .get() as { c: number }
    ).c;
    const edges = (
      this.db
        .prepare("SELECT COUNT(*) as c FROM memory_graph")
        .get() as { c: number }
    ).c;

    const typeRows = this.db
      .prepare(
        "SELECT memory_type, COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL GROUP BY memory_type",
      )
      .all() as Array<{ memory_type: string; cnt: number }>;

    return {
      active,
      deleted,
      edges,
      types: typeRows.map((r) => ({ type: r.memory_type || "none", count: r.cnt })),
    };
  }
}
