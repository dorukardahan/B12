const TRUSTED_B12_TOOL_RE = /^(?:mcp__B12__|B12_)memory_(?:store|search|update|quality)$/
const B12_TOOL_NAME_RE = /^memory_(?:store|search|update|quality)$/

function stringValues(value: unknown): string[] {
  if (typeof value === "string") return [value]
  if (Array.isArray(value)) return value.flatMap(stringValues)
  return []
}

export function isTrustedB12PermissionTool(input: {
  id?: string
  type?: string
  pattern?: string | string[]
  title?: string
  metadata?: Record<string, unknown>
}): boolean {
  if (input.type && !["tool", "mcp_tool", "permission"].includes(input.type)) {
    return false
  }
  const metadata = input.metadata || {}
  const canonical = [
    input.id,
    ...stringValues(input.pattern),
    metadata.tool,
    metadata.toolName,
    metadata.name,
    metadata.command,
  ]
    .filter((value): value is string => typeof value === "string")
    .map((value) => value.trim())

  if (canonical.some((value) => TRUSTED_B12_TOOL_RE.test(value))) return true

  const server = String(metadata.server || metadata.namespace || "").toLowerCase()
  if (server !== "b12") return false

  return [
    ...canonical,
    typeof input.title === "string" ? input.title.trim() : "",
  ].some((value) => B12_TOOL_NAME_RE.test(value) || TRUSTED_B12_TOOL_RE.test(value))
}
