export interface SessionMessagesResult {
  info: { role: string; id: string }
  parts: Array<{ type: string; text?: string }>
}

export type SessionMessagesResponse =
  | SessionMessagesResult[]
  | { data?: SessionMessagesResult[] | null }
  | null
  | undefined

function unwrapMessages(response: SessionMessagesResponse): SessionMessagesResult[] {
  if (Array.isArray(response)) return response
  if (response && Array.isArray(response.data)) return response.data
  return []
}

export async function fetchSessionMessages(
  client: {
    session: {
      messages: (opts: { path: { id: string } }) => Promise<SessionMessagesResponse>
    }
  },
  sessionId: string,
): Promise<Array<{ role: "user" | "assistant" | "system"; content: string }>> {
  const rawMsgs = unwrapMessages(
    await client.session.messages({ path: { id: sessionId } }),
  )
  const msgs: Array<{ role: "user" | "assistant" | "system"; content: string }> = []
  for (const m of rawMsgs) {
    const role = m.info.role
    if (role !== "user" && role !== "assistant" && role !== "system") continue
    const text = m.parts
      ?.filter((p) => p.type === "text" && p.text)
      .map((p) => p.text!)
      .join("\n")
    if (text) {
      msgs.push({ role, content: text })
    }
  }
  return msgs
}
