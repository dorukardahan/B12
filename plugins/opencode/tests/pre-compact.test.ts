import { afterEach, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"
import { preCompact } from "../src/hooks/pre-compact"

const tempDirs: string[] = []

afterEach(() => {
  delete process.env.B12_DATA_DIR
  for (const dir of tempDirs.splice(0)) {
    rmSync(dir, { recursive: true, force: true })
  }
})

function makeBase(): string {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-precompact-"))
  tempDirs.push(base)
  process.env.B12_DATA_DIR = base
  return base
}

test("preCompact includes modified files from working memory", async () => {
  const base = makeBase()
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
})

test("preCompact stores high-value assistant decisions and ignores short noise", async () => {
  const base = makeBase()
  const stored: Array<{ memory_type?: string; content: string }> = []
  const db = {
    store: (entry: { memory_type?: string; content: string }) => {
      stored.push(entry)
      return { id: stored.length }
    },
    storeEmbedding: () => {},
  }

  const summary = await preCompact(
    [
      { role: "assistant", content: "too short" },
      {
        role: "assistant",
        content:
          "We decided to preserve project-scoped modified files before OpenCode hook compaction because next-session recall depends on file context. " +
          "This decision keeps compact-time memory grounded in the actual worktree surface.",
      },
    ],
    "session-learning",
    "demo",
    base,
    db as any,
  )

  expect(summary).toContain("RECENT WORK:")
  expect(stored.some((entry) => entry.memory_type === "decision")).toBe(true)
})
