import { expect, test } from "bun:test"
import { messageRetrieval, shouldAttemptMessageRetrieval } from "../src/hooks/message-retrieval"

test("messageRetrieval skips greetings, slash commands, and exact short commands", async () => {
  let searches = 0
  const db = {
    search: () => {
      searches++
      return [{ id: 1, display: "[fact] should not appear", score: 0.9 }]
    },
    filterSearchResultsByTags: (rows: any[]) => rows,
    boostStrength: () => {},
    logFeedback: () => {},
  }

  expect(shouldAttemptMessageRetrieval("hi")).toBe(false)
  expect(shouldAttemptMessageRetrieval("/help")).toBe(false)
  expect(await messageRetrieval("ok", "demo", db as any)).toBe("")
  expect(searches).toBe(0)
})

test("messageRetrieval returns project-scoped memories for real prompts", async () => {
  let boosted: number[] = []
  const db = {
    search: () => [{ id: 2, display: "[lesson] proj:demo auth bug fix", score: 0.95 }],
    filterSearchResultsByTags: (rows: any[]) => rows,
    boostStrength: (ids: number[]) => {
      boosted = ids
    },
    logFeedback: () => {},
  }

  const context = await messageRetrieval("auth bug", "demo", db as any)

  expect(context).toContain("auth bug fix")
  expect(boosted).toEqual([2])
})

test("messageRetrieval filters semantic candidates after a larger project-aware pool", async () => {
  let requestedLimit = 0
  const semanticClient = {
    health: async () => ({ alive: true }),
    semanticSearch: async (_query: string, _dbPath: string, limit: number) => {
      requestedLimit = limit
      return [
        { id: 1, display: "[fact] proj:other auth memory 1", score: 0.99 },
        { id: 2, display: "[fact] proj:other auth memory 2", score: 0.98 },
        { id: 3, display: "[fact] proj:other auth memory 3", score: 0.97 },
        { id: 4, display: "[fact] proj:other auth memory 4", score: 0.96 },
        { id: 5, display: "[fact] proj:other auth memory 5", score: 0.95 },
        { id: 6, display: "[fact] proj:demo scoped auth memory", score: 0.94 },
      ].slice(0, limit)
    },
    rerank: async () => [],
  }
  const db = {
    search: () => [],
    filterSearchResultsByTags: (rows: any[]) =>
      rows.filter((row) => row.display.includes("proj:demo")),
    boostStrength: () => {},
    logFeedback: () => {},
  }

  const context = await messageRetrieval("auth memory", "demo", db as any, semanticClient)

  expect(requestedLimit).toBeGreaterThan(5)
  expect(context).toContain("scoped auth memory")
})
