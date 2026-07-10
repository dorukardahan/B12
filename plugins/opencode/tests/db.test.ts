import { expect, test, describe, beforeEach, afterEach, mock } from "bun:test"
import { Database as BunSqlite } from "bun:sqlite"
import { mkdtempSync, rmSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"

// better-sqlite3 is a native Node addon that Bun's test runner cannot load
// (https://github.com/oven-sh/bun/issues/4290). The production plugin externalizes
// it at build time so the host Bun runtime resolves it; for unit tests we swap it
// for Bun's built-in bun:sqlite, whose Database API (prepare/all/get/run/exec/
// transaction/pragma/close) is compatible with the subset B12Database uses.
// This lets us exercise the real db.ts logic against a real SQLite engine.
mock.module("better-sqlite3", () => ({
  default: class Database {
    private inner: BunSqlite
    constructor(path: string, _opts?: Record<string, unknown>) {
      this.inner = new BunSqlite(path)
    }
    prepare(sql: string) {
      const statement = this.inner.prepare(sql)
      return {
        all: (...args: unknown[]) => statement.all(...args),
        get: (...args: unknown[]) => statement.get(...args) ?? undefined,
        run: (...args: unknown[]) => statement.run(...args),
      }
    }
    exec(sql: string) {
      return this.inner.exec(sql)
    }
    transaction(fn: (...args: never[]) => unknown) {
      return this.inner.transaction(fn as (...args: never[]) => void)
    }
    pragma(str: string) {
      // bun:sqlite has no .pragma() method; better-sqlite3 accepts both pragma
      // strings ("journal_mode = WAL") and "key=value" forms, returning rows.
      const normalized = str.includes("=") && !str.trim().startsWith("PRAGMA")
        ? `PRAGMA ${str}`
        : str.startsWith("PRAGMA")
          ? str
          : `PRAGMA ${str}`
      try {
        return this.inner.prepare(normalized).all()
      } catch {
        // some pragmas (e.g. journal_mode on :memory:) are write-style and
        // return no rows — better-sqlite3 still returns []; emulate that.
        this.inner.exec(normalized)
        return []
      }
    }
    close() {
      return this.inner.close()
    }
  },
}))

const { B12Database, computeContentHash, normalizeTags, getDbPath } =
  await import("../src/lib/db.js")

// Recreates the production schema for the tables/columns the plugin queries
// touch (parity with scripts/b12_mcp_server.py _ensure_schema). The plugin
// assumes tables already exist, so each test gets a fresh temp DB.
function makeSchema(raw: { exec: (s: string) => void }): void {
  raw.exec(`
    CREATE TABLE IF NOT EXISTS memories (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      content TEXT NOT NULL,
      content_hash TEXT UNIQUE,
      memory_type TEXT DEFAULT 'general',
      tags TEXT DEFAULT '',
      metadata TEXT DEFAULT '{}',
      created_at REAL,
      updated_at REAL,
      created_at_iso TEXT,
      updated_at_iso TEXT,
      deleted_at REAL DEFAULT NULL,
      strength REAL DEFAULT 1.0,
      last_accessed_at REAL DEFAULT NULL,
      valid_until TEXT DEFAULT NULL,
      difficulty REAL DEFAULT 5.0,
      due_date TEXT DEFAULT NULL
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS memory_content_fts USING fts5(
      content,
      content='memories',
      content_rowid='id',
      tokenize='trigram'
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_stemmed USING fts5(
      content,
      tags,
      content='memories',
      content_rowid='id',
      tokenize='porter unicode61'
    );

    CREATE TABLE IF NOT EXISTS memory_graph (
      source_hash TEXT NOT NULL,
      target_hash TEXT NOT NULL,
      similarity REAL NOT NULL,
      connection_types TEXT NOT NULL DEFAULT '[]',
      metadata TEXT,
      created_at REAL NOT NULL,
      relationship_type TEXT DEFAULT 'related',
      PRIMARY KEY (source_hash, target_hash)
    );

    CREATE TABLE IF NOT EXISTS memory_embeddings (
      rowid INTEGER PRIMARY KEY,
      content_embedding BLOB
    );
  `)

  // FTS5 external-content sync triggers (soft-delete aware). The trigram table
  // only indexes content.
  raw.exec(`
    CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories WHEN new.deleted_at IS NULL BEGIN
      INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
      INSERT INTO memory_content_fts(memory_content_fts, rowid, content) VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories
    WHEN new.deleted_at IS NULL AND old.deleted_at IS NULL BEGIN
      INSERT INTO memory_content_fts(memory_content_fts, rowid, content) VALUES ('delete', old.id, old.content);
      INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memories_fts_softdel AFTER UPDATE ON memories
    WHEN new.deleted_at IS NOT NULL AND old.deleted_at IS NULL BEGIN
      INSERT INTO memory_content_fts(memory_content_fts, rowid, content) VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memories_fts_ar AFTER UPDATE ON memories
    WHEN new.deleted_at IS NULL AND old.deleted_at IS NOT NULL BEGIN
      INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
    END;
  `)

  // The porter table indexes both content and tags and follows the same
  // active/update/soft-delete/restore transition contract as production.
  raw.exec(`
    CREATE TRIGGER IF NOT EXISTS memory_fts_stemmed_insert AFTER INSERT ON memories
    WHEN new.deleted_at IS NULL BEGIN
      INSERT INTO memory_fts_stemmed(rowid, content, tags)
      VALUES (new.id, new.content, COALESCE(new.tags, ''));
    END;
    CREATE TRIGGER IF NOT EXISTS memory_fts_stemmed_delete AFTER DELETE ON memories BEGIN
      INSERT INTO memory_fts_stemmed(memory_fts_stemmed, rowid, content, tags)
      VALUES ('delete', old.id, old.content, COALESCE(old.tags, ''));
    END;
    CREATE TRIGGER IF NOT EXISTS memory_fts_stemmed_update AFTER UPDATE ON memories
    WHEN new.deleted_at IS NULL AND old.deleted_at IS NULL BEGIN
      INSERT INTO memory_fts_stemmed(memory_fts_stemmed, rowid, content, tags)
      VALUES ('delete', old.id, old.content, COALESCE(old.tags, ''));
      INSERT INTO memory_fts_stemmed(rowid, content, tags)
      VALUES (new.id, new.content, COALESCE(new.tags, ''));
    END;
    CREATE TRIGGER IF NOT EXISTS memory_fts_stemmed_softdel AFTER UPDATE ON memories
    WHEN new.deleted_at IS NOT NULL AND old.deleted_at IS NULL BEGIN
      INSERT INTO memory_fts_stemmed(memory_fts_stemmed, rowid, content, tags)
      VALUES ('delete', old.id, old.content, COALESCE(old.tags, ''));
    END;
    CREATE TRIGGER IF NOT EXISTS memory_fts_stemmed_restore AFTER UPDATE ON memories
    WHEN new.deleted_at IS NULL AND old.deleted_at IS NOT NULL BEGIN
      INSERT INTO memory_fts_stemmed(rowid, content, tags)
      VALUES (new.id, new.content, COALESCE(new.tags, ''));
    END;
  `)
}

type DbCtx = {
  db: InstanceType<typeof B12Database>
  raw: { prepare: (s: string) => any; exec: (s: string) => void }
  dir: string
}

function makeDb(): DbCtx {
  const dir = mkdtempSync(join(tmpdir(), "b12-opencode-db-"))
  const path = join(dir, "test.db")
  const db = new B12Database(path)
  makeSchema(db.raw)
  return { db, raw: db.raw, dir }
}

function ftsRowIds(
  table: "memory_content_fts" | "memory_fts_stemmed",
  query: string,
): number[] {
  return (ctx.raw.prepare(`SELECT rowid FROM ${table} WHERE ${table} MATCH ?`).all(query) as Array<{ rowid: number }>)
    .map((row) => row.rowid)
}

let ctx: DbCtx

beforeEach(() => {
  ctx = makeDb()
})

afterEach(() => {
  ctx.db.close()
  rmSync(ctx.dir, { recursive: true, force: true })
})

// ── Pure helpers ────────────────────────────────────────────────────────────

describe("computeContentHash", () => {
  test("trims and lowercases before hashing", () => {
    expect(computeContentHash("  Hello  ")).toBe(computeContentHash("hello"))
    expect(computeContentHash("HELLO")).toBe(computeContentHash("hello"))
  })

  test("produces a stable hex sha256 of length 64", () => {
    const h = computeContentHash("test content")
    expect(h).toMatch(/^[0-9a-f]{64}$/)
    expect(h).toBe(computeContentHash("test content"))
  })

  test("different content yields different hashes", () => {
    expect(computeContentHash("alpha")).not.toBe(computeContentHash("beta"))
  })
})

describe("normalizeTags", () => {
  test("returns empty string for null/undefined", () => {
    expect(normalizeTags(null)).toBe("")
    expect(normalizeTags(undefined)).toBe("")
  })

  test("joins a filtered array with commas", () => {
    expect(normalizeTags(["a", "b", ""])).toBe("a,b")
    expect(normalizeTags(["only"])).toBe("only")
  })

  test("passes a string through unchanged", () => {
    expect(normalizeTags("proj:demo,type:fact")).toBe("proj:demo,type:fact")
  })
})

describe("getDbPath", () => {
  test("returns a path ending in the sqlite_vec.db filename", () => {
    expect(getDbPath()).toMatch(/sqlite_vec\.db$/)
  })
})

// ── B12Database.store ───────────────────────────────────────────────────────

describe("B12Database.store", () => {
  test("inserts a new memory and returns its id + content hash", () => {
    const res = ctx.db.store({ content: "first memory", tags: "proj:test" })

    expect(res.id).toBeGreaterThanOrEqual(1)
    expect(res.hash).toBe(computeContentHash("first memory"))
  })

  test("idempotent: storing identical content does not duplicate", () => {
    const a = ctx.db.store({ content: "dup content" })
    const b = ctx.db.store({ content: "dup content" })

    expect(b.id).toBe(a.id)
    expect(b.hash).toBe(a.hash)
    expect(ctx.db.getStats().active).toBe(1)
  })

  test("merges tags on re-store of identical content", () => {
    const a = ctx.db.store({ content: "merge me", tags: "tag-a" })
    ctx.db.store({ content: "merge me", tags: "tag-b" })

    const row = ctx.db.getById(a.id)
    expect(row?.tags).toContain("tag-a")
    expect(row?.tags).toContain("tag-b")
  })

  test("scrubs secrets before hashing and caps importance at baseline", () => {
    const secret = "ghp_" + "x".repeat(40)
    const res = ctx.db.store({ content: `token: ${secret}` })

    const row = ctx.db.getById(res.id)
    expect(row).toBeDefined()
    // raw secret must never persist
    expect(row!.content).not.toContain(secret)
    expect(row!.content).toContain("[REDACTED:")
    // importance capped at baseline 0.5
    expect(JSON.parse(row!.metadata).importance_score).toBe(0.5)
  })

  test("revives a soft-deleted memory on re-store", () => {
    const a = ctx.db.store({ content: "phoenix memory" })
    ctx.db.update(a.hash, { deleted_at: 1 })
    expect(ctx.db.getById(a.id)).toBeUndefined()

    const b = ctx.db.store({ content: "phoenix memory" })
    expect(b.id).toBe(a.id)
    expect(ctx.db.getById(a.id)).toBeDefined()
  })

  test("defaults memory_type to general", () => {
    const a = ctx.db.store({ content: "no type" })
    expect(ctx.db.getById(a.id)?.memory_type).toBe("general")
  })

  test("honors an explicit memory_type", () => {
    const a = ctx.db.store({ content: "decision X", memory_type: "decision" })
    expect(ctx.db.getById(a.id)?.memory_type).toBe("decision")
  })
})

// ── FTS trigger parity ───────────────────────────────────────────────────────

describe("FTS sync transitions", () => {
  test("keeps trigram and stemmed indexes in sync across update, delete, and restore", () => {
    const stored = ctx.db.store({
      content: "running memory marker",
      tags: "topic:oldtag",
    })

    expect(ftsRowIds("memory_content_fts", "running")).toEqual([stored.id])
    expect(ftsRowIds("memory_fts_stemmed", "run")).toEqual([stored.id])
    expect(ftsRowIds("memory_fts_stemmed", "oldtag")).toEqual([stored.id])
    expect(ctx.db.search({ query: "running", mode: "hybrid" }).map((row) => row.id)).toContain(stored.id)

    expect(ctx.db.update(stored.hash, { tags: "topic:newtag" })).toBe(true)
    expect(ftsRowIds("memory_content_fts", "running")).toEqual([stored.id])
    expect(ftsRowIds("memory_fts_stemmed", "oldtag")).toEqual([])
    expect(ftsRowIds("memory_fts_stemmed", "newtag")).toEqual([stored.id])
    expect(ctx.db.search({ query: "running", mode: "hybrid" }).map((row) => row.id)).toContain(stored.id)
    expect(ctx.db.search({ query: "running", mode: "hybrid", stemmed: true }).map((row) => row.id)).toContain(stored.id)

    expect(ctx.db.update(stored.hash, { deleted_at: 1 })).toBe(true)
    expect(ftsRowIds("memory_content_fts", "running")).toEqual([])
    expect(ftsRowIds("memory_fts_stemmed", "run")).toEqual([])
    expect(ctx.db.search({ query: "running", mode: "hybrid" })).toEqual([])
    expect(ctx.db.search({ query: "running", mode: "hybrid", stemmed: true })).toEqual([])

    const restored = ctx.db.store({
      content: "running memory marker",
      tags: "topic:restoredtag",
    })
    expect(restored.id).toBe(stored.id)
    expect(ftsRowIds("memory_content_fts", "running")).toEqual([stored.id])
    expect(ftsRowIds("memory_fts_stemmed", "run")).toEqual([stored.id])
    expect(ftsRowIds("memory_fts_stemmed", "newtag")).toEqual([])
    expect(ftsRowIds("memory_fts_stemmed", "restoredtag")).toEqual([stored.id])
    expect(ctx.db.search({ query: "running", mode: "hybrid" }).map((row) => row.id)).toContain(stored.id)
    expect(ctx.db.search({ query: "running", mode: "hybrid", stemmed: true }).map((row) => row.id)).toContain(stored.id)
  })
})

// ── B12Database.update ──────────────────────────────────────────────────────

describe("B12Database.update", () => {
  test("updates tags and memory_type", () => {
    const a = ctx.db.store({ content: "to update" })
    const ok = ctx.db.update(a.hash, { tags: "new-tag", memory_type: "lesson" })

    expect(ok).toBe(true)
    const row = ctx.db.getById(a.id)
    expect(row?.tags).toBe("new-tag")
    expect(row?.memory_type).toBe("lesson")
  })

  test("merges metadata shallowly", () => {
    const a = ctx.db.store({
      content: "meta base",
      metadata: { keep: 1, overwrite: "old" },
    })
    ctx.db.update(a.hash, { metadata: { overwrite: "new", added: 2 } })

    const meta = JSON.parse(ctx.db.getById(a.id)!.metadata)
    expect(meta.keep).toBe(1)
    expect(meta.overwrite).toBe("new")
    expect(meta.added).toBe(2)
  })

  test("clamps strength into [0.3, 5.0]", () => {
    const a = ctx.db.store({ content: "strength test" })

    ctx.db.update(a.hash, { strength: 100 })
    expect(ctx.db.getById(a.id)?.strength).toBe(5.0)

    ctx.db.update(a.hash, { strength: 0 })
    expect(ctx.db.getById(a.id)?.strength).toBe(0.3)
  })

  test("refuses to update protected fields", () => {
    const a = ctx.db.store({ content: "protected" })
    // @ts-expect-error — deliberately passing a protected field
    expect(ctx.db.update(a.hash, { content: "hacked" })).toBe(false)
  })

  test("returns false for an unknown hash", () => {
    expect(ctx.db.update("nope", { tags: "x" })).toBe(false)
  })

  test("soft-delete hides the row from active getters", () => {
    const a = ctx.db.store({ content: "delete me" })
    ctx.db.update(a.hash, { deleted_at: 999 })
    expect(ctx.db.getById(a.id)).toBeUndefined()
  })
})

// ── B12Database.search ──────────────────────────────────────────────────────

describe("B12Database.search", () => {
  test("returns matching rows for an exact substring query", () => {
    ctx.db.store({ content: "the quick brown fox" })
    ctx.db.store({ content: "a totally unrelated note" })

    const results = ctx.db.search({ query: "brown fox", mode: "exact" })
    expect(results.some((r) => r.display.includes("brown fox"))).toBe(true)
    expect(results.every((r) => !r.display.includes("unrelated"))).toBe(true)
  })

  test("returns all active rows when no query is given", () => {
    ctx.db.store({ content: "alpha note" })
    ctx.db.store({ content: "beta note" })

    const results = ctx.db.search({})
    expect(results).toHaveLength(2)
  })

  test("returns no rows for a query that matches nothing", () => {
    ctx.db.store({ content: "nothing here matches" })
    expect(ctx.db.search({ query: "zzzznomatch" })).toHaveLength(0)
  })

  test("filters by tag", () => {
    ctx.db.store({ content: "tagged one", tags: "proj:demo" })
    ctx.db.store({ content: "tagged two", tags: "proj:other" })

    const results = ctx.db.search({ tags: "proj:demo" })
    expect(results).toHaveLength(1)
    expect(results[0].display).toContain("tagged one")
  })

  test("requires every requested tag and respects token boundaries", () => {
    ctx.db.store({ content: "both tags", tags: "proj:demo,type:fact" })
    ctx.db.store({ content: "one tag", tags: "proj:demo" })
    ctx.db.store({ content: "near miss", tags: "proj:democracy,type:fact-extra" })

    const results = ctx.db.search({ tags: ["proj:demo", "type:fact"] })
    expect(results).toHaveLength(1)
    expect(results[0].display).toContain("both tags")
  })
})

// ── B12Database.getByTags / filterSearchResultsByTags ───────────────────────

describe("B12Database.getByTags", () => {
  test("returns rows matching all supplied tags without token-boundary near misses", () => {
    ctx.db.store({ content: "row one", tags: "type:fact,type:lesson" })
    ctx.db.store({ content: "row two", tags: "type:lesson" })
    ctx.db.store({ content: "row three", tags: "type:fact-extra,type:lesson" })

    const rows = ctx.db.getByTags(["type:fact", "type:lesson"])
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe("row one")
  })

  test("returns empty for an empty tag list", () => {
    ctx.db.store({ content: "row one", tags: "x" })
    expect(ctx.db.getByTags([])).toEqual([])
  })
})

describe("B12Database.filterSearchResultsByTags", () => {
  test("narrows a result set to rows carrying the tag", () => {
    const a = ctx.db.store({ content: "keep", tags: "proj:demo" })
    const b = ctx.db.store({ content: "drop", tags: "proj:other" })

    const filtered = ctx.db.filterSearchResultsByTags(
      [
        { id: a.id, display: "keep", score: 0.9 },
        { id: b.id, display: "drop", score: 0.8 },
      ],
      "proj:demo",
    )
    expect(filtered).toHaveLength(1)
    expect(filtered[0].id).toBe(a.id)
  })

  test("requires all tags and rejects token-boundary near misses", () => {
    const both = ctx.db.store({ content: "both", tags: "proj:demo,type:fact" })
    const one = ctx.db.store({ content: "one", tags: "proj:demo" })
    const near = ctx.db.store({ content: "near", tags: "proj:democracy,type:fact-extra" })

    const filtered = ctx.db.filterSearchResultsByTags(
      [
        { id: both.id, display: "both", score: 0.9 },
        { id: one.id, display: "one", score: 0.8 },
        { id: near.id, display: "near", score: 0.7 },
      ],
      ["proj:demo", "type:fact"],
    )
    expect(filtered).toEqual([{ id: both.id, display: "both", score: 0.9 }])
  })

  test("passes through all results when no tags given", () => {
    const a = ctx.db.store({ content: "x" })
    const out = ctx.db.filterSearchResultsByTags(
      [{ id: a.id, display: "x", score: 0.5 }],
      [],
    )
    expect(out).toHaveLength(1)
  })
})

// ── B12Database.getByHash / getById ─────────────────────────────────────────

describe("B12Database.getByHash / getById", () => {
  test("getByHash returns the active row", () => {
    const a = ctx.db.store({ content: "hash lookup" })
    expect(ctx.db.getByHash(a.hash)?.content).toBe("hash lookup")
  })

  test("getByHash returns undefined for soft-deleted", () => {
    const a = ctx.db.store({ content: "gone" })
    ctx.db.update(a.hash, { deleted_at: 1 })
    expect(ctx.db.getByHash(a.hash)).toBeUndefined()
  })

  test("getById returns undefined for unknown id", () => {
    expect(ctx.db.getById(999999)).toBeUndefined()
  })
})

// ── B12Database.rateQuality ─────────────────────────────────────────────────

describe("B12Database.rateQuality", () => {
  test("blends a user rating with the existing score", () => {
    const a = ctx.db.store({ content: "quality target" })

    const score = ctx.db.rateQuality(a.hash, "1")
    expect(score).not.toBeNull()
    expect(score).toBeGreaterThan(0.5) // positive rating raises from 0.5 baseline

    const meta = JSON.parse(ctx.db.getById(a.id)!.metadata)
    expect(meta.quality_provider).toBe("user")
    expect(meta.quality_score).toBe(score)
  })

  test("a negative rating lowers the blended score", () => {
    const a = ctx.db.store({ content: "downvote" })
    const score = ctx.db.rateQuality(a.hash, "-1")
    expect(score).toBeLessThan(0.5)
  })

  test("records optional feedback", () => {
    const a = ctx.db.store({ content: "with feedback" })
    ctx.db.rateQuality(a.hash, "1", "very useful")
    expect(JSON.parse(ctx.db.getById(a.id)!.metadata).quality_feedback).toBe(
      "very useful",
    )
  })

  test("returns null for an unknown hash", () => {
    expect(ctx.db.rateQuality("missing", "1")).toBeNull()
  })
})

// ── B12Database.boostStrength ───────────────────────────────────────────────

describe("B12Database.boostStrength", () => {
  test("increments strength and access_count without exceeding 5.0", () => {
    const a = ctx.db.store({ content: "boost me" })

    ctx.db.boostStrength([a.id])
    let row = ctx.raw
      .prepare("SELECT strength, metadata FROM memories WHERE id = ?")
      .get(a.id) as { strength: number; metadata: string }
    expect(row.strength).toBeCloseTo(1.2, 5)
    expect(JSON.parse(row.metadata).access_count).toBe(1)

    // boost many times; must cap at 5.0
    for (let i = 0; i < 30; i++) ctx.db.boostStrength([a.id])
    const cappedRow = ctx.raw
      .prepare("SELECT strength FROM memories WHERE id = ?")
      .get(a.id) as { strength: number }
    expect(cappedRow.strength).toBe(5.0)
  })

  test("is a no-op for an empty id list", () => {
    ctx.db.store({ content: "untouched" })
    expect(() => ctx.db.boostStrength([])).not.toThrow()
  })
})

// ── B12Database.getStats ────────────────────────────────────────────────────

describe("B12Database.getStats", () => {
  test("counts active vs deleted and groups by type", () => {
    ctx.db.store({ content: "one", memory_type: "fact" })
    ctx.db.store({ content: "two", memory_type: "fact" })
    ctx.db.store({ content: "three", memory_type: "lesson" })
    const d = ctx.db.store({ content: "deleted one" })
    ctx.db.update(d.hash, { deleted_at: 1 })

    const stats = ctx.db.getStats()
    expect(stats.active).toBe(3)
    expect(stats.deleted).toBe(1)
    expect(stats.edges).toBe(0)
    const factType = stats.types.find((t) => t.type === "fact")
    expect(factType?.count).toBe(2)
  })
})

// ── B12Database.storeEmbedding ──────────────────────────────────────────────

describe("B12Database.storeEmbedding", () => {
  test("stores and updates an embedding for a memory rowid", () => {
    const a = ctx.db.store({ content: "embedded" })

    ctx.db.storeEmbedding(a.id, Buffer.from([1, 2, 3]))
    let row = ctx.raw
      .prepare("SELECT content_embedding FROM memory_embeddings WHERE rowid = ?")
      .get(a.id) as { content_embedding: Uint8Array }
    expect(row.content_embedding.length).toBe(3)

    // update path
    ctx.db.storeEmbedding(a.id, Buffer.from([9, 9, 9, 9]))
    row = ctx.raw
      .prepare("SELECT content_embedding FROM memory_embeddings WHERE rowid = ?")
      .get(a.id) as { content_embedding: Uint8Array }
    expect(row.content_embedding.length).toBe(4)
    expect(row.content_embedding[0]).toBe(9)
  })

  test("accepts a base64 string embedding", () => {
    const a = ctx.db.store({ content: "b64 embed" })
    const b64 = Buffer.from([4, 5, 6]).toString("base64")
    ctx.db.storeEmbedding(a.id, b64)

    const row = ctx.raw
      .prepare("SELECT content_embedding FROM memory_embeddings WHERE rowid = ?")
      .get(a.id) as { content_embedding: Uint8Array }
    expect(row.content_embedding.length).toBe(3)
  })
})

// ── B12Database.getGraphNeighbors ───────────────────────────────────────────

describe("B12Database.getGraphNeighbors", () => {
  test("returns edges for a hash in either direction", () => {
    const src = ctx.db.store({ content: "source node" })
    const tgt = ctx.db.store({ content: "target node" })

    ctx.raw
      .prepare(
        `INSERT INTO memory_graph (source_hash, target_hash, similarity, created_at, relationship_type)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(src.hash, tgt.hash, 0.8, 1000, "related")

    const edges = ctx.db.getGraphNeighbors(src.hash)
    expect(edges).toHaveLength(1)
    expect(edges[0].target_hash).toBe(tgt.hash)

    // reverse lookup also finds it
    expect(ctx.db.getGraphNeighbors(tgt.hash)).toHaveLength(1)
  })

  test("returns empty for a hash with no edges", () => {
    expect(ctx.db.getGraphNeighbors("orphan-hash")).toEqual([])
  })
})

// ── B12Database.getSessionContext ───────────────────────────────────────────

describe("B12Database.getSessionContext", () => {
  test("returns project + universal memories and nulls when absent", () => {
    ctx.db.store({ content: "proj fact", tags: "proj:demo", memory_type: "fact" })
    ctx.db.store({ content: "universal fact", memory_type: "fact" })

    const c = ctx.db.getSessionContext("demo")
    expect(c.projectMemories.length).toBeGreaterThan(0)
    expect(c.projectMemories[0].content).toContain("proj fact")
    expect(c.universalMemories.length).toBeGreaterThan(0)
    expect(c.lastSessionSummary).toBeNull()
  })
})

// ── B12Database.storeWorkingMemory ──────────────────────────────────────────

describe("B12Database.storeWorkingMemory", () => {
  test("stores tagged working memory and scrubs secrets", () => {
    const secret = "sk-ant-" + "y".repeat(50)
    ctx.db.storeWorkingMemory(`recent context with ${secret}`, "demo")

    const rows = ctx.db.getByTags("source:opencode")
    expect(rows.length).toBe(1)
    expect(rows[0].content).not.toContain(secret)
    expect(rows[0].tags).toContain("proj:demo")
    expect(rows[0].tags).toContain("type:working_memory")
  })
})

// ── B12Database.getContentGuardrails ────────────────────────────────────────

describe("B12Database.getContentGuardrails", () => {
  test("returns only guardrail-typed active rows", () => {
    ctx.db.store({ content: "do not commit secrets", memory_type: "guardrail" })
    ctx.db.store({ content: "a regular fact", memory_type: "fact" })

    const guardrails = ctx.db.getContentGuardrails()
    expect(guardrails).toEqual(["do not commit secrets"])
  })
})
