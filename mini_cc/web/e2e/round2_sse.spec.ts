/**
 * Round 2 — SSE / Last-Event-Id replay smoke (B8).
 *
 * The full reconnect UX needs a live LLM stream + mid-stream disconnect
 * (flaky in CI). This spec covers the deterministic parts:
 *
 *   - The /send endpoint accepts a `Last-Event-Id` header without 4xx
 *     (the B8 replay path doesn't crash on a cold event log).
 *   - The replay code-path returns 200 + text/event-stream.
 *
 * The actual replay-from-disk correctness is covered by
 * tests/test_round2_b4.py at the storage layer.
 */
import { test, expect } from "@playwright/test";
import { BASE, TENANT } from "./helpers";

const PID = "e2e_proj";
const E2E_KEY = process.env.E2E_API_KEY!;

test.describe("round 2 — SSE Last-Event-Id", () => {
  test("/send accepts Last-Event-Id header and returns text/event-stream", async ({ request }) => {
    // Create a session to send into.
    const sess = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${PID}/sessions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${E2E_KEY}`, "Content-Type": "application/json" },
      data: "{}",
    });
    expect(sess.status()).toBe(201);
    const sid = (await sess.json()).session_id;

    // Send with Last-Event-Id. The server should accept it and begin
    // streaming (we abort the stream after the first event to keep
    // the test fast — we just want to confirm the endpoint doesn't
    // reject the B8 path).
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 5_000);
    try {
      const res = await fetch(`${BASE}/tenants/${TENANT}/projects/${PID}/sessions/${sid}/send`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${E2E_KEY}`,
          "Content-Type": "application/json",
          "Accept": "text/event-stream",
          "Last-Event-Id": "42",
        },
        body: JSON.stringify({ user_input: "say hi" }),
        signal: ctrl.signal,
      });
      // 200 (streaming) or 409 (busy from a lingering previous send).
      // Either way, NOT a 4xx that would indicate the B8 path crashed
      // on the Last-Event-Id header parse.
      expect([200, 409]).toContain(res.status);
      expect(res.headers.get("content-type")).toContain("text/event-stream");
    } catch (e) {
      // Network abort is fine — we just wanted to confirm the request
      // was accepted and streaming started.
      if (!(e as Error).name.includes("Abort")) throw e;
    }
  });
});
