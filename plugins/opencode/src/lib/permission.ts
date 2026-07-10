const TRUSTED_B12_TOOL_RE = /^(?:mcp__B12__|B12_)memory_(?:store|search|update|quality)$/
const B12_TOOL_NAME_RE = /^memory_(?:store|search|update|quality)$/

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
  const server = String(metadata.server || metadata.namespace || "").toLowerCase()
  const isTrustedCandidate = (value: string): boolean =>
    TRUSTED_B12_TOOL_RE.test(value) ||
    (server === "b12" && B12_TOOL_NAME_RE.test(value))
  const rawPattern: unknown = input.pattern
  let patterns: string[] = []
  if (rawPattern !== undefined) {
    if (typeof rawPattern === "string") {
      patterns = [rawPattern]
    } else if (
      Array.isArray(rawPattern) &&
      rawPattern.every((value) => typeof value === "string")
    ) {
      patterns = rawPattern
    } else {
      return false
    }
  }
  patterns = patterns.map((value) => value.trim())

  // A permission pattern array can describe several alternatives that an
  // "always" approval would cover. Fail closed on blank/malformed entries and
  // never let one trusted B12 entry bless a mixed array.
  if (
    patterns.some((value) => !value) ||
    (patterns.length > 0 && !patterns.every(isTrustedCandidate))
  ) return false

  const canonical = [
    input.id,
    metadata.tool,
    metadata.toolName,
    metadata.name,
    metadata.command,
  ]
    .filter((value): value is string => typeof value === "string")
    .map((value) => value.trim())

  return [
    ...canonical,
    ...patterns,
    typeof input.title === "string" ? input.title.trim() : "",
  ].some(isTrustedCandidate)
}
