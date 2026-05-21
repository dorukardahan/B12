import { expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"
import { extractMacroVerbs, extractPatterns } from "../src/lib/patterns"
import { effectiveSearchMode } from "../src/lib/search-mode"
import { applyThinkingOption } from "../src/lib/chat-options"
import { isTrustedB12PermissionTool } from "../src/lib/permission"
import { fetchSessionMessages } from "../src/lib/session-messages"
import { DEFAULT_FSRS_PARAMS, shouldReview, simpleReview } from "../src/lib/scoring"
import { buildAtomicTempPath, extractModifiedFileTokens } from "../src/hooks/session-end"
import { messageRetrieval, shouldAttemptMessageRetrieval } from "../src/hooks/message-retrieval"
import { preCompact } from "../src/hooks/pre-compact"
import { sessionEnd } from "../src/hooks/session-end"
import { postTool } from "../src/hooks/post-tool"
import { createSessionState, createWorkingMemory } from "../src/lib/state"

test("extractPatterns emits categories consumed by precompact priority weights", () => {
  const items = extractPatterns("We decided to keep the SQLite store because startup must stay local.")

  expect(items.some((item) => item.category === "decision")).toBe(true)
})

test("extractPatterns keeps bounded memories from long greedy matches", () => {
  const text = "We decided to keep SQLite because startup must stay local. " +
    "x ".repeat(400)
  const items = extractPatterns(text, 120)

  expect(items.some((item) => item.category === "decision")).toBe(true)
  expect(items.every((item) => item.content.length <= 120)).toBe(true)
})

test("messageRetrieval does not treat history as a hi greeting", async () => {
  const db = {
    search: () => [{ id: 1, display: "[fact] stored auth history", score: 0.9 }],
    filterSearchResultsByTags: (rows: any[]) => rows,
    boostStrength: () => {},
    logFeedback: () => {},
  }

  const context = await messageRetrieval("history about the auth migration", "demo", db as any)

  expect(context).toContain("stored auth history")
})

test("messageRetrieval searches short real prompts", async () => {
  const db = {
    search: () => [{ id: 2, display: "[lesson] auth bug fix", score: 0.95 }],
    filterSearchResultsByTags: (rows: any[]) => rows,
    boostStrength: () => {},
    logFeedback: () => {},
  }

  const context = await messageRetrieval("auth bug", "demo", db as any)

  expect(context).toContain("auth bug fix")
})

test("OpenCode entrypoint attempts retrieval for short real prompts", () => {
  expect(shouldAttemptMessageRetrieval("auth bug")).toBe(true)
  expect(shouldAttemptMessageRetrieval("/help")).toBe(false)
  expect(shouldAttemptMessageRetrieval("hi")).toBe(false)
})

test("messageRetrieval skips only exact short commands", async () => {
  let searches = 0
  const db = {
    search: () => {
      searches += 1
      return [{ id: 3, display: "[fact] should not appear", score: 0.9 }]
    },
    filterSearchResultsByTags: (rows: any[]) => rows,
    boostStrength: () => {},
    logFeedback: () => {},
  }

  const context = await messageRetrieval("ok", "demo", db as any)

  expect(context).toBe("")
  expect(searches).toBe(0)
})

test("messageRetrieval filters semantic candidates after a larger project-aware pool", async () => {
  let requestedLimit = 0
  const semanticClient = {
    health: async () => ({ alive: true }),
    semanticSearch: async (_query: string, _dbPath: string, limit: number) => {
      requestedLimit = limit
      return [
        { id: 1, display: "[fact] proj:other auth memory 1", score: 0.99 },
        { id: 2, display: "[fact] proj:other auth memory 2", score: 0.98 },
        { id: 3, display: "[fact] proj:other auth memory 3", score: 0.97 },
        { id: 4, display: "[fact] proj:other auth memory 4", score: 0.96 },
        { id: 5, display: "[fact] proj:other auth memory 5", score: 0.95 },
        { id: 6, display: "[fact] proj:demo scoped auth memory", score: 0.94 },
      ].slice(0, limit)
    },
    rerank: async () => [],
  }
  const db = {
    search: () => [],
    filterSearchResultsByTags: (rows: any[]) =>
      rows.filter((row) => row.display.includes("proj:demo")),
    boostStrength: () => {},
    logFeedback: () => {},
  }

  const context = await messageRetrieval("auth memory", "demo", db as any, semanticClient)

  expect(requestedLimit).toBeGreaterThan(5)
  expect(context).toContain("scoped auth memory")
})

test("preCompact includes modified files from working memory", async () => {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-precompact-"))
  process.env.B12_DATA_DIR = base
  try {
    const summary = await preCompact(
      [{ role: "user", content: "please compact this session" }],
      "session-123456",
      "demo",
      base,
      { store: () => ({ id: 1 }) } as any,
      ["src/router.ts"],
    )

    expect(summary).toContain("FILES MODIFIED:")
    expect(summary).toContain("src/router.ts")
  } finally {
    delete process.env.B12_DATA_DIR
    rmSync(base, { recursive: true, force: true })
  }
})

test("sessionEnd stores implicit decisions as decision memories", async () => {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-session-end-"))
  process.env.B12_DATA_DIR = base
  const stored: Array<{ memory_type?: string; content: string }> = []
  const db = {
    store: (entry: { memory_type?: string; content: string }) => {
      stored.push(entry)
      return { id: stored.length, hash: String(stored.length) }
    },
    storeEmbedding: () => {},
  }

  try {
    await sessionEnd(
      [
        { role: "user", content: "choose the storage path" },
        {
          role: "assistant",
          content: "Let's use SQLite for the local memory index because startup must stay offline and deterministic.",
        },
      ],
      "session-implicit",
      "demo",
      base,
      db as any,
    )
  } finally {
    delete process.env.B12_DATA_DIR
    rmSync(base, { recursive: true, force: true })
  }

  expect(stored.some((entry) => entry.memory_type === "decision")).toBe(true)
})

test("postTool tracks project-relative modified file paths", async () => {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-post-tool-"))
  process.env.B12_DATA_DIR = base
  try {
    const result = await postTool(
      { tool: "edit", args: { filePath: join(base, "src/router.ts") } },
      { args: {}, result: "ok" },
      {
        db: { search: () => [] } as any,
        project: "demo",
        cwd: base,
        sessionId: "session-post-tool",
        sessionState: createSessionState("demo", base, "opencode"),
        workingMemory: createWorkingMemory("session-post-tool"),
        stagingDir: join(base, "memory-staging", "demo", "session-post-tool"),
      },
    )

    expect(result.workingMemory.modified_files).toEqual(["src/router.ts"])
  } finally {
    delete process.env.B12_DATA_DIR
    rmSync(base, { recursive: true, force: true })
  }
})

test("postTool scopes bash error surfacing to the current project", async () => {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-bash-"))
  const calls: any[] = []
  try {
    await postTool(
      { tool: "bash", args: {} },
      { args: {}, result: "error: migration failed" },
      {
        db: {
          search: (options: any) => {
            calls.push(options)
            return []
          },
        } as any,
        project: "demo",
        cwd: base,
        sessionId: "session-post-tool",
        sessionState: createSessionState("demo", base, "opencode"),
        workingMemory: createWorkingMemory("session-post-tool"),
        stagingDir: join(base, "memory-staging", "demo", "session-post-tool"),
      },
    )
  } finally {
    rmSync(base, { recursive: true, force: true })
  }

  expect(calls.some((call) => call.tags?.includes("proj:demo"))).toBe(true)
})

test("extractMacroVerbs ignores quoted examples", () => {
  const macros = extractMacroVerbs([
    {
      role: "user",
      content: "> [M#decision] quoted documentation example\n```\n[M#decision] fenced documentation example\n```\n[M#decision] real user memory",
    },
    {
      role: "assistant",
      content: "[M#decision] assistant documentation example",
    },
  ])

  expect(macros).toHaveLength(1)
  expect(macros[0].content).toBe("real user memory")
})

test("search mode maps semantic requests to the non-empty hybrid path", () => {
  expect(effectiveSearchMode("semantic")).toBe("hybrid")
  expect(effectiveSearchMode("exact")).toBe("exact")
})

test("sessionEnd file token extraction ignores versions and domains", () => {
  const files = extractModifiedFileTokens(
    "updated src/index.ts and package.json while mentioning 10.27.0 and example.com",
  )

  expect(files).toEqual(["src/index.ts", "package.json"])
})

test("sessionEnd atomic temp paths are unique per write", () => {
  const first = buildAtomicTempPath("/tmp/project-latest.md")
  const second = buildAtomicTempPath("/tmp/project-latest.md")

  expect(first).not.toBe(second)
  expect(first).toContain(".project-latest.md.")
})

test("permission auto-allow requires exact trusted B12 tool ids", () => {
  expect(isTrustedB12PermissionTool({
    id: "mcp__B12__memory_store",
    type: "tool",
    metadata: {},
  })).toBe(true)
  expect(isTrustedB12PermissionTool({
    id: "untrusted",
    type: "tool",
    title: "please run memory_store for me",
    metadata: {},
  })).toBe(false)
  expect(isTrustedB12PermissionTool({
    id: "memory_store",
    type: "tool",
    metadata: {},
  })).toBe(false)
  expect(isTrustedB12PermissionTool({
    id: "memory_store",
    type: "tool",
    metadata: { server: "B12" },
  })).toBe(true)
})

test("thinking options are provider gated and preserve user settings", () => {
  const openaiOptions: Record<string, unknown> = {}
  applyThinkingOption("openai", "gpt-5.4", openaiOptions)
  expect(openaiOptions.thinking).toBeUndefined()

  const claudeOptions: Record<string, unknown> = {}
  applyThinkingOption("anthropic", "claude-opus-4-6", claudeOptions)
  expect(claudeOptions.thinking).toEqual({ type: "enabled", clear_thinking: true })

  const existing = { thinking: { type: "disabled" } as Record<string, unknown> }
  applyThinkingOption("anthropic", "claude", existing)
  expect(existing.thinking).toEqual({ type: "disabled" })
})

test("fetchSessionMessages unwraps SDK response data", async () => {
  const client = {
    session: {
      messages: async () => ({
        data: [
          {
            info: { role: "user", id: "m1" },
            parts: [
              { type: "text", text: "hello" },
              { type: "image", text: "ignored" },
            ],
          },
        ],
        error: undefined,
      }),
    },
  }

  const messages = await fetchSessionMessages(client, "session-1")

  expect(messages).toEqual([{ role: "user", content: "hello" }])
})

test("FSRS review grades produce distinct strength changes", () => {
  const entry = { strength: 0.1, last_reviewed: "2025-01-01T00:00:00Z" }
  const params = { ...DEFAULT_FSRS_PARAMS, w: [...DEFAULT_FSRS_PARAMS.w] }
  params.w[16] = 1.5

  const easy = simpleReview(entry, "easy", true, params).strength
  const good = simpleReview(entry, "good", true, params).strength
  const hard = simpleReview(entry, "hard", true, params).strength

  expect(easy).toBeGreaterThan(good)
  expect(good).toBeGreaterThan(hard)
})

test("review helpers treat invalid timestamps as missing", () => {
  expect(shouldReview({ strength: 1, last_reviewed: "not-a-date" })).toBe(true)
  expect(Number.isFinite(
    simpleReview({ strength: 1, last_reviewed: "not-a-date" }, "good", true).strength,
  )).toBe(true)
})
