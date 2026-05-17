// @bun
var __require = import.meta.require;

// src/index.ts
import { join as join9, basename as basename4 } from "path";
import { existsSync as existsSync7 } from "fs";
import { homedir as homedir8 } from "os";

// src/lib/db.ts
import Database from "better-sqlite3";
import { createHash } from "crypto";
import { homedir } from "os";
import { join } from "path";
import { platform } from "process";
import { existsSync, mkdirSync, readFileSync, appendFileSync } from "fs";
function getDbPath() {
  const home = homedir();
  if (platform === "darwin") {
    return join(home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db");
  }
  if (platform === "win32") {
    return join(home, "AppData", "Local", "mcp-memory", "sqlite_vec.db");
  }
  return join(home, ".local", "share", "mcp-memory", "sqlite_vec.db");
}
function computeContentHash(content) {
  return createHash("sha256").update(content.trim().toLowerCase()).digest("hex");
}
function normalizeTags(tags) {
  if (tags == null)
    return "";
  if (Array.isArray(tags))
    return tags.filter((t) => t).join(",");
  return String(tags);
}
function nowTs() {
  const ts = Math.floor(Date.now() / 1000);
  const iso = new Date(ts * 1000).toISOString();
  return [ts, iso];
}
function validateMetadata(value) {
  if (value == null)
    return "{}";
  if (typeof value === "object")
    return JSON.stringify(value);
  if (typeof value === "string") {
    const s = value.trim();
    if (!s)
      return "{}";
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
function unifiedScore(row, relevance) {
  const nowMs = Date.now();
  const nowTs2 = Math.floor(nowMs / 1000);
  const accessed = row.last_accessed_at ?? row.created_at ?? nowTs2;
  const ageDays = Math.max((nowTs2 - accessed) / 86400, 0.001);
  let strength = row.strength ?? 1;
  if (strength <= 0)
    strength = 0.01;
  const decay = Math.max(1 / (1 + ageDays / (9 * strength)), 0.01);
  let meta = {};
  try {
    meta = JSON.parse(row.metadata || "{}");
  } catch {}
  const importance = Math.min((Number(meta.importance_score) || 1) / 2, 1);
  return 0.3 * decay + 0.3 * importance + 0.4 * relevance;
}
function formatMemory(row, score) {
  const parts = [
    `[${row.memory_type || "general"}] ${row.content.slice(0, 500)}`
  ];
  if (row.tags)
    parts.push(`  Tags: ${row.tags}`);
  parts.push(`  Hash: ${row.content_hash}  Created: ${row.created_at_iso || "?"}`);
  if (score !== undefined)
    parts.push(`  Score: ${score.toFixed(3)}`);
  return parts.join(`
`);
}

class B12Database {
  db;
  constructor(dbPath) {
    const path = dbPath || getDbPath();
    const dir = join(path, "..");
    if (!existsSync(dir))
      mkdirSync(dir, { recursive: true });
    this.db = new Database(path, { timeout: 30000 });
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("busy_timeout = 30000");
    this.db.pragma("wal_autocheckpoint = 100");
  }
  close() {
    this.db.close();
  }
  get raw() {
    return this.db;
  }
  store(options) {
    const { content, valid_until } = options;
    const tags = normalizeTags(options.tags);
    const memoryType = options.memory_type || "general";
    const contentHash = computeContentHash(content);
    const [ts, iso] = nowTs();
    const baseMeta = {
      quality_score: 0.5,
      quality_provider: "implicit",
      access_count: 0,
      source_type: "user",
      credibility: 1
    };
    if (options.metadata)
      Object.assign(baseMeta, options.metadata);
    const metaJson = validateMetadata(baseMeta);
    const existing = this.db.prepare("SELECT id, deleted_at FROM memories WHERE content_hash = ?").get(contentHash);
    if (existing && existing.deleted_at !== null) {
      this.db.prepare(`UPDATE memories SET deleted_at = NULL, strength = 1.0,
           tags = ?, memory_type = ?, metadata = ?,
           updated_at = ?, updated_at_iso = ?, valid_until = ?
           WHERE content_hash = ?`).run(tags, memoryType, metaJson, ts, iso, valid_until ?? null, contentHash);
    } else {
      this.db.prepare(`INSERT OR IGNORE INTO memories
           (content_hash, content, tags, memory_type, metadata,
            strength, created_at, created_at_iso, updated_at, updated_at_iso,
            valid_until)
           VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)`).run(contentHash, content, tags, memoryType, metaJson, ts, iso, ts, iso, valid_until ?? null);
    }
    const row = this.db.prepare("SELECT id FROM memories WHERE content_hash = ?").get(contentHash);
    return { hash: contentHash, id: row.id };
  }
  search(options = {}) {
    const {
      query = "",
      mode = "hybrid",
      limit = 10,
      stemmed = false,
      maxResponseChars = 40000
    } = options;
    const tagList = normalizeTags(options.tags).split(",").map((t) => t.trim()).filter(Boolean);
    const wheres = [
      "m.deleted_at IS NULL",
      "(m.valid_until IS NULL OR m.valid_until > datetime('now'))"
    ];
    const params = [];
    for (const t of tagList) {
      wheres.push("m.tags LIKE ?");
      params.push(`%${t}%`);
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
    const results = new Map;
    if (mode === "exact" && query) {
      const rows = this.db.prepare(`SELECT * FROM memories m
           WHERE m.content LIKE ? AND ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`).all(`%${query}%`, ...params, limit);
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: unifiedScore(r, 0.9) });
      }
    }
    if (mode === "hybrid" && query) {
      const ftsTable = stemmed ? "memory_fts_stemmed" : "memory_content_fts";
      for (const ftsAttempt of ["phrase", "or"]) {
        try {
          let ftsQuery;
          if (ftsAttempt === "phrase") {
            ftsQuery = '"' + query.replace(/"/g, '""') + '"';
          } else {
            const words = query.split(/\s+/).map((w) => w.trim()).filter((w) => w.length > 1);
            if (!words.length)
              break;
            ftsQuery = words.map((w) => '"' + w.replace(/"/g, '""') + '"').join(" OR ");
          }
          const ftsRows = this.db.prepare(`SELECT m.*, rank
               FROM ${ftsTable} fts
               JOIN memories m ON m.id = fts.rowid
               WHERE fts.content MATCH ? AND ${whereSql}
               ORDER BY rank LIMIT ?`).all(ftsQuery, ...params, limit);
          for (const r of ftsRows) {
            const bonus = ftsAttempt === "phrase" ? 0.1 : 0;
            const rawRelevance = Math.min(Math.abs(r.rank) / 20, 1) + bonus;
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
      const rows = this.db.prepare(`SELECT * FROM memories m
           WHERE ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`).all(...params, limit);
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: 0.5 });
      }
    }
    const sorted = [...results.values()].sort((a, b) => b.score - a.score).slice(0, limit);
    if (query && sorted.length > 0) {
      this.boostStrength(sorted.map((s) => s.row.id));
    }
    return sorted.map(({ row, score }) => ({
      id: row.id,
      display: `[${row.memory_type || "general"}] ${row.content.slice(0, 300).replace(/\n/g, " ")}`,
      score
    }));
  }
  searchFormatted(options = {}) {
    const { limit = 10, maxResponseChars = 40000 } = options;
    const tagList = normalizeTags(options.tags).split(",").map((t) => t.trim()).filter(Boolean);
    const wheres = [
      "m.deleted_at IS NULL",
      "(m.valid_until IS NULL OR m.valid_until > datetime('now'))"
    ];
    const params = [];
    for (const t of tagList) {
      wheres.push("m.tags LIKE ?");
      params.push(`%${t}%`);
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
    const results = new Map;
    const query = options.query || "";
    const mode = options.mode || "hybrid";
    const stemmed = options.stemmed ?? false;
    if (mode === "exact" && query) {
      const rows = this.db.prepare(`SELECT * FROM memories m
           WHERE m.content LIKE ? AND ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`).all(`%${query}%`, ...params, limit);
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: unifiedScore(r, 0.9) });
      }
    }
    if (mode === "hybrid" && query) {
      const ftsTable = stemmed ? "memory_fts_stemmed" : "memory_content_fts";
      for (const ftsAttempt of ["phrase", "or"]) {
        try {
          let ftsQuery;
          if (ftsAttempt === "phrase") {
            ftsQuery = '"' + query.replace(/"/g, '""') + '"';
          } else {
            const words = query.split(/\s+/).map((w) => w.trim()).filter((w) => w.length > 1);
            if (!words.length)
              break;
            ftsQuery = words.map((w) => '"' + w.replace(/"/g, '""') + '"').join(" OR ");
          }
          const ftsRows = this.db.prepare(`SELECT m.*, rank
               FROM ${ftsTable} fts
               JOIN memories m ON m.id = fts.rowid
               WHERE fts.content MATCH ? AND ${whereSql}
               ORDER BY rank LIMIT ?`).all(ftsQuery, ...params, limit);
          for (const r of ftsRows) {
            const bonus = ftsAttempt === "phrase" ? 0.1 : 0;
            const rawRel = Math.min(Math.abs(r.rank) / 20, 1) + bonus;
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
      const rows = this.db.prepare(`SELECT * FROM memories m
           WHERE ${whereSql}
           ORDER BY m.created_at DESC LIMIT ?`).all(...params, limit);
      for (const r of rows) {
        results.set(r.content_hash, { row: r, score: 0.5 });
      }
    }
    const sorted = [...results.values()].sort((a, b) => b.score - a.score).slice(0, limit);
    if (query && sorted.length > 0) {
      this.boostStrength(sorted.map((s) => s.row.id));
    }
    if (!sorted.length)
      return "No memories found.";
    const outputParts = [`Found ${sorted.length} memories:
`];
    let totalChars = 0;
    for (const { row, score } of sorted) {
      const entry = formatMemory(row, score) + `
`;
      if (totalChars + entry.length > maxResponseChars) {
        outputParts.push(`
... truncated (${sorted.length} total)`);
        break;
      }
      outputParts.push(entry);
      totalChars += entry.length;
    }
    return outputParts.join(`
`);
  }
  getByTags(tags, limit = 10) {
    const tagList = normalizeTags(tags).split(",").map((t) => t.trim()).filter(Boolean);
    if (!tagList.length)
      return [];
    const wheres = [
      "deleted_at IS NULL",
      "(valid_until IS NULL OR valid_until > datetime('now'))"
    ];
    const params = [];
    for (const t of tagList) {
      wheres.push("tags LIKE ?");
      params.push(`%${t}%`);
    }
    return this.db.prepare(`SELECT * FROM memories
         WHERE ${wheres.join(" AND ")}
         ORDER BY created_at DESC LIMIT ?`).all(...params, limit);
  }
  getUniversalKnowledge(limit = 5) {
    return this.db.prepare(`SELECT * FROM memories
         WHERE deleted_at IS NULL
           AND (valid_until IS NULL OR valid_until > datetime('now'))
           AND (tags NOT LIKE '%proj:%' OR tags IS NULL OR tags = '')
           AND memory_type NOT IN ('session_summary', 'progress')
         ORDER BY COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') END, 1.0)
                  * COALESCE(strength, 1.0) DESC
         LIMIT ?`).all(limit);
  }
  getByHash(contentHash) {
    return this.db.prepare("SELECT * FROM memories WHERE content_hash = ? AND deleted_at IS NULL").get(contentHash);
  }
  getById(id) {
    return this.db.prepare("SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL").get(id);
  }
  update(contentHash, updates) {
    const row = this.db.prepare("SELECT * FROM memories WHERE content_hash = ? AND deleted_at IS NULL").get(contentHash);
    if (!row)
      return false;
    const protectedFields = new Set(["content", "content_hash", "embedding", "id"]);
    for (const key of Object.keys(updates)) {
      if (protectedFields.has(key))
        return false;
    }
    const sets = [];
    const vals = [];
    if (updates.tags !== undefined) {
      sets.push("tags = ?");
      vals.push(normalizeTags(updates.tags));
    }
    if (updates.memory_type !== undefined) {
      sets.push("memory_type = ?");
      vals.push(updates.memory_type);
    }
    if (updates.metadata !== undefined) {
      let existing = {};
      try {
        existing = JSON.parse(row.metadata || "{}");
      } catch {}
      Object.assign(existing, updates.metadata);
      sets.push("metadata = ?");
      vals.push(validateMetadata(existing));
    }
    if (updates.strength !== undefined) {
      sets.push("strength = ?");
      vals.push(Math.max(0.3, Math.min(5, updates.strength)));
    }
    if (updates.valid_until !== undefined) {
      sets.push("valid_until = ?");
      vals.push(updates.valid_until);
    }
    if (updates.deleted_at !== undefined) {
      sets.push("deleted_at = ?");
      vals.push(updates.deleted_at);
    }
    if (!sets.length)
      return false;
    const [ts, iso] = nowTs();
    sets.push("updated_at = ?");
    vals.push(ts);
    sets.push("updated_at_iso = ?");
    vals.push(iso);
    vals.push(contentHash);
    this.db.prepare(`UPDATE memories SET ${sets.join(", ")} WHERE content_hash = ?`).run(...vals);
    return true;
  }
  rateQuality(contentHash, rating, feedback) {
    const row = this.db.prepare("SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL").get(contentHash);
    if (!row)
      return null;
    let meta = {};
    try {
      meta = JSON.parse(row.metadata || "{}");
    } catch {}
    const userScore = { "1": 1, "0": 0.5, "-1": 0 };
    const existing = Number(meta.quality_score) || 0.5;
    const newScore = Math.round((0.6 * (userScore[rating] ?? 0.5) + 0.4 * existing) * 1e4) / 1e4;
    meta.quality_score = newScore;
    meta.quality_provider = "user";
    if (feedback)
      meta.quality_feedback = feedback;
    const [ts, iso] = nowTs();
    this.db.prepare("UPDATE memories SET metadata = ?, updated_at = ?, updated_at_iso = ? WHERE content_hash = ?").run(JSON.stringify(meta), ts, iso, contentHash);
    return newScore;
  }
  boostStrength(ids) {
    if (!ids.length)
      return;
    const nowTs2 = Math.floor(Date.now() / 1000);
    const stmt = this.db.prepare(`UPDATE memories
       SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
           last_accessed_at = ?,
           metadata = json_set(COALESCE(metadata, '{}'),
             '$.access_count',
             COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.access_count') END, 0) + 1)
       WHERE id = ?`);
    const tx = this.db.transaction((ids2) => {
      for (const id of ids2) {
        try {
          stmt.run(nowTs2, id);
        } catch {}
      }
    });
    tx(ids);
  }
  boostStrengthFSRS(ids) {
    if (!ids.length)
      return;
    const nowTs2 = Math.floor(Date.now() / 1000);
    const selectStmt = this.db.prepare("SELECT strength, difficulty, due_date, metadata FROM memories WHERE id = ?");
    const updateStmt = this.db.prepare(`UPDATE memories
       SET strength = ?, difficulty = ?, due_date = ?,
           last_accessed_at = unixepoch('now'),
           metadata = ?
       WHERE id = ?`);
    const tx = this.db.transaction((ids2) => {
      for (const id of ids2) {
        const row = selectStmt.get(id);
        if (!row)
          continue;
        const strength = row.strength || 1;
        const difficulty = row.difficulty || 5;
        let accessCount = 0;
        try {
          const meta2 = JSON.parse(row.metadata || "{}");
          accessCount = Number(meta2.access_count) || 0;
        } catch {}
        const newStrength = Math.min(strength + 0.2, 5);
        let meta = {};
        try {
          meta = JSON.parse(row.metadata || "{}");
        } catch {}
        meta.access_count = accessCount + 1;
        updateStmt.run(newStrength, difficulty, row.due_date, JSON.stringify(meta), id);
      }
    });
    tx(ids);
  }
  getSessionContext(projectName) {
    const nowTs2 = Math.floor(Date.now() / 1000);
    const result = {
      projectMemories: [],
      universalMemories: [],
      lastSessionSummary: null,
      userProfile: null
    };
    if (projectName) {
      const projRows = this.db.prepare(`SELECT id, content, memory_type, tags, metadata, strength
           FROM memories
           WHERE deleted_at IS NULL
             AND (valid_until IS NULL OR valid_until > datetime('now'))
             AND tags LIKE ?
             AND memory_type NOT IN ('session_summary', 'progress')
           ORDER BY COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') END, 1.0)
                    * COALESCE(strength, 1.0) DESC
           LIMIT 3`).all(`%proj:${projectName}%`);
      result.projectMemories = projRows.map((m) => ({
        content: m.content.slice(0, 300),
        memory_type: m.memory_type
      }));
      if (projRows.length > 0) {
        const boostIds = projRows.map((m) => m.id);
        this.boostStrength(boostIds);
      }
    }
    const universalRows = this.db.prepare(`SELECT content, memory_type FROM memories
         WHERE deleted_at IS NULL
           AND (valid_until IS NULL OR valid_until > datetime('now'))
           AND (tags NOT LIKE '%proj:%' OR tags IS NULL OR tags = '')
           AND memory_type NOT IN ('session_summary', 'progress')
         ORDER BY COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') END, 1.0)
                  * COALESCE(strength, 1.0) DESC
         LIMIT 2`).all();
    result.universalMemories = universalRows.map((m) => ({
      content: m.content.slice(0, 300),
      memory_type: m.memory_type
    }));
    if (projectName) {
      const summaryRow = this.db.prepare(`SELECT content FROM memories
           WHERE memory_type = 'session_summary'
             AND deleted_at IS NULL
             AND tags LIKE ?
           ORDER BY created_at DESC LIMIT 1`).get(`%proj:${projectName}%`);
      if (summaryRow) {
        result.lastSessionSummary = summaryRow.content.slice(0, 800);
      }
    }
    const profilePath = join(process.env.B12_DATA_DIR || join(homedir(), ".B12"), "user-profile.md");
    if (existsSync(profilePath)) {
      try {
        const profile = readFileSync(profilePath, "utf-8").trim();
        if (profile)
          result.userProfile = profile.slice(0, 500);
      } catch {}
    }
    return result;
  }
  getContentGuardrails() {
    const rows = this.db.prepare(`SELECT content FROM memories
         WHERE deleted_at IS NULL
           AND (valid_until IS NULL OR valid_until > datetime('now'))
           AND memory_type = 'guardrail'
         ORDER BY COALESCE(strength, 1.0) DESC`).all();
    return rows.map((r) => r.content);
  }
  expandGraph(ids, excludeIds = [], limit = 2) {
    if (!ids.length)
      return [];
    const topIds = ids.slice(0, 3);
    const allIds = ids.length > 0 ? ids : topIds;
    const topPlaceholders = topIds.map(() => "?").join(",");
    const allPlaceholders = allIds.map(() => "?").join(",");
    const rows = this.db.prepare(`SELECT DISTINCT m2.id,
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
         LIMIT ?`).all(...topIds, ...topIds, ...allIds, limit);
    return rows.map((r) => ({
      id: r.id,
      display: r.display,
      score: r.similarity
    }));
  }
  getGraphNeighbors(contentHash) {
    return this.db.prepare(`SELECT * FROM memory_graph
         WHERE source_hash = ? OR target_hash = ?
         ORDER BY similarity DESC`).all(contentHash, contentHash);
  }
  logFeedback(feedbackDir, data) {
    if (!existsSync(feedbackDir))
      mkdirSync(feedbackDir, { recursive: true });
    const feedbackFile = join(feedbackDir, "feedback.jsonl");
    const entry = {
      ts: Math.floor(Date.now() / 1000),
      type: "plugin_retrieval",
      ...data
    };
    appendFileSync(feedbackFile, JSON.stringify(entry) + `
`);
  }
  storeWorkingMemory(content, project, extraTags = []) {
    const tags = [
      `proj:${project}`,
      "type:working_memory",
      "source:opencode",
      ...extraTags
    ].join(",");
    const contentHash = computeContentHash(content);
    const [ts, iso] = nowTs();
    this.db.prepare(`INSERT OR IGNORE INTO memories
         (content_hash, content, tags, memory_type, metadata,
          strength, created_at, created_at_iso, updated_at, updated_at_iso)
         VALUES (?, ?, ?, 'working_memory', '{}', 1.0, ?, ?, ?, ?)`).run(contentHash, content, tags, ts, iso, ts, iso);
  }
  getStats() {
    const active = this.db.prepare("SELECT COUNT(*) as c FROM memories WHERE deleted_at IS NULL").get().c;
    const deleted = this.db.prepare("SELECT COUNT(*) as c FROM memories WHERE deleted_at IS NOT NULL").get().c;
    const edges = this.db.prepare("SELECT COUNT(*) as c FROM memory_graph").get().c;
    const typeRows = this.db.prepare("SELECT memory_type, COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL GROUP BY memory_type").all();
    return {
      active,
      deleted,
      edges,
      types: typeRows.map((r) => ({ type: r.memory_type || "none", count: r.cnt }))
    };
  }
}

// src/hooks/session-start.ts
import { join as join2 } from "path";
import { existsSync as existsSync2, readFileSync as readFileSync2 } from "fs";
import { homedir as homedir2 } from "os";

// src/lib/daemon.ts
import { connect } from "net";
var {spawn } = globalThis.Bun;
var _UID = process.getuid?.() ?? process.pid;
var SOCKET_PATH = `/tmp/b12-embed-${_UID}.sock`;
var REQUEST_TIMEOUT = 4000;
var BATCH_TIMEOUT = 15000;
var BUFFER_SIZE = 1024 * 1024;
function socketRequest(payload, timeout) {
  return new Promise((resolve) => {
    const socket = connect(SOCKET_PATH);
    let buffer = Buffer.alloc(0);
    let settled = false;
    const timer = setTimeout(() => {
      settled = true;
      socket.destroy();
      resolve(null);
    }, timeout);
    socket.on("data", (data) => {
      buffer = Buffer.concat([buffer, data]);
      if (buffer.length > BUFFER_SIZE) {
        settled = true;
        clearTimeout(timer);
        socket.destroy();
        resolve(null);
        return;
      }
      const nlIdx = buffer.indexOf(10);
      if (nlIdx === -1)
        return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      const line = buffer.subarray(0, nlIdx).toString("utf-8");
      try {
        resolve(JSON.parse(line));
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
    socket.write(JSON.stringify(payload) + `
`);
  });
}
async function health() {
  const res = await socketRequest({ op: "health" }, REQUEST_TIMEOUT);
  if (!res || !res.ok)
    return { alive: false };
  return {
    alive: true,
    uptime: res.uptime,
    requests_served: res.requests_served
  };
}
async function semanticSearch(query, dbPath, limit = 10) {
  const res = await socketRequest({ op: "semantic_search", query, db_path: dbPath, limit }, BATCH_TIMEOUT);
  if (!res?.ok || !Array.isArray(res.results))
    return [];
  return res.results;
}
async function rerank(query, dbPath, ids) {
  const res = await socketRequest({ op: "rerank", query, db_path: dbPath, ids }, REQUEST_TIMEOUT);
  if (!res?.ok || !Array.isArray(res.ranked_ids))
    return [];
  return res.ranked_ids;
}
async function encodeBatch(texts) {
  const res = await socketRequest({ op: "encode_batch", texts }, BATCH_TIMEOUT);
  if (!res?.ok || !Array.isArray(res.embeddings))
    return [];
  return res.embeddings;
}
var _daemonProcess = null;
async function startDaemon(venvPython, scriptPath) {
  const h = await health();
  if (h.alive)
    return true;
  try {
    _daemonProcess = spawn({
      cmd: [venvPython, scriptPath],
      stdout: "ignore",
      stderr: "ignore",
      detached: true
    });
    for (let i = 0;i < 20; i++) {
      await new Promise((r) => setTimeout(r, 500));
      const check = await health();
      if (check.alive)
        return true;
    }
    return false;
  } catch {
    return false;
  }
}

// src/hooks/session-start.ts
var CHAR_BUDGET = 6000;
var B12_BASE = process.env.B12_DATA_DIR || join2(homedir2(), ".B12");
async function sessionStart(project, cwd, db) {
  const venvPython = join2(homedir2(), ".local", "b12-venv", "bin", "python3");
  const scriptPath = join2(B12_BASE, "hooks", "scripts", "embed_daemon.py");
  if (existsSync2(venvPython) && existsSync2(scriptPath)) {
    await startDaemon(venvPython, scriptPath);
  }
  const sections = [];
  let totalChars = 0;
  const profilePath = join2(B12_BASE, "user-profile.md");
  if (existsSync2(profilePath)) {
    const profile = readFileSync2(profilePath, "utf-8").trim();
    if (profile && totalChars + profile.length < CHAR_BUDGET) {
      sections.push(`## User Profile
${profile}`);
      totalChars += profile.length;
    }
  }
  const summaryDir = join2(B12_BASE, "memory-summaries");
  const summaryFile = project ? join2(summaryDir, `${project}-latest.md`) : null;
  if (summaryFile && existsSync2(summaryFile)) {
    const summary = readFileSync2(summaryFile, "utf-8").trim();
    if (summary && totalChars + summary.length < CHAR_BUDGET) {
      sections.push(`## Last Session Summary
${summary.slice(0, 1500)}`);
      totalChars += Math.min(summary.length, 1500);
    }
  }
  const context = db.getSessionContext(project);
  if (context.projectMemories.length > 0) {
    const lines = context.projectMemories.map((m) => `[${m.memory_type}] ${m.content}`).join(`
`);
    if (totalChars + lines.length < CHAR_BUDGET) {
      sections.push(`## Project Memories (${project})
${lines}`);
      totalChars += lines.length;
    }
  }
  if (context.universalMemories.length > 0) {
    const lines = context.universalMemories.map((m) => `[${m.memory_type}] ${m.content}`).join(`
`);
    if (totalChars + lines.length < CHAR_BUDGET) {
      sections.push(`## Cross-Project Knowledge
${lines}`);
      totalChars += lines.length;
    }
  }
  const guardrails = db.getContentGuardrails();
  if (guardrails.length > 0) {
    const text = guardrails.slice(0, 3).join(`
`);
    if (totalChars + text.length < CHAR_BUDGET) {
      sections.push(`## Content Guardrails
${text}`);
      totalChars += text.length;
    }
  }
  const feedbackDir = join2(B12_BASE, "memory-staging");
  const feedbackFile = join2(feedbackDir, "feedback.jsonl");
  if (existsSync2(feedbackFile)) {
    try {
      const lines = readFileSync2(feedbackFile, "utf-8").split(`
`).filter((l) => l.trim()).slice(-20);
      const lowRated = lines.map((l) => {
        try {
          return JSON.parse(l);
        } catch {
          return null;
        }
      }).filter((e) => e != null && e.rating === "-1").slice(0, 3);
      if (lowRated.length > 0) {
        const text = lowRated.map((e) => `- ${String(e.query || "").slice(0, 80)}: ${String(e.feedback || "low quality")}`).join(`
`);
        if (totalChars + text.length < CHAR_BUDGET) {
          sections.push(`## Recent Feedback (Low Quality)
${text}`);
          totalChars += text.length;
        }
      }
    } catch {}
  }
  let result = sections.join(`

`);
  if (result.length > CHAR_BUDGET) {
    result = result.slice(0, CHAR_BUDGET);
  }
  return result;
}

// src/hooks/message-retrieval.ts
import { join as join3 } from "path";
import { existsSync as existsSync3, readFileSync as readFileSync3 } from "fs";
import { homedir as homedir3 } from "os";
var B12_BASE2 = process.env.B12_DATA_DIR || join3(homedir3(), ".B12");
var STOPWORDS_EN = new Set([
  "the",
  "a",
  "an",
  "is",
  "are",
  "was",
  "were",
  "be",
  "been",
  "being",
  "have",
  "has",
  "had",
  "do",
  "does",
  "did",
  "will",
  "would",
  "could",
  "should",
  "may",
  "might",
  "shall",
  "can",
  "to",
  "of",
  "in",
  "for",
  "on",
  "with",
  "at",
  "by",
  "from",
  "as",
  "into",
  "through",
  "during",
  "before",
  "after",
  "above",
  "below",
  "between",
  "out",
  "off",
  "over",
  "under",
  "again",
  "further",
  "then",
  "once",
  "here",
  "there",
  "when",
  "where",
  "why",
  "how",
  "all",
  "both",
  "each",
  "few",
  "more",
  "most",
  "other",
  "some",
  "such",
  "no",
  "nor",
  "not",
  "only",
  "own",
  "same",
  "so",
  "than",
  "too",
  "very",
  "just",
  "because",
  "but",
  "and",
  "or",
  "if",
  "while",
  "this",
  "that",
  "these",
  "those",
  "it",
  "its",
  "what",
  "which",
  "who",
  "whom",
  "i",
  "me",
  "my",
  "we",
  "our",
  "you",
  "your",
  "he",
  "him",
  "his",
  "she",
  "her",
  "they",
  "them",
  "their",
  "about",
  "up",
  "also",
  "like",
  "get",
  "make",
  "go",
  "know",
  "take",
  "see",
  "come",
  "think",
  "look",
  "want",
  "give",
  "use",
  "find",
  "tell",
  "ask",
  "work",
  "seem",
  "feel",
  "try",
  "leave",
  "call",
  "keep",
  "let",
  "begin",
  "show",
  "hear",
  "play",
  "run",
  "move",
  "live",
  "believe",
  "bring",
  "happen",
  "write",
  "provide",
  "sit",
  "stand",
  "lose",
  "pay",
  "meet",
  "include",
  "continue",
  "set",
  "learn",
  "change",
  "lead",
  "understand",
  "watch",
  "follow",
  "stop",
  "create",
  "speak",
  "read",
  "allow",
  "add",
  "spend",
  "grow",
  "open",
  "walk",
  "win",
  "offer",
  "remember",
  "love",
  "consider",
  "appear",
  "buy",
  "wait",
  "serve",
  "die",
  "send",
  "expect",
  "build",
  "stay",
  "fall",
  "cut",
  "reach",
  "kill",
  "remain",
  "suggest",
  "raise",
  "pass",
  "sell",
  "require",
  "report",
  "decide",
  "pull",
  "really",
  "much",
  "thing",
  "any",
  "even"
]);
var STOPWORDS_TR = new Set([
  "bir",
  "bu",
  "\u015Fu",
  "da",
  "de",
  "ve",
  "ile",
  "i\xE7in",
  "ama",
  "fakat",
  "yada",
  "veya",
  "gibi",
  "kadar",
  "daha",
  "en",
  "\xE7ok",
  "az",
  "her",
  "hi\xE7",
  "baz\u0131",
  "t\xFCm",
  "hepsi",
  "b\xFCt\xFCn",
  "ba\u015Fka",
  "sonra",
  "\xF6nce",
  "ilk",
  "son",
  "yeni",
  "eski",
  "b\xFCy\xFCk",
  "k\xFC\xE7\xFCk",
  "iyi",
  "k\xF6t\xFC",
  "uzun",
  "k\u0131sa",
  "genel",
  "\xF6zel",
  "ayn\u0131",
  "farkl\u0131",
  "nas\u0131l",
  "neden",
  "ni\xE7in",
  "nerede",
  "ne zaman",
  "kim",
  "ne",
  "hangi",
  "ka\xE7",
  "nas\u0131l",
  "kez",
  "olarak",
  "\xFCzere",
  "gibi",
  "sanki",
  "ben",
  "sen",
  "o",
  "biz",
  "siz",
  "onlar",
  "benim",
  "senin",
  "onun",
  "bizim",
  "sizin",
  "onlar\u0131n",
  "bana",
  "sana",
  "ona",
  "bize",
  "size",
  "onlara",
  "beni",
  "seni",
  "onu",
  "bizi",
  "sizi",
  "onlar\u0131",
  "benle",
  "senle",
  "onla",
  "bizle",
  "sizle",
  "onlarla",
  "evet",
  "hay\u0131r",
  "belki",
  "tabii",
  "tamam",
  "olur",
  "olmaz",
  "iyi",
  "g\xFCzel",
  "peki",
  "demek",
  "var",
  "yok",
  "mi",
  "m\u0131",
  "mu",
  "m\xFC"
]);
var ALL_STOPWORDS = new Set([...STOPWORDS_EN, ...STOPWORDS_TR]);
var _aliases = null;
function loadAliases() {
  if (_aliases)
    return _aliases;
  const aliasPath = join3(B12_BASE2, "hooks", "scripts", "query_aliases.json");
  if (existsSync3(aliasPath)) {
    try {
      _aliases = JSON.parse(readFileSync3(aliasPath, "utf-8"));
      return _aliases;
    } catch {}
  }
  _aliases = {};
  return _aliases;
}
function extractKeywords(text) {
  const words = text.toLowerCase().replace(/[^\w\u00C0-\u024F\u1E00-\u1EFF]/g, " ").split(/\s+/).filter((w) => w.length >= 3 && !ALL_STOPWORDS.has(w));
  return [...new Set(words)];
}
function buildFtsQuery(keywords) {
  if (keywords.length === 0)
    return "";
  const aliases = loadAliases();
  const expanded = [];
  for (const kw of keywords.slice(0, 8)) {
    expanded.push(`"${kw.replace(/"/g, '""')}"`);
    if (aliases[kw]) {
      for (const alias of aliases[kw].slice(0, 2)) {
        expanded.push(`"${alias.replace(/"/g, '""')}"`);
      }
    }
  }
  return expanded.join(" OR ");
}
function isGreeting(text) {
  const t = text.toLowerCase().trim();
  const greetings = [
    "hi",
    "hello",
    "hey",
    "merhaba",
    "selam",
    "naber",
    "nas\u0131ls\u0131n",
    "good morning",
    "good afternoon",
    "good evening",
    "g\xFCnayd\u0131n",
    "iyi g\xFCnler",
    "iyi ak\u015Famlar"
  ];
  return greetings.some((g) => t === g || t.length < 20 && t.startsWith(g));
}
function isSlashCommand(text) {
  return text.trim().startsWith("/");
}
function isShortCommand(text) {
  return text.trim().split(/\s+/).length <= 2 && text.length < 15;
}
async function messageRetrieval(userMessage, project, db) {
  const startTime = Date.now();
  if (isGreeting(userMessage) || isSlashCommand(userMessage) || isShortCommand(userMessage)) {
    return "";
  }
  const keywords = extractKeywords(userMessage);
  if (keywords.length === 0)
    return "";
  const ftsQuery = buildFtsQuery(keywords);
  let ftsResults = db.search({
    query: ftsQuery,
    mode: "hybrid",
    tags: [`proj:${project}`],
    limit: 10
  });
  let semanticResults = [];
  try {
    const daemonAlive = await health();
    if (daemonAlive.alive) {
      semanticResults = await semanticSearch(keywords.join(" "), getDbPath(), 5);
    }
  } catch {}
  const mergedMap = new Map;
  for (const r of ftsResults) {
    mergedMap.set(r.id, { display: r.display, score: r.score });
  }
  for (const r of semanticResults) {
    const existing = mergedMap.get(r.id);
    if (existing) {
      mergedMap.set(r.id, {
        display: existing.display,
        score: Math.max(existing.score, r.score) * 1.1
      });
    } else {
      mergedMap.set(r.id, { display: r.display, score: r.score * 0.8 });
    }
  }
  const merged = [...mergedMap.entries()].map(([id, v]) => ({ id, ...v })).sort((a, b) => b.score - a.score).slice(0, 5);
  if (merged.length === 0)
    return "";
  let reranked = false;
  if (merged.length > 1) {
    try {
      const rankedIds = await rerank(keywords.join(" "), getDbPath(), merged.map((m) => m.id));
      if (rankedIds.length > 0) {
        reranked = true;
        const idOrder = new Map(rankedIds.map((id, i) => [id, i]));
        merged.sort((a, b) => {
          const oa = idOrder.get(a.id) ?? 999;
          const ob = idOrder.get(b.id) ?? 999;
          return oa - ob;
        });
      }
    } catch {}
  }
  db.boostStrength(merged.map((m) => m.id));
  const latencyMs = Date.now() - startTime;
  const stagingDir = join3(B12_BASE2, "memory-staging");
  try {
    db.logFeedback(stagingDir, {
      query: userMessage.slice(0, 200),
      keywords: keywords.join(","),
      resultCount: merged.length,
      reranked,
      queryMode: "hybrid",
      skipReason: "",
      searchSource: "plugin",
      latencyMs,
      project
    });
  } catch {}
  const lines = merged.map((m) => `${m.display} (score: ${m.score.toFixed(2)})`);
  return `## Relevant Memories
${lines.join(`
`)}`;
}

// src/hooks/tag-enforce.ts
import { join as join4 } from "path";
import { homedir as homedir4 } from "os";
var B12_BASE3 = process.env.B12_DATA_DIR || join4(homedir4(), ".B12");
function tagEnforce(input, output, project, setupContext) {
  if (input.tool !== "B12_memory_store" && input.tool !== "mcp__B12__memory_store")
    return;
  const args = output.args;
  const metadataRaw = args.metadata;
  let tags = [];
  if (typeof args.tags === "string") {
    tags = args.tags.split(",").map((t) => t.trim()).filter(Boolean);
  } else if (Array.isArray(args.tags)) {
    tags = args.tags;
  }
  let hasProj = tags.some((t) => t.startsWith("proj:"));
  let hasUser = tags.some((t) => t.startsWith("user:"));
  if (!hasProj && project) {
    tags.push(`proj:${project}`);
    hasProj = true;
  }
  if (!hasUser) {
    if (setupContext) {
      tags.push(`user:${setupContext}`);
    } else {
      tags.push("user:universal");
    }
    hasUser = true;
  }
  output.args = { ...args, tags: tags.join(",") };
}

// src/hooks/post-tool.ts
import { join as join6, basename as basename2 } from "path";
import { homedir as homedir5 } from "os";

// src/lib/patterns.ts
var DECISION_RE = /(?i)(?:(?:decided|chose|going with|selected|opted for|switched to|went with)\s+.{5,}|(?:will use|using|let.?s use|we.?ll use)\s+\S+\s+(?:instead of|rather than|for|because)\s+|(?:the (?:approach|solution|decision|plan) is to)\s+|(?:switching from|replacing|migrating from)\s+\S+\s+(?:to|with)\s+|(?:karar verdik?|se\u00E7tik?|tercih ettik?|bununla gid|bunu kullan)\s*.{5,}|(?:yerine|de\u011Fil de|bunun yerine)\s+\S+\s+.{3,}|(?:plan\u0131m\u0131z|yakla\u015F\u0131m\u0131m\u0131z|\u00E7\u00F6z\u00FCm(?:\u00FCm\u00FCz)?)\s+.{5,})/;
var ERROR_RE = /(?i)(?:(?:fixed|resolved|solved|workaround for)\s+.{5,}|(?:the fix|the solution|root cause)\s*(?:is|was|:)\s+|(?:error|bug|issue)\s+.{0,40}(?:was caused by|because|due to|fixed by)|(?:had to|needed to)\s+.{3,40}(?:because|due to|since)\s+.{3,}(?:error|bug|fail|broke|crash)|(?:d\u00FCzelttik?|\u00E7\u00F6zd\u00FCk?|giderdik?|fix.?ledik?)\s+.{5,}|(?:hata|bug|sorun)\s+.{0,40}(?:sebebi|nedeni|\u00E7\u00F6z\u00FCm\u00FC|d\u00FCzeltmesi)|(?:sorun \u015Fuydu|hata \u015Fuydu|sebebi \u015Fuydu)\s*(?::)?\s+)/;
var LEARNING_RE = /(?i)(?:(?:turns out|TIL|important to note|gotcha|pitfall|caveat|note:)\s*(?::|that|,)?\s+|(?:learned|discovered|realized|found out)\s+that\s+|(?:the (?:trick|key|insight|important thing) (?:is|was))\s+|(?:remember|important):\s+|(?:pro.?tip|heads.?up|watch out|be careful|don.?t forget)\s*(?::|,)\s+|(?:me\u011Fer|me\u011Ferse|anla\u015F\u0131lan)\s+.{5,}|(?:\u00F6\u011Frendik?|fark ettik?|ke\u015Ffettik?)\s+.{3,}|(?:dikkat|\u00F6nemli|unutma)(?::|\s).{5,})/;
var PREFERENCE_RE = /(?i)(?:(?:user\s+(?:prefers?|wants?|asked for|(?:does ?\x27?n.?t|never)\s+(?:want|like|use)))|(?:always use|never use|convention is|style preference|workflow:)|\[user\]\s+|(?:kullan\u0131c\u0131\s+(?:tercih|istiyor|istemiyor|istemez))|(?:her zaman|hi\u00E7bir zaman|asla|daima)\s+(?:kullan|yap|kullanma|yapma))/;
var TOOL_PREF_RE = /(?i)(?:(?:always use|prefer\s+\S+\s+over)\s+.{5,}|(?:works?\s+better\s+than)\s+.{5,}|(?:switched to|switching to)\s+\S+\s+(?:for|because)\s+.{5,}|(?:don.?t use|avoid using|stop using)\s+\S+\s+(?:because|for|since)\s+.{3,}|(?:prefer\s+\S+\s+for)\s+.{5,}|(?:hep\s+\S+\s+kullan)\s*.{5,}|(?:\S+.?[\u0131i]\s+tercih\s+et)\s*.{5,}|(?:daha\s+iyi\s+\u00E7al\u0131\u015F\u0131yor)\s*.{3,}|(?:\S+\s+kullanma\s+\u00E7\u00FCnk\u00FC)\s+.{5,})/;
var ARCH_RE = /(?i)(?:(?:the\s+architecture\s+is)\s+.{5,}|(?:we\s+structured\s+it\s+as)\s+.{5,}|(?:the\s+pattern\s+we\s+use\s+is)\s+.{5,}|(?:built\s+on\s+top\s+of)\s+.{5,}|(?:the\s+design\s+is)\s+.{5,}|(?:using\s+\S+\s+(?:pattern|approach|architecture))\s+.{3,}|(?:the\s+(?:system|service|module|component)\s+(?:is structured|is designed|follows))\s+.{5,}|(?:mimari(?:si|miz)?\s+(?:\u015F\u00F6yle|b\u00F6yle|olarak))\s*.{5,}|(?:yap\u0131(?:s\u0131|m\u0131z)?\s+(?:\u015F\u00F6yle|b\u00F6yle|olarak))\s*.{5,}|(?:tasar\u0131m(?:\u0131|\u0131m\u0131z)?\s+(?:\u015F\u00F6yle|b\u00F6yle|olarak))\s*.{5,}|(?:bunun\s+\u00FCzerine\s+kurduk)\s*.{5,}|(?:yakla\u015F\u0131m\s+olarak)\s+.{5,})/;
var WORKFLOW_RE = /(?i)(?:(?:the\s+workflow\s+is)\s*(?::)?\s+.{5,}|(?:the\s+process\s+is)\s*(?::)?\s+.{5,}|(?:first\s+\S+\s+then)\s+.{5,}|(?:deploy\s+with)\s+.{5,}|(?:run\s+\S+\s+before)\s+.{5,}|(?:the\s+pipeline\s+is)\s*(?::)?\s+.{5,}|(?:the\s+(?:build|release|test|ci)\s+(?:process|pipeline|flow)\s+(?:is|goes|works))\s+.{5,}|(?:step\s+\d+\s*(?::|is|,))\s+.{5,}|(?:i\u015F\s*ak\u0131\u015F\u0131(?:m\u0131z)?\s*(?::|\u015F\u00F6yle|b\u00F6yle))\s*.{5,}|(?:s\u00FCre\u00E7\s*(?::|\u015F\u00F6yle|b\u00F6yle))\s*.{5,}|(?:\u00F6nce\s+\S+\s+sonra)\s+.{5,}|(?:deploy\s+i\u00E7in)\s+.{5,}|(?:s\u0131ras\u0131yla)\s+.{5,})/;
var FILE_CONV_RE = /(?i)(?:(?:files?\s+go\s+in)\s+.{5,}|(?:naming\s+convention\s+(?:is|for))\s+.{5,}|(?:put\s+\S+\s+in\s+(?:the\s+)?\S+\s+directory)\s*.{3,}|(?:file\s+structure\s+(?:is|looks))\s+.{5,}|(?:organized\s+as)\s+.{5,}|(?:(?:directory|folder)\s+(?:structure|layout|convention)\s+(?:is|for))\s+.{5,}|(?:dosyalar\s+\S+.?[ea]\s+konur)\s*.{3,}|(?:isimlendirme\s+kural\u0131)\s*.{5,}|(?:dosya\s+yap\u0131s\u0131)\s*.{5,}|(?:d\u00FCzen\s+olarak)\s+.{5,})/;
var CORRECTION_RE = /(?i)(?:(?:not\s+.{3,30}(?:,\s*|\s+but\s+)(?:it.?s|actually)\s+.{3,30})|(?:(?:wrong|incorrect)\s+.{0,20}(?:should be|is actually)\s+.{3,30})|(?:changed?\s+(?:from|my)\s+.{3,30}\s+to\s+.{3,30})|(?:(?:yanl\u0131\u015F|hatal\u0131)\s+.{3,30}(?:asl\u0131nda|art\u0131k|olarak)\s+.{3,30})|(?:(?:de\u011Fil)\s+.{3,30}(?:art\u0131k|\u015Fimdi)\s+.{3,30}))/;
var INFRA_RE = /(?i)(?:(?:(?:server|host|ip|vps|ssh)\s+.{0,30}(?:\d{1,3}\.){3}\d{1,3})|(?:ssh\s+[-\w]+@[\w.-]+)|(?:(?:version|s\u00FCr\u00FCm)\s+.{0,10}v?\d+\.\d+)|(?:port\s+\d{2,5}))/;
var CONTENT_RE = /(?i)(?:(?:(?:blog|article)\s+.{0,30}(?:published|approved|rejected|haz\u0131r|onayland\u0131))|(?:(?:editorial|content)\s+decision\s*:\s+.{5,})|(?:(?:do not|never|asla)\s+(?:write|post|publish|mention)\s+.{5,})|(?:(?:review|feedback)\s*:\s+.{5,}))/;
var IMPLICIT_DECISION_RE = /(?i)(?:(?:let.?s\s+(?:go\s+with|use|try|pick|choose|stick with)\s+.{3,80})|(?:going\s+to\s+use\s+.{3,80})|(?:plan\s+is\s+to\s+.{3,80})|(?:(?:I|we).?(?:ll|will)\s+(?:go with|use|try|pick)\s+.{3,80})|(?:(?:better|best)\s+to\s+(?:use|go with|try)\s+.{3,80})|(?:(?:yapaca\u011F\u0131z|kullanal\u0131m|ge\u00E7elim|deneyelim|se\u00E7elim)\s+.{3,80})|(?:(?:bununla|bunu|\u015Funu)\s+(?:gidelim|deneyelim|kullanal\u0131m)\s*.{0,80})|(?:(?:en iyisi|daha iyi)\s+.{3,80}))/;
var REASON_RE = /(?i)(?:(?:because\s+.{10,200})|(?:since\s+.{10,200})|(?:the\s+reason\s+(?:is|was|being)\s+.{10,200})|(?:due\s+to\s+.{10,200})|(?:this\s+is\s+(?:because|since|due to)\s+.{10,200})|(?:\u00E7\u00FCnk\u00FC\s+.{10,200})|(?:(?:nedeni|sebebi|sebebiyle)\s+.{10,200})|(?:(?:bunun\s+nedeni|bunun\s+sebebi)\s+.{10,200}))/;
var BLOCKER_RE = /(?i)(?:(?:blocked\s+by\s+.{5,150})|(?:waiting\s+for\s+.{5,150})|(?:can.?t\s+proceed\s+.{5,150})|(?:stuck\s+on\s+.{5,150})|(?:(?:depends|dependent)\s+on\s+.{5,150})|(?:need\s+to\s+(?:wait|resolve|fix)\s+.{5,150})|(?:(?:bekliyor|tak\u0131ld\u0131k|t\u0131kand\u0131k)\s+.{5,150})|(?:(?:buna\s+ba\u011Fl\u0131|bundan\s+\u00F6nce)\s+.{5,150})|(?:(?:\u00E7\u00F6zmemiz|d\u00FCzeltmemiz)\s+(?:laz\u0131m|gerek)\s*.{0,150}))/;
var SUMMARY_MARKERS = [
  "# Session Summary",
  "## Decisions Made",
  "## Errors & Fixes",
  "## Key Learnings",
  "## User Preferences",
  "## What Was Done",
  "## Sprint Handoff",
  "## User Requests",
  "## Files Modified"
];
function summaryFilter(text) {
  if (!text)
    return false;
  let count = 0;
  for (const marker of SUMMARY_MARKERS) {
    if (text.includes(marker)) {
      count++;
      if (count >= 2)
        return true;
    }
  }
  return false;
}
var PREFIX_RE = /^\[([^\]]{2,30})\]/;
var PREFIX_MAP = {
  decision: "decision",
  "error fix": "error_fix",
  error: "error_fix",
  gotcha: "learning",
  learning: "learning",
  preference: "preference",
  progress: "observation",
  observation: "observation",
  architecture: "knowledge",
  pattern: "knowledge",
  reference: "knowledge",
  review: "knowledge",
  note: "knowledge",
  handoff: "session_summary",
  audit: "knowledge",
  test: "knowledge"
};
function classifyByPrefix(content) {
  if (!content)
    return null;
  const m = content.trim().match(PREFIX_RE);
  if (!m)
    return null;
  const tag = m[1].trim().toLowerCase();
  for (const [key, typ] of Object.entries(PREFIX_MAP)) {
    if (tag.includes(key))
      return { type: typ, confidence: 1 };
  }
  return null;
}
function scoreExtraction(text, category) {
  let score = 0;
  const tl = text.toLowerCase();
  const hasAny = (words) => words.some((w) => tl.includes(w));
  switch (category) {
    case "decision":
      if (hasAny(["instead of", "over", "rather than", "because", "tradeoff", "yerine", "\xE7\xFCnk\xFC", "sebebiyle", "nedeniyle"]))
        score += 2;
      if (hasAny(["chose", "decided", "selected", "opted", "karar", "se\xE7tik", "tercih", "gidelim"]))
        score += 1;
      break;
    case "error": {
      const hasProblem = hasAny(["error", "bug", "crash", "fail", "broke", "hata", "sorun", "\xE7\xF6kt\xFC", "bozuldu"]);
      const hasResolution = hasAny(["fixed", "resolved", "solved", "workaround", "caused by", "root cause", "d\xFCzelttik", "\xE7\xF6zd\xFCk", "giderdik", "sebebi", "nedeni"]);
      if (hasProblem && hasResolution)
        score += 3;
      break;
    }
    case "learning":
      if (hasAny(["turns out", "gotcha", "pitfall", "caveat", "important to note", "me\u011Fer", "me\u011Ferse", "anla\u015F\u0131lan", "dikkat", "\xF6nemli"]))
        score += 2;
      if (hasAny(["because", "so that", "\xE7\xFCnk\xFC", "dolay\u0131"]))
        score += 1;
      break;
    case "preference":
      if (hasAny(["always", "never", "prefer", "convention", "her zaman", "asla", "hi\xE7bir zaman", "tercih"]))
        score += 1;
      if (hasAny(["user", "[user]", "kullan\u0131c\u0131"]))
        score += 2;
      break;
    case "tool_pref":
      if (hasAny(["always", "never", "prefer", "better", "works better", "hep", "asla", "tercih", "daha iyi"]))
        score += 2;
      if (hasAny(["because", "instead of", "over", "\xE7\xFCnk\xFC", "yerine"]))
        score += 1;
      break;
    case "arch":
      if (hasAny(["architecture", "pattern", "design", "structure", "layer", "mimari", "tasar\u0131m", "yap\u0131", "katman"]))
        score += 1;
      if (hasAny(["because", "so that", "enables", "\xE7\xFCnk\xFC", "sa\u011Flar"]))
        score += 1;
      break;
    case "workflow":
      if (hasAny(["first", "then", "before", "after", "step", "pipeline", "\xF6nce", "sonra", "ad\u0131m", "s\u0131ras\u0131yla"]))
        score += 2;
      break;
    case "file_conv":
      if (hasAny(["directory", "folder", "path", "naming", "convention", "dizin", "klas\xF6r", "dosya", "isimlendirme"]))
        score += 2;
      break;
    case "correction":
      if (hasAny(["not", "actually", "wrong", "incorrect", "should be", "de\u011Fil", "yanl\u0131\u015F", "hatal\u0131", "asl\u0131nda"]))
        score += 2;
      if (hasAny(["changed", "updated", "renamed", "de\u011Fi\u015Ftirdik"]))
        score += 1;
      break;
    case "infra":
      if (/(?:\d{1,3}\.){3}\d{1,3}/.test(text))
        score += 2;
      if (/port\s+\d{2,5}/.test(tl))
        score += 1;
      if (/v?\d+\.\d+/.test(text))
        score += 1;
      if (hasAny(["trying", "test", "debug", "attempt"]))
        score -= 2;
      break;
    case "content":
      if (hasAny(["approved", "published", "onayland\u0131", "yay\u0131nland\u0131"]))
        score += 2;
      if (hasAny(["never", "always", "asla", "her zaman"]))
        score += 1;
      break;
  }
  if (text.length < 40)
    score -= 1;
  if (/[/\\][\w.-]+\.\w+/.test(text))
    score += 1;
  if (/v?\d+\.\d+/.test(text))
    score += 1;
  if (/(?:npm|pip|brew|cargo|go|docker|git|kubectl|yarn|bun)\s/.test(tl))
    score += 1;
  return score;
}
var PATTERN_TABLE = [
  [DECISION_RE, "decision", 8],
  [IMPLICIT_DECISION_RE, "implicit_decision", 7],
  [ERROR_RE, "error", 8],
  [LEARNING_RE, "learning", 7],
  [PREFERENCE_RE, "preference", 9],
  [TOOL_PREF_RE, "tool_pref", 7],
  [ARCH_RE, "architecture", 7],
  [WORKFLOW_RE, "workflow", 6],
  [CORRECTION_RE, "correction", 8],
  [REASON_RE, "reasoning", 6],
  [BLOCKER_RE, "blocker", 8],
  [FILE_CONV_RE, "file_convention", 6],
  [INFRA_RE, "infrastructure", 5],
  [CONTENT_RE, "content", 6]
];
function extractPatterns(text, maxLen = 500) {
  if (!text || text.length < 20)
    return [];
  if (summaryFilter(text.slice(0, 2000)))
    return [];
  const prefixResult = classifyByPrefix(text);
  if (prefixResult) {
    return [{ content: text.slice(0, 300), category: prefixResult.type, score: 9 }];
  }
  const matches = [];
  const seen = new Set;
  for (const [regex, category, baseScore] of PATTERN_TABLE) {
    const flags = regex.flags.includes("g") ? regex.flags : regex.flags + "g";
    const globalRegex = new RegExp(regex.source, flags);
    let m;
    while ((m = globalRegex.exec(text)) !== null) {
      const matchText = m[0].trim();
      if (matchText.length < 15 || matchText.length > maxLen)
        continue;
      const key = matchText.slice(0, 80);
      if (seen.has(key))
        continue;
      seen.add(key);
      matches.push({ content: matchText, category, score: baseScore });
    }
  }
  return matches;
}
function dedup(items, maxCount = 5) {
  const seen = new Set;
  const result = [];
  for (const item of items) {
    const short = item.slice(0, 80);
    if (!seen.has(short) && result.length < maxCount) {
      result.push(item);
      seen.add(short);
    }
  }
  return result;
}

// src/lib/state.ts
import { renameSync, existsSync as existsSync4, mkdirSync as mkdirSync2 } from "fs";
import { join as join5, dirname } from "path";
var MAX_ACTIVE_FILES = 20;
var MAX_MODIFIED_FILES = 15;
var MAX_SEARCH_PATTERNS = 10;
var FEEDBACK_MAX_LINES = 5000;
var FEEDBACK_TRIM_TO = 2500;
function atomicWrite(filePath, content) {
  const dir = dirname(filePath);
  if (!existsSync4(dir)) {
    mkdirSync2(dir, { recursive: true });
  }
  const tmpPath = filePath + ".tmp";
  Bun.write(tmpPath, content);
  renameSync(tmpPath, filePath);
}
function atomicWriteJSON(filePath, data) {
  atomicWrite(filePath, JSON.stringify(data, null, 2) + `
`);
}
function createWorkingMemory(sessionId) {
  return {
    active_files: [],
    modified_files: [],
    search_patterns: [],
    session_id: sessionId,
    updated_at: Date.now()
  };
}
function createSessionState(project, cwd, setupContext) {
  return {
    startTime: Date.now(),
    project,
    cwd,
    setupContext,
    callCount: 0,
    lastCheckpoint: Date.now()
  };
}
function addActiveFile(memory, filePath) {
  const filtered = memory.active_files.filter((f) => f !== filePath);
  filtered.unshift(filePath);
  return {
    ...memory,
    active_files: filtered.slice(0, MAX_ACTIVE_FILES),
    updated_at: Date.now()
  };
}
function addModifiedFile(memory, filePath) {
  const filtered = memory.modified_files.filter((f) => f !== filePath);
  filtered.unshift(filePath);
  return {
    ...memory,
    modified_files: filtered.slice(0, MAX_MODIFIED_FILES),
    updated_at: Date.now()
  };
}
function addSearchPattern(memory, pattern) {
  const filtered = memory.search_patterns.filter((p) => p !== pattern);
  filtered.unshift(pattern);
  return {
    ...memory,
    search_patterns: filtered.slice(0, MAX_SEARCH_PATTERNS),
    updated_at: Date.now()
  };
}
async function loadWorkingMemory(stagingDir) {
  const filePath = join5(stagingDir, "working-memory.json");
  if (!existsSync4(filePath))
    return null;
  try {
    const file = Bun.file(filePath);
    const text = await file.text();
    return JSON.parse(text);
  } catch {
    return null;
  }
}
function saveWorkingMemory(stagingDir, memory) {
  const filePath = join5(stagingDir, "working-memory.json");
  atomicWriteJSON(filePath, memory);
}
async function appendFeedback(stagingDir, entry) {
  const filePath = join5(stagingDir, "feedback.jsonl");
  if (!existsSync4(stagingDir)) {
    mkdirSync2(stagingDir, { recursive: true });
  }
  const line = JSON.stringify(entry) + `
`;
  const file = Bun.file(filePath);
  let existing = "";
  if (existsSync4(filePath)) {
    existing = await file.text();
  }
  const combined = existing + line;
  const lines = combined.split(`
`).filter((l) => l.trim().length > 0);
  if (lines.length > FEEDBACK_MAX_LINES) {
    const trimmed = lines.slice(-FEEDBACK_TRIM_TO);
    atomicWrite(filePath, trimmed.join(`
`) + `
`);
  } else {
    Bun.write(filePath, line, { create: true, append: true });
  }
}

// src/hooks/post-tool.ts
var B12_BASE4 = process.env.B12_DATA_DIR || join6(homedir5(), ".B12");
var CHECKPOINT_CALL_INTERVAL = 15;
var CHECKPOINT_TIME_INTERVAL = 600000;
async function postTool(input, output, deps) {
  const { db, project, sessionId, sessionState, workingMemory } = deps;
  const toolName = input.tool;
  const result = { sessionState, workingMemory, surfaced: undefined };
  if (toolName === "B12_memory_store" || toolName === "mcp__B12__memory_store" || toolName === "B12_memory_search" || toolName === "mcp__B12__memory_search" || toolName === "B12_memory_update" || toolName === "mcp__B12__memory_update") {
    return result;
  }
  let entity = "";
  let entityType = "file";
  if (toolName === "read" || toolName === "edit" || toolName === "write") {
    const fp = input.args.filePath || output.args.filePath || "";
    if (fp) {
      entity = basename2(fp);
      if (toolName === "edit" || toolName === "write") {
        entityType = "modified";
      }
    }
  } else if (toolName === "glob" || toolName === "grep") {
    const pattern = input.args.pattern || "";
    if (pattern) {
      entity = pattern.slice(0, 80);
      entityType = "search";
    }
  }
  if (entity) {
    if (entityType === "modified") {
      result.workingMemory = addModifiedFile(result.workingMemory, entity);
      result.workingMemory = addActiveFile(result.workingMemory, entity);
    } else if (entityType === "file") {
      result.workingMemory = addActiveFile(result.workingMemory, entity);
    } else if (entityType === "search") {
      result.workingMemory = addSearchPattern(result.workingMemory, entity);
    }
    const stagingDir = join6(B12_BASE4, "memory-staging");
    saveWorkingMemory(stagingDir, result.workingMemory);
  }
  if (toolName === "read" || toolName === "edit") {
    const filePath = input.args.filePath || output.args.filePath || "";
    if (filePath) {
      const memResults = db.search({
        query: basename2(filePath),
        tags: [`proj:${project}`],
        limit: 3
      });
      if (memResults.length > 0) {
        result.surfaced = memResults.map((m) => m.display).join(`
`);
      }
    }
  }
  if (toolName === "bash") {
    const cmdOutput = output.result || "";
    const errorIndicators = [
      "error",
      "failed",
      "exception",
      "traceback",
      "errno",
      "permission denied",
      "not found",
      "command not found",
      "hata",
      "ba\u015Far\u0131s\u0131z"
    ];
    const hasError = errorIndicators.some((ind) => cmdOutput.toLowerCase().includes(ind));
    if (hasError) {
      const errorMemories = db.search({
        query: cmdOutput.slice(0, 200),
        mode: "hybrid",
        limit: 3
      });
      if (errorMemories.length > 0) {
        const surfaceText = errorMemories.map((m) => m.display).join(`
`);
        result.surfaced = result.surfaced ? `${result.surfaced}

## Related Error Memories
${surfaceText}` : `## Related Error Memories
${surfaceText}`;
      }
    }
  }
  result.sessionState = {
    ...result.sessionState,
    callCount: result.sessionState.callCount + 1
  };
  const elapsed = Date.now() - result.sessionState.lastCheckpoint;
  if (result.sessionState.callCount % CHECKPOINT_CALL_INTERVAL === 0 || elapsed >= CHECKPOINT_TIME_INTERVAL) {
    await runCheckpoint(input, output, db, project);
    result.sessionState = {
      ...result.sessionState,
      lastCheckpoint: Date.now()
    };
  }
  try {
    const stagingDir = join6(B12_BASE4, "memory-staging");
    await appendFeedback(stagingDir, {
      timestamp: Math.floor(Date.now() / 1000),
      session_id: sessionId,
      type: "tool_usage",
      data: { tool: toolName, entity, entityType }
    });
  } catch {}
  return result;
}
async function runCheckpoint(input, output, db, project) {
  const parts = [];
  const inputContent = [
    typeof input.args.content === "string" ? input.args.content : "",
    typeof input.args.command === "string" ? input.args.command : "",
    typeof input.args.filePath === "string" ? input.args.filePath : "",
    typeof input.args.pattern === "string" ? input.args.pattern : ""
  ].filter(Boolean).join(`
`);
  const outputContent = typeof output.result === "string" ? output.result : "";
  const scanText = (inputContent + `
` + outputContent).slice(0, 4000);
  if (scanText.length < 20)
    return;
  if (summaryFilter(scanText.slice(0, 2000)))
    return;
  const extractions = extractPatterns(scanText);
  if (extractions.length === 0)
    return;
  const scored = extractions.filter((e) => e.score >= 6).slice(0, 5);
  if (scored.length === 0)
    return;
  for (const item of scored) {
    try {
      const content = `[${item.category}] ${item.content.slice(0, 300)}`;
      const tags = `proj:${project},checkpoint,${item.category}`;
      const meta = {
        type: item.category,
        source: "checkpoint",
        importance_score: 0.7,
        project
      };
      db.store({
        content,
        tags,
        memory_type: item.category,
        metadata: meta
      });
    } catch {}
  }
}

// src/hooks/pre-compact.ts
import { join as join7 } from "path";
import { mkdirSync as mkdirSync3, writeFileSync, readdirSync as readdirSync2, statSync, unlinkSync, renameSync as renameSync2 } from "fs";
import { homedir as homedir6 } from "os";
var B12_BASE5 = process.env.B12_DATA_DIR || join7(homedir6(), ".B12");
var CHAR_BUDGET2 = 8000;
var PRIORITY_WEIGHTS = {
  decision: 10,
  error_fix: 9,
  learning: 8,
  preference: 8,
  file_modified: 7,
  user_request: 6,
  progress: 5,
  general_work: 2
};
async function preCompact(messages, sessionId, project, cwd, db) {
  const stagingDir = join7(B12_BASE5, "memory-staging");
  mkdirSync3(stagingDir, { recursive: true });
  const scoredItems = [];
  const userMessages = [];
  const filesModified = new Set;
  for (const msg of messages) {
    if (msg.role === "user") {
      const text = msg.content.trim();
      if (text)
        userMessages.push(text.slice(0, 300));
    } else if (msg.role === "assistant") {
      const text = msg.content.trim();
      if (!text || text.length < 100)
        continue;
      const snippet = text.slice(0, 400);
      if (summaryFilter(text.slice(0, 2000)))
        continue;
      const extractions = extractPatterns(snippet);
      for (const ext of extractions) {
        const priority = PRIORITY_WEIGHTS[ext.category] ?? PRIORITY_WEIGHTS["general_work"];
        scoredItems.push({ priority, category: ext.category, text: ext.content.slice(0, 200) });
      }
    }
  }
  scoredItems.sort((a, b) => b.priority - a.priority);
  const seen = new Set;
  const uniqueItems = [];
  for (const item of scoredItems) {
    const key = item.text.slice(0, 80);
    if (!seen.has(key)) {
      uniqueItems.push(item);
      seen.add(key);
    }
  }
  const lines = [];
  lines.push(`Project: ${project}`);
  lines.push(`Session: ${sessionId.slice(0, 12)}`);
  lines.push(`User messages: ${userMessages.length}`);
  lines.push("");
  lines.push("USER REQUESTS:");
  for (const msg of userMessages.slice(-10)) {
    lines.push(`  - ${msg.slice(0, 200)}`);
  }
  lines.push("");
  let charUsed = lines.join(`
`).length;
  lines.push("RECENT WORK:");
  for (const item of uniqueItems) {
    const entry = `  [${item.category}] ${item.text.slice(0, 300)}`;
    if (charUsed + entry.length > CHAR_BUDGET2)
      break;
    lines.push(entry);
    charUsed += entry.length;
  }
  lines.push("");
  if (filesModified.size > 0) {
    lines.push("FILES MODIFIED:");
    for (const f of [...filesModified].sort().slice(0, 20)) {
      lines.push(`  - ${f}`);
    }
  }
  const summary = lines.join(`
`);
  const stageFile = join7(stagingDir, `precompact-${sessionId}.txt`);
  const tmpFile = stageFile + ".tmp";
  writeFileSync(tmpFile, summary, "utf-8");
  try {
    unlinkSync(stageFile);
  } catch {}
  try {
    renameSync2(tmpFile, stageFile);
  } catch {
    writeFileSync(stageFile, summary, "utf-8");
  }
  const highValue = uniqueItems.filter((item) => item.priority >= 8 && item.text.length > 30).slice(0, 5);
  if (highValue.length > 0) {
    await storeHighValue(highValue, project, cwd, db);
  }
  cleanupOldStaging(stagingDir);
  return summary;
}
async function storeHighValue(items, project, cwd, db) {
  const texts = items.map((item) => `[${item.category}] ${item.text}`);
  let embeddings = null;
  try {
    embeddings = await encodeBatch(texts);
  } catch {}
  for (let i = 0;i < items.length; i++) {
    const item = items[i];
    const prefixed = texts[i];
    try {
      db.store({
        content: prefixed,
        tags: `proj:${project},precompact-save,${item.category},${new Date().toISOString().slice(0, 7)}`,
        memory_type: item.category,
        metadata: {
          project,
          type: item.category,
          importance_score: 1.5,
          source: "precompact",
          extraction_method: "precompact_plugin"
        }
      });
    } catch {}
  }
}
function cleanupOldStaging(stagingDir) {
  try {
    const files = readdirSync2(stagingDir).filter((f) => f.startsWith("precompact-") && f.endsWith(".txt")).map((f) => ({ name: f, path: join7(stagingDir, f), mtime: statSync(join7(stagingDir, f)).mtimeMs })).sort((a, b) => b.mtime - a.mtime);
    const twoHoursAgo = Date.now() - 2 * 60 * 60 * 1000;
    for (const file of files) {
      if (file.mtime < twoHoursAgo) {
        try {
          unlinkSync(file.path);
        } catch {}
      }
    }
  } catch {}
}

// src/hooks/session-end.ts
import { join as join8 } from "path";
import { existsSync as existsSync6, mkdirSync as mkdirSync4, writeFileSync as writeFileSync2, unlinkSync as unlinkSync2 } from "fs";
import { homedir as homedir7 } from "os";
var B12_BASE6 = process.env.B12_DATA_DIR || join8(homedir7(), ".B12");
async function sessionEnd(messages, sessionId, project, cwd, db) {
  const summaryDir = join8(B12_BASE6, "memory-summaries");
  mkdirSync4(summaryDir, { recursive: true });
  const decisions = [];
  const errors = [];
  const learnings = [];
  const preferences = [];
  const architecture = [];
  const workflows = [];
  const fileConventions = [];
  const corrections = [];
  const infrastructure = [];
  const contentItems = [];
  const userRequests = [];
  const filesModified = [];
  for (const msg of messages) {
    if (msg.role === "user") {
      const text2 = msg.content.trim();
      if (text2 && text2.length > 5) {
        userRequests.push(text2.slice(0, 300));
      }
      continue;
    }
    if (msg.role !== "assistant")
      continue;
    const text = msg.content;
    if (!text || text.length < 50)
      continue;
    if (summaryFilter(text.slice(0, 2000)))
      continue;
    const extractions = extractPatterns(text);
    for (const ext of extractions) {
      const score = scoreExtraction(ext.content, ext.category);
      if (score < 2)
        continue;
      switch (ext.category) {
        case "decision":
        case "implicit_decision":
          decisions.push(ext.content.slice(0, 300));
          break;
        case "error":
          errors.push(ext.content.slice(0, 300));
          break;
        case "learning":
          learnings.push(ext.content.slice(0, 300));
          break;
        case "preference":
        case "tool_pref":
          preferences.push(ext.content.slice(0, 300));
          break;
        case "architecture":
          architecture.push(ext.content.slice(0, 300));
          break;
        case "workflow":
          workflows.push(ext.content.slice(0, 300));
          break;
        case "file_convention":
          fileConventions.push(ext.content.slice(0, 300));
          break;
        case "correction":
          corrections.push(ext.content.slice(0, 300));
          break;
        case "infrastructure":
          infrastructure.push(ext.content.slice(0, 300));
          break;
        case "content":
          contentItems.push(ext.content.slice(0, 300));
          break;
      }
    }
    const fileMatches = text.match(/(?:^|\s)([\w./\-]+\.\w{1,10})(?:\s|$)/g);
    if (fileMatches) {
      for (const fm of fileMatches) {
        const cleaned = fm.trim();
        if (cleaned.length > 3 && cleaned.includes(".")) {
          filesModified.push(cleaned);
        }
      }
    }
  }
  const summaryLines = [];
  summaryLines.push(`# Session Summary (${new Date().toISOString().slice(0, 10)})`);
  summaryLines.push(`Project: ${project} | Session: ${sessionId.slice(0, 12)}`);
  summaryLines.push("");
  if (decisions.length > 0) {
    summaryLines.push("## Decisions Made");
    for (const d of dedup(decisions))
      summaryLines.push(`- ${d}`);
    summaryLines.push("");
  }
  if (errors.length > 0) {
    summaryLines.push("## Errors & Fixes");
    for (const e of dedup(errors))
      summaryLines.push(`- ${e}`);
    summaryLines.push("");
  }
  if (learnings.length > 0) {
    summaryLines.push("## Key Learnings");
    for (const l of dedup(learnings))
      summaryLines.push(`- ${l}`);
    summaryLines.push("");
  }
  if (preferences.length > 0) {
    summaryLines.push("## User Preferences");
    for (const p of dedup(preferences))
      summaryLines.push(`- ${p}`);
    summaryLines.push("");
  }
  if (architecture.length > 0) {
    summaryLines.push("## Architecture");
    for (const a of dedup(architecture))
      summaryLines.push(`- ${a}`);
    summaryLines.push("");
  }
  if (workflows.length > 0) {
    summaryLines.push("## Workflows");
    for (const w of dedup(workflows))
      summaryLines.push(`- ${w}`);
    summaryLines.push("");
  }
  if (fileConventions.length > 0) {
    summaryLines.push("## File Conventions");
    for (const f of dedup(fileConventions))
      summaryLines.push(`- ${f}`);
    summaryLines.push("");
  }
  if (corrections.length > 0) {
    summaryLines.push("## Corrections");
    for (const c of dedup(corrections))
      summaryLines.push(`- ${c}`);
    summaryLines.push("");
  }
  if (userRequests.length > 0) {
    summaryLines.push("## User Requests");
    for (const r of dedup(userRequests, 10))
      summaryLines.push(`- ${r}`);
    summaryLines.push("");
  }
  if (filesModified.length > 0) {
    const unique = [...new Set(filesModified)].sort().slice(0, 30);
    summaryLines.push("## Files Modified");
    for (const f of unique)
      summaryLines.push(`- ${f}`);
    summaryLines.push("");
  }
  const summary = summaryLines.join(`
`);
  const projectSummaryFile = join8(summaryDir, `${project}-latest.md`);
  atomicWrite2(projectSummaryFile, summary);
  const globalSummaryFile = join8(summaryDir, "global-latest.md");
  atomicWrite2(globalSummaryFile, summary.slice(0, 2000));
  const handoffFile = join8(summaryDir, `${project}-handoff.md`);
  atomicWrite2(handoffFile, summary);
  const allItems = [
    ...decisions.map((d) => ({ content: d, category: "decision", score: 8 })),
    ...errors.map((e) => ({ content: e, category: "error", score: 8 })),
    ...learnings.map((l) => ({ content: l, category: "learning", score: 7 })),
    ...preferences.map((p) => ({ content: p, category: "preference", score: 9 })),
    ...architecture.map((a) => ({ content: a, category: "architecture", score: 7 })),
    ...workflows.map((w) => ({ content: w, category: "workflow", score: 6 })),
    ...fileConventions.map((f) => ({ content: f, category: "file_convention", score: 6 })),
    ...corrections.map((c) => ({ content: c, category: "correction", score: 8 })),
    ...infrastructure.map((i) => ({ content: i, category: "infrastructure", score: 5 })),
    ...contentItems.map((c) => ({ content: c, category: "content", score: 6 }))
  ];
  allItems.sort((a, b) => b.score - a.score);
  const topItems = allItems.filter((item) => item.score >= 6).slice(0, 20);
  let embeddings = null;
  if (topItems.length > 0) {
    const texts = topItems.map((item) => `[${item.category}] ${item.content}`);
    try {
      embeddings = await encodeBatch(texts);
    } catch {}
  }
  for (let i = 0;i < topItems.length; i++) {
    const item = topItems[i];
    const prefixed = `[${item.category}] ${item.content}`;
    try {
      db.store({
        content: prefixed,
        tags: `proj:${project},session-end,${item.category},${new Date().toISOString().slice(0, 7)}`,
        memory_type: item.category,
        metadata: {
          type: item.category,
          source: "session_end",
          importance_score: item.score >= 8 ? 1.5 : 1,
          project,
          session_id: sessionId.slice(0, 12),
          extraction_method: "session_end_plugin"
        }
      });
    } catch {}
  }
}
function atomicWrite2(filePath, content) {
  const dir = join8(filePath, "..");
  if (!existsSync6(dir))
    mkdirSync4(dir, { recursive: true });
  const tmpFile = filePath + ".tmp";
  writeFileSync2(tmpFile, content, "utf-8");
  try {
    __require("fs").renameSync(tmpFile, filePath);
  } catch {
    writeFileSync2(filePath, content, "utf-8");
    try {
      unlinkSync2(tmpFile);
    } catch {}
  }
}

// src/index.ts
var B12_BASE7 = process.env.B12_DATA_DIR || join9(homedir8(), ".B12");
async function fetchSessionMessages(client, sessionId) {
  const rawMsgs = await client.session.messages({ path: { id: sessionId } });
  const msgs = [];
  for (const m of rawMsgs) {
    const text = m.parts?.filter((p) => p.type === "text" && p.text).map((p) => p.text).join(`
`);
    if (text) {
      msgs.push({ role: m.info.role, content: text });
    }
  }
  return msgs;
}
var B12Plugin = async (ctx) => {
  const { client, directory } = ctx;
  const dbPath = getDbPath();
  let db = null;
  try {
    if (existsSync7(dbPath)) {
      db = new B12Database(dbPath);
    }
  } catch {
    return {};
  }
  const projectName = basename4(directory);
  const sessionId = "oc-" + Date.now().toString(36);
  const sessionState = createSessionState(projectName, directory, "opencode");
  const loaded = await loadWorkingMemory(join9(B12_BASE7, "memory-staging"));
  const workingMemory = loaded ?? createWorkingMemory(sessionId);
  const state = {
    db,
    sessionState,
    workingMemory,
    sessionId,
    isFirstMessage: true,
    currentSessionId: null
  };
  return {
    "experimental.chat.system.transform": async (_input, output) => {
      if (!state.isFirstMessage || !state.db)
        return;
      state.isFirstMessage = false;
      try {
        const context = await sessionStart(projectName, directory, state.db);
        if (context) {
          output.system.push(context);
        }
      } catch {}
    },
    "permission.ask": async (input, output) => {
      const isMemoryTool = input.title.includes("memory_store") || input.title.includes("memory_search") || input.title.includes("memory_update") || input.title.includes("memory_quality") || input.pattern && (typeof input.pattern === "string" && input.pattern.includes("B12_memory") || Array.isArray(input.pattern) && input.pattern.some((p) => p.includes("B12_memory")));
      if (isMemoryTool) {
        output.status = "allow";
      }
    },
    "chat.params": async (_input, output) => {
      output.options.thinking = { type: "enabled", clear_thinking: true };
    },
    "chat.message": async (input, output) => {
      if (!state.db)
        return;
      if (input.sessionID && !state.currentSessionId) {
        state.currentSessionId = input.sessionID;
      }
      const userText = output.parts?.filter((p) => p.type === "text" && p.text).map((p) => p.text).join(" ").trim();
      if (!userText || userText.length < 10 || userText.startsWith("/"))
        return;
      if (/^(selam|merhaba|naber|hey|hi|hello|good morning)/i.test(userText))
        return;
      try {
        await messageRetrieval(userText, projectName, state.db);
      } catch {}
    },
    "tool.execute.before": async (input, output) => {
      tagEnforce(input, output, projectName, "opencode");
    },
    "tool.execute.after": async (input, output) => {
      if (!state.db)
        return;
      try {
        const result = await postTool({
          tool: input.tool,
          args: input.args,
          result: output.output
        }, {
          args: output,
          result: output.output
        }, {
          db: state.db,
          project: projectName,
          cwd: directory,
          sessionId: state.sessionId,
          sessionState: state.sessionState,
          workingMemory: state.workingMemory
        });
        state.sessionState = result.sessionState;
        state.workingMemory = result.workingMemory;
      } catch {}
    },
    "experimental.session.compacting": async (input, output) => {
      if (!state.db || !state.currentSessionId)
        return;
      try {
        const msgs = await fetchSessionMessages(client, state.currentSessionId);
        const summary = await preCompact(msgs, state.sessionId, projectName, directory, state.db);
        output.context.push(summary);
      } catch {}
    },
    event: async (input) => {
      const evt = input.event;
      if (evt.type !== "session.idle" && evt.type !== "session.deleted")
        return;
      if (!state.db)
        return;
      const sid = evt.properties?.sessionID || state.currentSessionId || state.sessionId;
      try {
        let msgs = [];
        if (evt.type === "session.idle" && sid) {
          try {
            msgs = await fetchSessionMessages(client, sid);
          } catch {}
        }
        await sessionEnd(msgs, state.sessionId, projectName, directory, state.db);
      } catch {}
    }
  };
};
var src_default = B12Plugin;
export {
  src_default as default,
  B12Plugin
};
