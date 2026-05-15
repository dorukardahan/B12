import { join } from "path"
import { existsSync, mkdirSync, writeFileSync, readdirSync, statSync, unlinkSync, renameSync } from "fs"
import { homedir } from "os"
import { B12Database } from "../lib/db.js"
import * as daemon from "../lib/daemon.js"
import { extractPatterns, summaryFilter } from "../lib/patterns.js"

const B12_BASE = process.env.B12_DATA_DIR || join(homedir(), ".B12")
const CHAR_BUDGET = 8000

const PRIORITY_WEIGHTS: Record<string, number> = {
  decision: 10,
  error_fix: 9,
  learning: 8,
  preference: 8,
  file_modified: 7,
  user_request: 6,
  progress: 5,
  general_work: 2,
}

interface ScoredItem {
  priority: number
  category: string
  text: string
}

export async function preCompact(
  messages: Array<{ role: string; content: string }>,
  sessionId: string,
  project: string,
  cwd: string,
  db: B12Database
): Promise<string> {
  const stagingDir = join(B12_BASE, "memory-staging")
  mkdirSync(stagingDir, { recursive: true })

  const scoredItems: ScoredItem[] = []
  const userMessages: string[] = []
  const filesModified = new Set<string>()

  for (const msg of messages) {
    if (msg.role === "user") {
      const text = msg.content.trim()
      if (text) userMessages.push(text.slice(0, 300))
    } else if (msg.role === "assistant") {
      const text = msg.content.trim()
      if (!text || text.length < 100) continue
      const snippet = text.slice(0, 400)

      if (summaryFilter(text.slice(0, 2000))) continue

      const extractions = extractPatterns(snippet)
      for (const ext of extractions) {
        const priority = PRIORITY_WEIGHTS[ext.category] ?? PRIORITY_WEIGHTS["general_work"]
        scoredItems.push({ priority, category: ext.category, text: ext.content.slice(0, 200) })
      }
    }
  }

  scoredItems.sort((a, b) => b.priority - a.priority)

  const seen = new Set<string>()
  const uniqueItems: ScoredItem[] = []
  for (const item of scoredItems) {
    const key = item.text.slice(0, 80)
    if (!seen.has(key)) {
      uniqueItems.push(item)
      seen.add(key)
    }
  }

  const lines: string[] = []
  lines.push(`Project: ${project}`)
  lines.push(`Session: ${sessionId.slice(0, 12)}`)
  lines.push(`User messages: ${userMessages.length}`)
  lines.push("")
  lines.push("USER REQUESTS:")
  for (const msg of userMessages.slice(-10)) {
    lines.push(`  - ${msg.slice(0, 200)}`)
  }
  lines.push("")

  let charUsed = lines.join("\n").length
  lines.push("RECENT WORK:")
  for (const item of uniqueItems) {
    const entry = `  [${item.category}] ${item.text.slice(0, 300)}`
    if (charUsed + entry.length > CHAR_BUDGET) break
    lines.push(entry)
    charUsed += entry.length
  }
  lines.push("")

  if (filesModified.size > 0) {
    lines.push("FILES MODIFIED:")
    for (const f of [...filesModified].sort().slice(0, 20)) {
      lines.push(`  - ${f}`)
    }
  }

  const summary = lines.join("\n")

  const stageFile = join(stagingDir, `precompact-${sessionId}.txt`)
  const tmpFile = stageFile + ".tmp"
  writeFileSync(tmpFile, summary, "utf-8")
  try { unlinkSync(stageFile) } catch {}
  try { renameSync(tmpFile, stageFile) } catch {
    writeFileSync(stageFile, summary, "utf-8")
  }

  const highValue = uniqueItems
    .filter((item) => item.priority >= 8 && item.text.length > 30)
    .slice(0, 5)

  if (highValue.length > 0) {
    await storeHighValue(highValue, project, cwd, db)
  }

  cleanupOldStaging(stagingDir)

  return summary
}

async function storeHighValue(
  const texts = items.map((item) => `[${item.category}] ${item.text}`)

  let embeddings: string[] | null = null
  try {
    embeddings = await daemon.encodeBatch(texts)
  } catch {}

  for (let i = 0; i < items.length; i++) {
    const item = items[i]
    const prefixed = texts[i]

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
          extraction_method: "precompact_plugin",
        },
      })
    } catch {}
  }
}

function cleanupOldStaging(stagingDir: string): void {
  try {
    const files = readdirSync(stagingDir)
      .filter((f) => f.startsWith("precompact-") && f.endsWith(".txt"))
      .map((f) => ({ name: f, path: join(stagingDir, f), mtime: statSync(join(stagingDir, f)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime)

    const twoHoursAgo = Date.now() - 2 * 60 * 60 * 1000
    for (const file of files) {
      if (file.mtime < twoHoursAgo) {
        try { unlinkSync(file.path) } catch {}
      }
    }
  } catch {}
}