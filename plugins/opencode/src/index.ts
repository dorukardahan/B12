import { join, basename } from "path"
import { randomUUID } from "node:crypto"
import { existsSync } from "fs"
import { homedir } from "os"
import { B12Database, getDbPath } from "./lib/db.js"
import { applyThinkingOption } from "./lib/chat-options.js"
import { isTrustedB12PermissionTool } from "./lib/permission.js"
import { sessionStart } from "./hooks/session-start.js"
import { messageRetrieval, shouldAttemptMessageRetrieval } from "./hooks/message-retrieval.js"
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
import { fetchSessionMessages, type SessionMessagesResponse } from "./lib/session-messages.js"

const B12_BASE = process.env.B12_DATA_DIR || join(homedir(), ".B12")

interface PluginContext {
  project: { path: string }
  client: {
    session: {
      messages: (opts: { path: { id: string } }) => Promise<SessionMessagesResponse>
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
  stagingDir: string
}

export const B12Plugin = async (ctx: PluginContext) => {
  const { client, directory } = ctx
  const dbPath = getDbPath()
  let db: B12Database | null = null

  function tryOpenDatabase(): B12Database | null {
    if (db) return db
    try {
      if (existsSync(dbPath)) {
        db = new B12Database(dbPath)
      }
    } catch {
      db = null
    }
    return db
  }

  tryOpenDatabase()

  const projectName = basename(directory)
  const states = new Map<string, PluginState>()
  const postToolQueues = new Map<string, Promise<void>>()

  async function enqueuePostTool(sessionId: string | undefined, task: () => Promise<void>): Promise<void> {
    const key = sessionId || "default"
    const previous = postToolQueues.get(key) ?? Promise.resolve()
    const next = previous.catch(() => undefined).then(task)
    postToolQueues.set(key, next)
    try {
      await next
    } finally {
      if (postToolQueues.get(key) === next) {
        postToolQueues.delete(key)
      }
    }
  }

  async function getPluginState(openCodeSessionId?: string | null): Promise<PluginState> {
    const key = openCodeSessionId || "default"
    const currentDb = tryOpenDatabase()
    const existing = states.get(key)
    if (existing) {
      if (!existing.db && currentDb) existing.db = currentDb
      return existing
    }

    const safeKey = key.replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 96) || "default"
    const sessionId = `oc-${safeKey}`
    const stagingDir = join(B12_BASE, "memory-staging", projectName, safeKey)
    const loaded = await loadWorkingMemory(stagingDir)
    const workingMemory = loaded?.session_id === sessionId
      ? loaded
      : createWorkingMemory(sessionId)
    const state: PluginState = {
      db: currentDb,
      sessionState: createSessionState(projectName, directory, "opencode"),
      workingMemory,
      sessionId,
      isFirstMessage: true,
      currentSessionId: key,
      stagingDir,
    }
    states.set(key, state)
    return state
  }

  return {
    "experimental.chat.system.transform": async (
      _input: { sessionID?: string; model: unknown },
      output: { system: string[] },
    ) => {
      const state = await getPluginState(_input.sessionID)
      const hasSessionId = Boolean(_input.sessionID)
      if (hasSessionId && !state.isFirstMessage) return
      if (!state.db) return
      if (hasSessionId) state.isFirstMessage = false

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
      if (isTrustedB12PermissionTool(input)) {
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
      applyThinkingOption(_input.provider, _input.model, output.options)
    },

    "chat.message": async (
      input: { sessionID: string; agent?: string },
      output: {
        message: { id?: string; role: string; content?: string }
        parts: Array<{ id?: string; sessionID?: string; messageID?: string; type: string; text?: string }>
      },
    ) => {
      const state = await getPluginState(input.sessionID)
      if (!state.db) return

      const userText = output.parts
        ?.filter((p) => p.type === "text" && p.text)
        .map((p) => p.text!)
        .join(" ")
        .trim()

      if (!shouldAttemptMessageRetrieval(userText)) return

      try {
        const memoryContext = await messageRetrieval(userText, projectName, state.db)
        if (memoryContext) {
          output.parts.unshift({
            id: `b12-memory-${randomUUID()}`,
            sessionID: input.sessionID,
            messageID: output.message.id || input.sessionID,
            type: "text",
            text: memoryContext,
          })
        }
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
      await enqueuePostTool(input.sessionID, async () => {
        const state = await getPluginState(input.sessionID)
        if (!state.db) return
        try {
          const result = await postTool(
            {
              tool: input.tool,
              args: input.args,
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
              stagingDir: state.stagingDir,
            },
          )
          state.sessionState = result.sessionState
          state.workingMemory = result.workingMemory
          if (result.surfaced) {
            output.output = output.output
              ? `${output.output}\n\n${result.surfaced}`
              : result.surfaced
            output.metadata = {
              ...(typeof output.metadata === "object" && output.metadata ? output.metadata : {}),
              b12SurfacedMemories: true,
            }
          }
        } catch {}
      })
    },

    "experimental.session.compacting": async (
      input: { sessionID: string },
      output: { context: string[]; prompt?: string },
    ) => {
      const state = await getPluginState(input.sessionID)
      if (!state.db) return
      try {
        const msgs = await fetchSessionMessages(client, input.sessionID)
        const summary = await preCompact(
          msgs,
          state.sessionId,
          projectName,
          directory,
          state.db,
          state.workingMemory.modified_files,
        )
        output.context.push(summary)
      } catch {}
    },

    event: async (input: {
      event: { type: string; properties?: Record<string, unknown> }
    }) => {
      const evt = input.event
      if (evt.type !== "session.idle") return

      const sid =
        (evt.properties?.sessionID as string) ||
        "default"
      const state = await getPluginState(sid)
      if (!state.db) return

      try {
        let msgs: Array<{ role: "user" | "assistant" | "system"; content: string }> = []
        if (sid) {
          try {
            msgs = await fetchSessionMessages(client, sid)
          } catch {}
        }
        if (!msgs.some((msg) => msg.content.trim())) return

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
