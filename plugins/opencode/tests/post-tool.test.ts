import { afterEach, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"
import { postTool } from "../src/hooks/post-tool"
import { createSessionState, createWorkingMemory } from "../src/lib/state"

const tempDirs: string[] = []

afterEach(() => {
  delete process.env.B12_DATA_DIR
  for (const dir of tempDirs.splice(0)) {
    rmSync(dir, { recursive: true, force: true })
  }
})

function makeBase(): string {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-post-tool-"))
  tempDirs.push(base)
  process.env.B12_DATA_DIR = base
  return base
}

function deps(base: string, dbOverride?: any) {
  return {
    db: dbOverride ?? { search: () => [] },
    project: "demo",
    cwd: base,
    sessionId: "sess-post-tool",
    sessionState: createSessionState("demo", base, "opencode"),
    workingMemory: createWorkingMemory("sess-post-tool"),
    stagingDir: join(base, "memory-staging", "demo", "sess-post-tool"),
  }
}

test("postTool short-circuits B12 memory MCP tools", async () => {
  const base = makeBase()
  let searches = 0
  const d = deps(base, {
    search: () => {
      searches++
      return []
    },
  })

  for (const tool of [
    "B12_memory_store",
    "mcp__B12__memory_store",
    "B12_memory_search",
    "mcp__B12__memory_search",
    "B12_memory_update",
    "mcp__B12__memory_update",
  ]) {
    const result = await postTool(
      { tool, args: { content: "x" } },
      { args: {}, result: "ok" },
      d,
    )
    expect(result.workingMemory.active_files).toEqual([])
    expect(result.workingMemory.modified_files).toEqual([])
  }
  expect(searches).toBe(0)
})

test("postTool records glob/grep patterns as bounded search patterns", async () => {
  const base = makeBase()
  const longPattern = "a".repeat(200)
  const result = await postTool(
    { tool: "glob", args: { pattern: longPattern } },
    { args: {} },
    deps(base),
  )

  expect(result.workingMemory.search_patterns).toHaveLength(1)
  expect(result.workingMemory.search_patterns[0]).toHaveLength(80)
})

test("postTool surfaces related memories on read of a known file", async () => {
  const base = makeBase()
  const d = deps(base, {
    search: () => [{ id: 1, display: "[decision] use sqlite for local index", score: 0.9 }],
  })

  const result = await postTool(
    { tool: "read", args: { filePath: join(base, "src/router.ts") } },
    { args: {} },
    d,
  )

  expect(result.workingMemory.active_files).toEqual(["src/router.ts"])
  expect(result.surfaced).toContain("use sqlite")
})
