import { join, basename } from "path"
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "fs"
import { homedir } from "os"
import { B12Database, computeContentHash } from "../lib/db.js"
import * as daemon from "../lib/daemon.js"
import { extractPatterns, summaryFilter } from "../lib/patterns.js"
import {
  type WorkingMemory,
  type SessionState,
  createWorkingMemory,
  addActiveFile,
  addModifiedFile,
  addSearchPattern,
  saveWorkingMemory,
  appendFeedback,
} from "../lib/state.js"

const B12_BASE = process.env.B12_DATA_DIR || join(homedir(), ".B12")
const CHECKPOINT_CALL_INTERVAL = 15
const CHECKPOINT_TIME_INTERVAL = 600000

interface ToolInput {
  tool: string
  args: Record<string, unknown>
}

interface ToolOutput {
  args: Record<string, unknown>
  result?: string
}

interface PostToolDeps {
  db: B12Database
  project: string
  cwd: string
  sessionId: string
  sessionState: SessionState
  workingMemory: WorkingMemory
}

export async function postTool(
  input: ToolInput,
  output: ToolOutput,
  deps: PostToolDeps
): Promise<{ sessionState: SessionState; workingMemory: WorkingMemory; surfaced?: string }> {
  const { db, project, sessionId, sessionState, workingMemory } = deps
  const toolName = input.tool
  const result = { sessionState, workingMemory, surfaced: undefined as string | undefined }

  if (
    toolName === "B12_memory_store" ||
    toolName === "mcp__B12__memory_store" ||
    toolName === "B12_memory_search" ||
    toolName === "mcp__B12__memory_search" ||
    toolName === "B12_memory_update" ||
    toolName === "mcp__B12__memory_update"
  ) {
    return result
  }

  let entity = ""
  let entityType: "file" | "modified" | "search" = "file"

  if (toolName === "read" || toolName === "edit" || toolName === "write") {
    const fp = (input.args.filePath as string) || (output.args.filePath as string) || ""
    if (fp) {
      entity = basename(fp)
      if (toolName === "edit" || toolName === "write") {
        entityType = "modified"
      }
    }
  } else if (toolName === "glob" || toolName === "grep") {
    const pattern = (input.args.pattern as string) || ""
    if (pattern) {
      entity = pattern.slice(0, 80)
      entityType = "search"
    }
  }

  if (entity) {
    if (entityType === "modified") {
      result.workingMemory = addModifiedFile(result.workingMemory, entity)
      result.workingMemory = addActiveFile(result.workingMemory, entity)
    } else if (entityType === "file") {
      result.workingMemory = addActiveFile(result.workingMemory, entity)
    } else if (entityType === "search") {
      result.workingMemory = addSearchPattern(result.workingMemory, entity)
    }

    const stagingDir = join(B12_BASE, "memory-staging")
    saveWorkingMemory(stagingDir, result.workingMemory)
  }

  if (toolName === "read" || toolName === "edit") {
    const filePath = (input.args.filePath as string) || (output.args.filePath as string) || ""
    if (filePath) {
      const memResults = db.search({
        query: basename(filePath),
        tags: [`proj:${project}`],
        limit: 3,
      })
      if (memResults.length > 0) {
        result.surfaced = memResults
          .map((m) => m.display)
          .join("\n")
      }
    }
  }

  if (toolName === "bash") {
    const cmdOutput = output.result || ""
    const errorIndicators = [
      "error", "failed", "exception", "traceback",
      "errno", "permission denied", "not found",
      "command not found", "hata", "başarısız",
    ]
    const hasError = errorIndicators.some((ind) =>
      cmdOutput.toLowerCase().includes(ind)
    )
    if (hasError) {
      const errorMemories = db.search({
        query: cmdOutput.slice(0, 200),
        mode: "hybrid",
        limit: 3,
      })
      if (errorMemories.length > 0) {
        const surfaceText = errorMemories
          .map((m) => m.display)
          .join("\n")
        result.surfaced = result.surfaced
          ? `${result.surfaced}\n\n## Related Error Memories\n${surfaceText}`
          : `## Related Error Memories\n${surfaceText}`
      }
    }
  }

  result.sessionState = {
    ...result.sessionState,
    callCount: result.sessionState.callCount + 1,
  }
  const elapsed = Date.now() - result.sessionState.lastCheckpoint

  if (
    result.sessionState.callCount % CHECKPOINT_CALL_INTERVAL === 0 ||
    elapsed >= CHECKPOINT_TIME_INTERVAL
  ) {
    await runCheckpoint(input, output, db, project)
    result.sessionState = {
      ...result.sessionState,
      lastCheckpoint: Date.now(),
    }
  }

  try {
    const stagingDir = join(B12_BASE, "memory-staging")
    await appendFeedback(stagingDir, {
      timestamp: Math.floor(Date.now() / 1000),
      session_id: sessionId,
      type: "tool_usage",
      data: { tool: toolName, entity, entityType },
    })
  } catch {}

  return result
}

async function runCheckpoint(
  input: ToolInput,
  output: ToolOutput,
  db: B12Database,
  project: string
): Promise<void> {
  const parts: string[] = []

  const inputContent = [
    typeof input.args.content === "string" ? input.args.content : "",
    typeof input.args.command === "string" ? input.args.command : "",
    typeof input.args.filePath === "string" ? input.args.filePath : "",
    typeof input.args.pattern === "string" ? input.args.pattern : "",
  ].filter(Boolean).join("\n")

  const outputContent = typeof output.result === "string" ? output.result : ""
  const scanText = (inputContent + "\n" + outputContent).slice(0, 4000)

  if (scanText.length < 20) return
  if (summaryFilter(scanText.slice(0, 2000))) return

  const extractions = extractPatterns(scanText)
  if (extractions.length === 0) return

  const scored = extractions.filter((e) => e.score >= 6).slice(0, 5)
  if (scored.length === 0) return

  for (const item of scored) {
    try {
      const content = `[${item.category}] ${item.content.slice(0, 300)}`
      const tags = `proj:${project},checkpoint,${item.category}`
      const meta = {
        type: item.category,
        source: "checkpoint",
        importance_score: 0.7,
        project,
      }

      db.store({
        content,
        tags,
        memory_type: item.category,
        metadata: meta,
      })
    } catch {}
  }
}
