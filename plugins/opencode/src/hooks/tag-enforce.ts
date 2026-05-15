import { join, basename } from "path"
import { homedir } from "os"
import type { B12Database } from "../lib/db.js"

const B12_BASE = process.env.B12_DATA_DIR || join(homedir(), ".B12")

interface TagEnforceInput {
  tool: string
  args: Record<string, unknown>
}

interface TagEnforceOutput {
  args: Record<string, unknown>
}

export function tagEnforce(
  input: TagEnforceInput,
  output: TagEnforceOutput,
  project: string,
  setupContext: string
): void {
  if (input.tool !== "B12_memory_store" && input.tool !== "mcp__B12__memory_store") return

  const args = output.args
  const metadataRaw = args.metadata as Record<string, unknown> | undefined

  let tags: string[] = []
  if (typeof args.tags === "string") {
    tags = args.tags.split(",").map((t: string) => t.trim()).filter(Boolean)
  } else if (Array.isArray(args.tags)) {
    tags = args.tags as string[]
  }

  let hasProj = tags.some((t) => t.startsWith("proj:"))
  let hasUser = tags.some((t) => t.startsWith("user:"))

  if (!hasProj && project) {
    tags.push(`proj:${project}`)
    hasProj = true
  }

  if (!hasUser) {
    if (setupContext) {
      tags.push(`user:${setupContext}`)
    } else {
      tags.push("user:universal")
    }
    hasUser = true
  }

  output.args = { ...args, tags: tags.join(",") }
}
