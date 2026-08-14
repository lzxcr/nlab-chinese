import assert from "node:assert/strict"
import { test } from "node:test"
import { normalizeBaseUrl } from "./siteUrl"

test("normalizes full canonical URLs to Quartz baseUrl form", () => {
  assert.equal(normalizeBaseUrl("https://nlab.example.org/"), "nlab.example.org")
  assert.equal(normalizeBaseUrl("http://localhost:8080/docs/"), "localhost:8080/docs")
})

test("rejects empty and credential-bearing canonical URLs", () => {
  assert.throws(() => normalizeBaseUrl("  "))
  assert.throws(() => normalizeBaseUrl("https://user:secret@example.org"))
})
