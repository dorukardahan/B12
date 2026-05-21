function textOf(value: unknown): string {
  if (typeof value === "string") return value
  if (value && typeof value === "object") return JSON.stringify(value)
  return ""
}

export function shouldEnableThinking(provider: unknown, model: unknown): boolean {
  const descriptor = `${textOf(provider)} ${textOf(model)}`.toLowerCase()
  return descriptor.includes("anthropic") || descriptor.includes("claude")
}

export function applyThinkingOption(
  provider: unknown,
  model: unknown,
  options: Record<string, unknown>,
): void {
  if (options.thinking !== undefined) return
  if (shouldEnableThinking(provider, model)) {
    options.thinking = { type: "enabled", clear_thinking: true }
  }
}
