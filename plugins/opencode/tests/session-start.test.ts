import { afterEach, expect, test } from "bun:test"
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"
import { sessionStart } from "../src/hooks/session-start"

const tempDirs: string[] = []

afterEach(() => {
  delete process.env.B12_DATA_DIR
  delete process.env.B12_HOOK_DIR
  for (const dir of tempDirs.splice(0)) {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("sessionStart includes a clipped long latest summary within budget", async () => {
  const base = mkdtempSync(join(tmpdir(), "b12-opencode-"))
  tempDirs.push(base)
  process.env.B12_DATA_DIR = base
  process.env.B12_HOOK_DIR = join(base, "hooks")
  const summaries = join(base, "memory-summaries")
  mkdirSync(summaries, { recursive: true })
  writeFileSync(join(summaries, "demo-latest.md"), "x".repeat(8000))

  const db = {
    getSessionContext: () => ({ projectMemories: [], universalMemories: [] }),
    getContentGuardrails: () => [],
  }

  const context = await sessionStart("demo", base, db as any)

  expect(context).toContain("## Last Session Summary")
  expect(context.length).toBeLessThanOrEqual(6000)
  expect(context).toContain("x".repeat(1200))
})
