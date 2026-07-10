import { expect, test, describe } from "bun:test"
import { isTrustedB12PermissionTool } from "../src/lib/permission.js"

// Covers the trusted-B12-tool auto-allow predicate and its deny paths.
// The helper gates an auto-allow for B12 MCP tool permissions, so the contract
// is: only exact trusted tool names pass; every other shape is denied.

describe("isTrustedB12PermissionTool — trusted shapes", () => {
  test("auto-allows the four canonical MCP-prefixed tool ids", () => {
    for (const id of [
      "mcp__B12__memory_store",
      "mcp__B12__memory_search",
      "mcp__B12__memory_update",
      "mcp__B12__memory_quality",
    ]) {
      expect(
        isTrustedB12PermissionTool({ id, type: "tool", metadata: {} }),
      ).toBe(true)
    }
  })

  test("auto-allows the bare-name prefix variant (B12_memory_*)", () => {
    for (const id of [
      "B12_memory_store",
      "B12_memory_search",
      "B12_memory_update",
      "B12_memory_quality",
    ]) {
      expect(
        isTrustedB12PermissionTool({ id, type: "tool", metadata: {} }),
      ).toBe(true)
    }
  })

  test("auto-allows via metadata.tool / toolName / name fields", () => {
    for (const key of ["tool", "toolName", "name"] as const) {
      expect(
        isTrustedB12PermissionTool({
          id: "something-else",
          type: "tool",
          metadata: { [key]: "mcp__B12__memory_store" },
        }),
      ).toBe(true)
    }
  })

  test("auto-allows via metadata.command", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "cmd-1",
        type: "tool",
        metadata: { command: "B12_memory_search" },
      }),
    ).toBe(true)
  })

  test("auto-allows via pattern (string) matching a trusted name", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "perm-1",
        type: "permission",
        pattern: "mcp__B12__memory_store",
        metadata: {},
      }),
    ).toBe(true)
  })

  test("auto-allows a pattern array only when every entry is trusted", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "perm-2",
        type: "permission",
        pattern: ["mcp__B12__memory_store", "B12_memory_quality"],
        metadata: {},
      }),
    ).toBe(true)
  })

  test("trims whitespace before matching trusted names", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "  mcp__B12__memory_store  ",
        type: "tool",
        metadata: {},
      }),
    ).toBe(true)
    expect(
      isTrustedB12PermissionTool({
        id: "x",
        type: "tool",
        metadata: { tool: "  B12_memory_search " },
      }),
    ).toBe(true)
  })

  test("bare memory_store passes ONLY when server/namespace is B12", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "memory_store",
        type: "tool",
        metadata: { server: "B12" },
      }),
    ).toBe(true)
    expect(
      isTrustedB12PermissionTool({
        id: "memory_store",
        type: "tool",
        metadata: { namespace: "b12" },
      }),
    ).toBe(true)
    expect(
      isTrustedB12PermissionTool({
        id: "memory_search",
        type: "tool",
        metadata: {},
      }),
    ).toBe(false)
  })

  test("a bare tool name in the title field passes under B12 server", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "perm-title",
        type: "permission",
        title: "memory_update",
        metadata: { server: "B12" },
      }),
    ).toBe(true)
  })
})

describe("isTrustedB12PermissionTool — deny shapes", () => {
  test("rejects an unknown type", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "mcp__B12__memory_store",
        type: "resource",
        metadata: {},
      }),
    ).toBe(false)
  })

  test("rejects a trusted-looking title when not under B12 server", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "untrusted",
        type: "tool",
        title: "please run memory_store for me",
        metadata: {},
      }),
    ).toBe(false)
  })

  test("rejects bare memory_store with a non-B12 server", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "memory_store",
        type: "tool",
        metadata: { server: "other-mcp" },
      }),
    ).toBe(false)
  })

  test("rejects a non-B12 tool name even under B12 server", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "memory_delete",
        type: "tool",
        metadata: { server: "B12" },
      }),
    ).toBe(false)
    expect(
      isTrustedB12PermissionTool({
        id: "filesystem_read",
        type: "tool",
        metadata: { server: "B12" },
      }),
    ).toBe(false)
  })

  test("rejects near-miss names that are not exact tool ids", () => {
    for (const id of [
      "mcp__B12__memory_store_extra",
      "B12_memory_storev2",
      "mcp__Other__memory_store",
      "mcp__B12__memory_delete",
      "B12MEMORY_STORE",
    ]) {
      expect(
        isTrustedB12PermissionTool({ id, type: "tool", metadata: {} }),
      ).toBe(false)
    }
  })

  test("rejects empty / missing identifiers", () => {
    expect(isTrustedB12PermissionTool({ metadata: {} })).toBe(false)
    expect(
      isTrustedB12PermissionTool({ id: "", type: "tool", metadata: {} }),
    ).toBe(false)
  })

  test("rejects array patterns with no trusted element", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "perm-3",
        type: "permission",
        pattern: ["tool_a", "tool_b"],
        metadata: {},
      }),
    ).toBe(false)
  })

  test("rejects a mixed trusted and untrusted pattern array", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "perm-4",
        type: "permission",
        pattern: ["B12_memory_quality", "unrelated_tool"],
        metadata: {},
      }),
    ).toBe(false)
  })

  test("rejects blank or malformed entries mixed into trusted patterns", () => {
    expect(
      isTrustedB12PermissionTool({
        id: "perm-5",
        type: "permission",
        pattern: ["B12_memory_quality", ""],
        metadata: {},
      }),
    ).toBe(false)
    expect(
      isTrustedB12PermissionTool({
        id: "perm-6",
        type: "permission",
        pattern: ["B12_memory_quality", 42] as unknown as string[],
        metadata: {},
      }),
    ).toBe(false)
  })
})
