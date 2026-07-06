import { expect, test } from "bun:test"
import { tagEnforce } from "../src/hooks/tag-enforce"

// tagEnforce is a pure, synchronous hook (no fs/db/daemon) that runs in the
// `tool.execute.before` lifecycle. It injects mandatory proj:/user: tag
// namespaces into B12_memory_store calls so memories stay project-scoped and
// user-attributed. These tests cover every branch: tool gating, tag-source
// precedence (metadata.tags string/array → args.tags string/array), proj/user
// injection, setupContext fallback to user:universal, and the
// mcp__B12__memory_store vs B12_memory_store output-shape divergence.

const PROJ = "demo"
const SETUP = "opencode"

function run(tool: string, args: Record<string, unknown>) {
  const output = { args: { ...args } }
  tagEnforce({ tool, args }, output, PROJ, SETUP)
  return output.args
}

test("ignores non-memory-store tools and leaves output untouched", () => {
  const args = { content: "hello", tags: "feat" }
  const out = run("B12_memory_search", args)

  expect(out).toEqual(args)
})

test("ignores arbitrary tools even if they look related", () => {
  const args = { content: "hello" }
  const out = run("read", args)

  expect(out).toEqual(args)
})

test("injects proj: and user: tags when both are missing", () => {
  const out = run("B12_memory_store", { content: "decided to use sqlite" }) as any

  expect(out.tags).toContain("proj:demo")
  expect(out.tags).toContain("user:opencode")
  // metadata also carries the resolved tags
  expect(out.metadata.tags).toContain("proj:demo")
  expect(out.metadata.tags).toContain("user:opencode")
})

test("falls back to user:universal when setupContext is empty", () => {
  const output = { args: { content: "x" } }
  tagEnforce({ tool: "B12_memory_store", args: {} }, output, PROJ, "")

  expect((output.args.tags as string).split(",")).toContain("user:universal")
})

test("does not duplicate proj: or user: tags already present", () => {
  const out = run("B12_memory_store", {
    content: "x",
    tags: "proj:demo,user:opencode,feat",
  }) as any

  const tagsList = (out.tags as string).split(",")
  const projCount = tagsList.filter((t: string) => t === "proj:demo").length
  const userCount = tagsList.filter((t: string) => t === "user:opencode").length

  expect(projCount).toBe(1)
  expect(userCount).toBe(1)
  expect(tagsList).toContain("feat")
})

test("preserves a user-set user: tag and does not overwrite it", () => {
  const out = run("B12_memory_store", {
    content: "x",
    tags: "user:custom",
  }) as any

  const tagsList = (out.tags as string).split(",")
  expect(tagsList).toContain("user:custom")
  expect(tagsList).not.toContain("user:opencode")
  expect(tagsList).toContain("proj:demo")
})

test("reads tags from metadata.tags as a comma string", () => {
  const out = run("B12_memory_store", {
    content: "x",
    metadata: { tags: "proj:demo,decision,user:opencode" },
  }) as any

  const tagsList = (out.tags as string).split(",")
  expect(tagsList).toContain("decision")
  expect(out.metadata.tags).toEqual(expect.arrayContaining(tagsList))
})

test("reads tags from metadata.tags as an array", () => {
  const out = run("B12_memory_store", {
    content: "x",
    metadata: { tags: ["proj:demo", "learning"] },
  }) as any

  expect((out.tags as string).split(",")).toContain("learning")
})

test("reads tags from args.tags as an array", () => {
  const out = run("B12_memory_store", {
    content: "x",
    tags: ["proj:demo", "architecture"],
  }) as any

  expect((out.tags as string).split(",")).toContain("architecture")
})

test("metadata.tags takes precedence over args.tags", () => {
  const out = run("B12_memory_store", {
    content: "x",
    tags: "only-args-tag",
    metadata: { tags: "only-meta-tag" },
  }) as any

  const tagsList = (out.tags as string).split(",")
  expect(tagsList).toContain("only-meta-tag")
  expect(tagsList).not.toContain("only-args-tag")
})

test("mcp__B12__memory_store removes args.tags (tags live only in metadata)", () => {
  const out = run("mcp__B12__memory_store", { content: "x" }) as any

  expect(out.tags).toBeUndefined()
  // but metadata still carries the injected tags
  expect(out.metadata.tags).toContain("proj:demo")
  expect(out.metadata.tags).toContain("user:opencode")
})

test("B12_memory_store sets args.tags as a comma-joined string", () => {
  const out = run("B12_memory_store", { content: "x" }) as any

  expect(typeof out.tags).toBe("string")
  expect((out.tags as string).split(",")).toContain("proj:demo")
})

test("handles non-object metadata gracefully", () => {
  const out = run("B12_memory_store", {
    content: "x",
    metadata: "not-an-object",
  }) as any

  expect(out.metadata.tags).toContain("proj:demo")
  expect(out.metadata.tags).toContain("user:opencode")
})

test("handles missing args.tags and missing metadata", () => {
  const out = run("B12_memory_store", {}) as any

  expect(out.tags).toBeDefined()
  expect(out.metadata.tags).toContain("proj:demo")
  expect(out.metadata.tags).toContain("user:opencode")
})
