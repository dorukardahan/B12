import { join } from "path"
import { existsSync, readFileSync } from "fs"
import { homedir } from "os"
import type { B12Database } from "../lib/db.js"
import * as daemon from "../lib/daemon.js"

const CHAR_BUDGET = 6000

function b12Base(): string {
  return process.env.B12_DATA_DIR || join(homedir(), ".B12")
}

function b12HookDir(base: string): string {
  return process.env.B12_HOOK_DIR || join(base, "hooks")
}

export async function sessionStart(
  project: string,
  cwd: string,
  db: B12Database
): Promise<string> {
  const B12_BASE = b12Base()
  const B12_HOOK_DIR = b12HookDir(B12_BASE)
  const venvPython = join(homedir(), ".local", "b12-venv", "bin", "python3")
  const scriptPath = join(B12_HOOK_DIR, "scripts", "embed_daemon.py")
  if (existsSync(venvPython) && existsSync(scriptPath)) {
    await daemon.startDaemon(venvPython, scriptPath)
  }

  const sections: string[] = []
  let totalChars = 0

  const profilePath = join(B12_BASE, "user-profile.md")
  if (existsSync(profilePath)) {
    const profile = readFileSync(profilePath, "utf-8").trim()
    if (profile && totalChars + profile.length < CHAR_BUDGET) {
      sections.push(`## User Profile\n${profile}`)
      totalChars += profile.length
    }
  }

  const summaryDir = join(B12_BASE, "memory-summaries")
  const summaryFile = project
    ? join(summaryDir, `${project}-latest.md`)
    : null
  if (summaryFile && existsSync(summaryFile)) {
    const summary = readFileSync(summaryFile, "utf-8").trim()
    const clipped = summary.slice(0, 1500)
    const section = `## Last Session Summary\n${clipped}`
    if (clipped && totalChars + section.length < CHAR_BUDGET) {
      sections.push(section)
      totalChars += section.length
    }
  }

  const context = db.getSessionContext(project)

  if (context.projectMemories.length > 0) {
    const lines = context.projectMemories
      .map((m) => `[${m.memory_type}] ${m.content}`)
      .join("\n")
    if (totalChars + lines.length < CHAR_BUDGET) {
      sections.push(`## Project Memories (${project})\n${lines}`)
      totalChars += lines.length
    }
  }

  if (context.universalMemories.length > 0) {
    const lines = context.universalMemories
      .map((m) => `[${m.memory_type}] ${m.content}`)
      .join("\n")
    if (totalChars + lines.length < CHAR_BUDGET) {
      sections.push(`## Cross-Project Knowledge\n${lines}`)
      totalChars += lines.length
    }
  }

  const guardrails = db.getContentGuardrails()
  if (guardrails.length > 0) {
    const text = guardrails.slice(0, 3).join("\n")
    if (totalChars + text.length < CHAR_BUDGET) {
      sections.push(`## Content Guardrails\n${text}`)
      totalChars += text.length
    }
  }

  const feedbackDir = join(B12_BASE, "memory-staging")
  const feedbackFile = join(feedbackDir, "feedback.jsonl")
  if (existsSync(feedbackFile)) {
    try {
      const lines = readFileSync(feedbackFile, "utf-8")
        .split("\n")
        .filter((l) => l.trim())
        .slice(-20)
      const lowRated = lines
        .map((l) => { try { return JSON.parse(l) } catch { return null } })
        .filter((e): e is Record<string, unknown> & { rating: string } => e != null && e.rating === "-1")
        .slice(0, 3)
      if (lowRated.length > 0) {
        const text = lowRated
          .map((e) => `- ${String(e.query || "").slice(0, 80)}: ${String(e.feedback || "low quality")}`)
          .join("\n")
        if (totalChars + text.length < CHAR_BUDGET) {
          sections.push(`## Recent Feedback (Low Quality)\n${text}`)
          totalChars += text.length
        }
      }
    } catch {}
  }

  let result = sections.join("\n\n")
  if (result.length > CHAR_BUDGET) {
    result = result.slice(0, CHAR_BUDGET)
  }
  return result
}
