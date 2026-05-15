import { join, basename } from "path"
import { existsSync } from "fs"
import { homedir } from "os"
import { B12Database, getDbPath } from "./lib/db.js"
import { sessionStart } from "./hooks/session-start.js"
import { messageRetrieval } from "./hooks/message-retrieval.js"
import { tagEnforce } from "./hooks/tag-enforce.js"
import { postTool } from "./hooks/post-tool.js"
import { preCompact } from "./hooks/pre-compact.js"
import { sessionEnd } from "./hooks/session-end.js"
import {
  createWorkingMemory,
  createSessionState,
  loadWorkingMemory,
  type WorkingMemory,
  type SessionState,
} from "./lib/state.js"

const B12_BASE = process.env.B12_DATA_DIR || join(homedir(), ".B12")

interface SessionMessagesResult {
  info: { role: string; id: string }
  parts: Array<{ type: string; text?: string }>
}

interface PluginContext {
  project: { path: string }
  client: {
    session: {
      messages: (opts: { path: { id: string } }) => Promise<SessionMessagesResult[]>
    }
  }
  directory: string
  worktree: string
}

interface PluginState {
  db: B12Database | null
  sessionState: SessionState
  workingMemory: WorkingMemory
  sessionId: string
  isFirstMessage: boolean
  currentSessionId: string | null
}

async function fetchSessionMessages(
  client: PluginContext["client"],
  sessionId: string,
): Promise<Array<{ role: string; content: string }>> {
  const rawMsgs = await client.session.messages({ path: { id: sessionId } })
  const msgs: Array<{ role: string; content: string }> = []
  for (const m of rawMsgs) {
    const text = m.parts
      ?.filter((p) => p.type === "text" && p.text)
      .map((p) => p.text!)
      .join("\n")
    if (text) {
      msgs.push({ role: m.info.role, content: text })
    }
  }
  return msgs
}

export const B12Plugin = async (ctx: PluginContext) => {
  const { client, directory } = ctx
  const dbPath = getDbPath()
  let db: B12Database | null = null

  try {
    if (existsSync(dbPath)) {
      db = new B12Database(dbPath)
    }
  } catch {
    return {}
  }

  const projectName = basename(directory)
  const sessionId = "oc-" + Date.now().toString(36)
  const sessionState = createSessionState(projectName, directory, "opencode")
  const loaded = await loadWorkingMemory(join(B12_BASE, "memory-staging"))
  const workingMemory = loaded ?? createWorkingMemory(sessionId)

  const state: PluginState = {
    db,
    sessionState,
    workingMemory,
    sessionId,
    isFirstMessage: true,
    currentSessionId: null,
  }

  return {
    "experimental.chat.system.transform": async (
      _input: { sessionID?: string; model: unknown },
      output: { system: string[] },
    ) => {
      if (!state.isFirstMessage || !state.db) return
      state.isFirstMessage = false

      try {
        const context = await sessionStart(projectName, directory, state.db)
        if (context) {
          output.system.push(context)
        }
      } catch {}
    },

    "permission.ask": async (
      input: { id: string; type: string; pattern?: string | string[]; title: string; metadata: Record<string, unknown> },
      output: { status: "ask" | "deny" | "allow" },
    ) => {
      const isMemoryTool = input.title.includes("memory_store") ||
        input.title.includes("memory_search") ||
        input.title.includes("memory_update") ||
        input.title.includes("memory_quality") ||
        (input.pattern && (
          (typeof input.pattern === "string" && input.pattern.includes("B12_memory")) ||
          (Array.isArray(input.pattern) && input.pattern.some((p: string) => p.includes("B12_memory")))
        ))
      if (isMemoryTool) {
        output.status = "allow"
      }
    },

    "chat.params": async (
      _input: {
        sessionID: string
        agent: string
        model: unknown
        provider: unknown
        message: unknown
      },
      output: {
        temperature: number
        topP: number
        topK: number
        options: Record<string, unknown>
      },
    ) => {
      output.options.thinking = { type: "enabled", clear_thinking: true }
    },

    "chat.message": async (
      input: { sessionID: string; agent?: string },
      output: {
        message: { role: string; content?: string }
        parts: Array<{ type: string; text?: string }>
      },
    ) => {
      if (!state.db) return
      if (input.sessionID && !state.currentSessionId) {
        state.currentSessionId = input.sessionID
      }

      const userText = output.parts
        ?.filter((p) => p.type === "text" && p.text)
        .map((p) => p.text!)
        .join(" ")
        .trim()

      if (!userText || userText.length < 10 || userText.startsWith("/")) return
      if (/^(selam|merhaba|naber|hey|hi|hello|good morning)/i.test(userText))
        return

      try {
        await messageRetrieval(userText, projectName, state.db)
      } catch {}
    },

    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      output: { args: Record<string, unknown> },
    ) => {
      tagEnforce(input, output, projectName, "opencode")
    },

    "tool.execute.after": async (
      input: {
        tool: string
        sessionID: string
        callID: string
        args: Record<string, unknown>
      },
      output: { title: string; output: string; metadata: unknown },
    ) => {
      if (!state.db) return
      try {
        const result = await postTool(
          {
            tool: input.tool,
            args: input.args,
            result: output.output,
          },
          {
            args: output,
            result: output.output,
          },
          {
            db: state.db,
            project: projectName,
            cwd: directory,
            sessionId: state.sessionId,
            sessionState: state.sessionState,
            workingMemory: state.workingMemory,
          },
        )
        state.sessionState = result.sessionState
        state.workingMemory = result.workingMemory
      } catch {}
    },

    "experimental.session.compacting": async (
      input: { sessionID: string },
      output: { context: string[]; prompt?: string },
    ) => {
      if (!state.db || !state.currentSessionId) return
      try {
        const msgs = await fetchSessionMessages(client, state.currentSessionId)
        const summary = await preCompact(
          msgs,
          state.sessionId,
          projectName,
          directory,
          state.db,
        )
        output.context.push(summary)
      } catch {}
    },

    event: async (input: {
      event: { type: string; properties?: Record<string, unknown> }
    }) => {
      const evt = input.event
      if (evt.type !== "session.idle" && evt.type !== "session.deleted")
        return
      if (!state.db) return

      const sid =
        (evt.properties?.sessionID as string) ||
        state.currentSessionId ||
        state.sessionId

      try {
        let msgs: Array<{ role: string; content: string }> = []
        if (evt.type === "session.idle" && sid) {
          try {
            msgs = await fetchSessionMessages(client, sid)
          } catch {}
        }

        await sessionEnd(
          msgs,
          state.sessionId,
          projectName,
          directory,
          state.db,
        )
      } catch {}
    },
  }
}

export default B12Plugin
