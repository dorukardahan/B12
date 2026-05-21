import { expect, mock, test } from "bun:test"

test("published dist entrypoint loads", async () => {
  mock.module("better-sqlite3", () => ({
    default: class MockDatabase {},
  }))

  const mod = await import("../dist/index.js")

  expect(typeof mod.default).toBe("function")
})
