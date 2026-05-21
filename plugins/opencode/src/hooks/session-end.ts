import { join, basename, dirname } from "path"
import { randomUUID } from "node:crypto"
import { existsSync, mkdirSync, writeFileSync, readdirSync, statSync, unlinkSync, renameSync } from "fs"
import { homedir } from "os"
import type { B12Database } from "../lib/db.js"
import * as daemon from "../lib/daemon.js"
import {
  extractPatterns,
  extractMacroVerbs,
  summaryFilter,
  scoreExtraction,
  dedup,
} from "../lib/patterns.js"

function getB12Base(): string {
  return process.env.B12_DATA_DIR || join(homedir(), ".B12")
}

interface SessionMessage {
  role: "user" | "assistant" | "system"
  content: string
}

interface Extraction {
  content: string
  category: string
  score: number
}

const FILE_TOKEN_RE =
  /(?:^|\s)(\.{0,2}\/[\w./-]+\.\w{1,10}|[\w./-]+\/[\w./-]+\.\w{1,10}|[\w.-]+\.(?:py|ts|tsx|js|jsx|json|md|toml|yaml|yml|sql|html|css|scss|sh|zsh|bash|txt))(?:\s|$)/g

export function extractModifiedFileTokens(text: string): string[] {
  const out: string[] = []
  let match: RegExpExecArray | null
  FILE_TOKEN_RE.lastIndex = 0
  while ((match = FILE_TOKEN_RE.exec(text)) !== null) {
    const token = match[1].trim()
    if (/^\d+(?:\.\d+)+$/.test(token)) continue
    if (/^[\w.-]+\.(?:com|org|net|io|ai|dev)$/i.test(token)) continue
    out.push(token)
  }
  return out
}

export async function sessionEnd(
  messages: SessionMessage[],
  sessionId: string,
  project: string,
  cwd: string,
  db: B12Database
): Promise<void> {
  if (!messages.some((msg) => msg.content.trim())) return

  const summaryDir = join(getB12Base(), "memory-summaries")
  mkdirSync(summaryDir, { recursive: true })

  const decisions: string[] = []
  const errors: string[] = []
  const learnings: string[] = []
  const preferences: string[] = []
  const architecture: string[] = []
  const workflows: string[] = []
  const fileConventions: string[] = []
  const corrections: string[] = []
  const infrastructure: string[] = []
  const contentItems: string[] = []
  const extractedItems: Extraction[] = []
  const userRequests: string[] = []
  const filesModified: string[] = []

  for (const msg of messages) {
    if (msg.role === "user") {
      const text = msg.content.trim()
      if (text && text.length > 5) {
        userRequests.push(text.slice(0, 300))
      }
      continue
    }

    if (msg.role !== "assistant") continue
    const text = msg.content
    if (!text || text.length < 50) continue
    if (summaryFilter(text.slice(0, 2000))) continue

    const extractions = extractPatterns(text)
    for (const ext of extractions) {
      const categoryMap: Record<string, string> = {
        implicit_decision: "decision",
        error_fix: "error",
        tool_pref: "preference",
      }
      const category = categoryMap[ext.category] || ext.category
      const score = Math.max(ext.score, scoreExtraction(ext.content, category))
      if (score < 2) continue
      extractedItems.push({ content: ext.content.slice(0, 300), category, score })

      switch (category) {
        case "decision":
          decisions.push(ext.content.slice(0, 300))
          break
        case "error":
          errors.push(ext.content.slice(0, 300))
          break
        case "learning":
          learnings.push(ext.content.slice(0, 300))
          break
        case "preference":
        case "tool_pref":
          preferences.push(ext.content.slice(0, 300))
          break
        case "architecture":
          architecture.push(ext.content.slice(0, 300))
          break
        case "workflow":
          workflows.push(ext.content.slice(0, 300))
          break
        case "file_convention":
          fileConventions.push(ext.content.slice(0, 300))
          break
        case "correction":
          corrections.push(ext.content.slice(0, 300))
          break
        case "infrastructure":
          infrastructure.push(ext.content.slice(0, 300))
          break
        case "content":
          contentItems.push(ext.content.slice(0, 300))
          break
      }
    }

    filesModified.push(...extractModifiedFileTokens(text))
  }

  const summaryLines: string[] = []
  summaryLines.push(`# Session Summary (${new Date().toISOString().slice(0, 10)})`)
  summaryLines.push(`Project: ${project} | Session: ${sessionId.slice(0, 12)}`)
  summaryLines.push("")

  if (decisions.length > 0) {
    summaryLines.push("## Decisions Made")
    for (const d of dedup(decisions)) summaryLines.push(`- ${d}`)
    summaryLines.push("")
  }

  if (errors.length > 0) {
    summaryLines.push("## Errors & Fixes")
    for (const e of dedup(errors)) summaryLines.push(`- ${e}`)
    summaryLines.push("")
  }

  if (learnings.length > 0) {
    summaryLines.push("## Key Learnings")
    for (const l of dedup(learnings)) summaryLines.push(`- ${l}`)
    summaryLines.push("")
  }

  if (preferences.length > 0) {
    summaryLines.push("## User Preferences")
    for (const p of dedup(preferences)) summaryLines.push(`- ${p}`)
    summaryLines.push("")
  }

  if (architecture.length > 0) {
    summaryLines.push("## Architecture")
    for (const a of dedup(architecture)) summaryLines.push(`- ${a}`)
    summaryLines.push("")
  }

  if (workflows.length > 0) {
    summaryLines.push("## Workflows")
    for (const w of dedup(workflows)) summaryLines.push(`- ${w}`)
    summaryLines.push("")
  }

  if (fileConventions.length > 0) {
    summaryLines.push("## File Conventions")
    for (const f of dedup(fileConventions)) summaryLines.push(`- ${f}`)
    summaryLines.push("")
  }

  if (corrections.length > 0) {
    summaryLines.push("## Corrections")
    for (const c of dedup(corrections)) summaryLines.push(`- ${c}`)
    summaryLines.push("")
  }

  if (userRequests.length > 0) {
    summaryLines.push("## User Requests")
    for (const r of dedup(userRequests, 10)) summaryLines.push(`- ${r}`)
    summaryLines.push("")
  }

  if (filesModified.length > 0) {
    const unique = [...new Set(filesModified)].sort().slice(0, 30)
    summaryLines.push("## Files Modified")
    for (const f of unique) summaryLines.push(`- ${f}`)
    summaryLines.push("")
  }

  const summary = summaryLines.join("\n")

  const projectSummaryFile = join(summaryDir, `${project}-latest.md`)
  atomicWrite(projectSummaryFile, summary)

  const globalSummaryFile = join(summaryDir, "global-latest.md")
  atomicWrite(globalSummaryFile, summary.slice(0, 2000))

  const handoffFile = join(summaryDir, `${project}-handoff.md`)
  atomicWrite(handoffFile, summary)

  const allItems: Extraction[] = [
    ...decisions.map((d) => ({ content: d, category: "decision", score: 8 })),
    ...errors.map((e) => ({ content: e, category: "error", score: 8 })),
    ...learnings.map((l) => ({ content: l, category: "learning", score: 7 })),
    ...preferences.map((p) => ({ content: p, category: "preference", score: 9 })),
    ...architecture.map((a) => ({ content: a, category: "architecture", score: 7 })),
    ...workflows.map((w) => ({ content: w, category: "workflow", score: 6 })),
    ...fileConventions.map((f) => ({ content: f, category: "file_convention", score: 6 })),
    ...corrections.map((c) => ({ content: c, category: "correction", score: 8 })),
    ...infrastructure.map((i) => ({ content: i, category: "infrastructure", score: 5 })),
    ...contentItems.map((c) => ({ content: c, category: "content", score: 6 })),
    ...extractedItems.filter((item) => ![
      "decision",
      "error",
      "learning",
      "preference",
      "architecture",
      "workflow",
      "file_convention",
      "correction",
      "infrastructure",
      "content",
    ].includes(item.category)),
  ]

  allItems.sort((a, b) => b.score - a.score)
  const topItems = allItems.filter((item) => item.score >= 6).slice(0, 20)

  let embeddings: string[] | null = null
  if (topItems.length > 0) {
    const texts = topItems.map((item) => `[${item.category}] ${item.content}`)
    try {
      embeddings = await daemon.encodeBatch(texts)
    } catch {}
  }

  for (let i = 0; i < topItems.length; i++) {
    const item = topItems[i]
    const prefixed = `[${item.category}] ${item.content}`
    try {
      const stored = db.store({
        content: prefixed,
        tags: `proj:${project},session-end,${item.category},${new Date().toISOString().slice(0, 7)}`,
        memory_type: item.category,
        embedding: embeddings?.[i] ?? null,
        metadata: {
          type: item.category,
          source: "session_end",
          importance_score: item.score >= 8 ? 1.5 : 1.0,
          project,
          session_id: sessionId.slice(0, 12),
          extraction_method: "session_end_plugin",
        },
      })
      if (embeddings?.[i]) db.storeEmbedding(stored.id, embeddings[i])
    } catch {}
  }

  // OpenCode `[M#]` macro verbs (Codex review PR #50). Gated on
  // B12_OPENCODE_MACRO_INGEST=true so default behavior is unchanged.
  // User-typed `[M#decision] ...` lines bypass the regex pipeline.
  const macroFlag = (process.env.B12_OPENCODE_MACRO_INGEST || "false").toLowerCase()
  if (["1", "true", "yes"].includes(macroFlag)) {
    const macros = extractMacroVerbs(messages)
    for (const mv of macros) {
      try {
        db.store({
          content: mv.content,
          tags: `proj:${project},${mv.type},extraction:macro_verbs,${new Date().toISOString().slice(0, 7)}`,
          memory_type: mv.type,
          metadata: {
            type: mv.type,
            source: "session_end",
            importance_score: mv.importance,
            project,
            session_id: sessionId.slice(0, 12),
            extraction_method: "macro_verbs",
            source_role: mv.source,
          },
        })
      } catch {}
    }
  }
}

export function buildAtomicTempPath(filePath: string): string {
  return join(dirname(filePath), `.${basename(filePath)}.${process.pid}.${randomUUID()}.tmp`)
}

export function atomicWrite(filePath: string, content: string): void {
  const dir = dirname(filePath)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  const tmpFile = buildAtomicTempPath(filePath)
  writeFileSync(tmpFile, content, "utf-8")
  try {
    renameSync(tmpFile, filePath)
  } catch (err) {
    try { unlinkSync(tmpFile) } catch {}
    throw err
  }
}
