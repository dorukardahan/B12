import { afterEach, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"
import { buildAtomicTempPath, extractModifiedFileTokens, sessionEnd } from "../src/hooks/session-end"

const tempDirs: string[] = []

afterEach(() => {
  delete process.env.B12_DATA_DIR
  for (const dir of tempDirs.splice(0)) {
    rmSync(dir, { recursive: true, force: true })
  }
})

function makeBase(): string {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-session-end-"))
  tempDirs.push(base)
  process.env.B12_DATA_DIR = base
  return base
}

test("sessionEnd extracts errors, learnings, decisions, and preferences", async () => {
  const base = makeBase()
  const stored: string[] = []
  const db = {
    store: (entry: { memory_type?: string; content: string }) => {
      stored.push(entry.memory_type || "")
      return { id: stored.length }
    },
    storeEmbedding: () => {},
  }

  await sessionEnd(
    [
      { role: "user", content: "fix the crash and document it" },
      {
        role: "assistant",
        content:
          "The error was caused by a null pointer in the migration runner. " +
          "We discovered that the runner silently crashes on undefined values. " +
          "We decided to add a validation layer. This is a preference: always use TypeScript strict mode.",
      },
    ],
    "sess-multi",
    "demo",
    base,
    db as any,
  )

  expect(stored).toContain("error")
  expect(stored).toContain("learning")
  expect(stored).toContain("decision")
  expect(stored).toContain("preference")
})

test("sessionEnd is a no-op when all messages are empty", async () => {
  const base = makeBase()
  let stores = 0
  const db = {
    store: () => {
      stores++
      return { id: 1 }
    },
    storeEmbedding: () => {},
  }

  await sessionEnd(
    [{ role: "user", content: "   " }, { role: "assistant", content: "" }],
    "sess-empty",
    "demo",
    base,
    db as any,
  )

  expect(stores).toBe(0)
})

test("sessionEnd file-token helpers ignore versions and keep temp paths unique", () => {
  const files = extractModifiedFileTokens(
    "updated src/index.ts and package.json while mentioning 10.27.0 and example.com",
  )

  expect(files).toEqual(["src/index.ts", "package.json"])
  expect(buildAtomicTempPath("/tmp/project-latest.md")).not.toBe(
    buildAtomicTempPath("/tmp/project-latest.md"),
  )
})
