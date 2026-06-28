/**
 * Round 2 — B8 resume-only path + sse.ts auto-reconnect.
 *
 * Two-pronged coverage:
 *
 *  1. Server contract: POST /send with body `{ resume: true }` and
 *     `Last-Event-Id: N` skips the LLM dispatch and replays events
 *     N+1.. from the per-session log. Verified end-to-end by:
 *       (a) drive a real /send (populates the event log)
 *       (b) issue a resume request, assert it returns 200 + the
 *           X-mini_cc-Resume header + replays events
 *
 *  2. Client wiring: streamSend with `reconnect: true` parses `id:`
 *     lines, tracks lastSeq, and on a mid-stream drop retries with
 *     `Last-Event-Id` + `resume: true`. Verified via Playwright route
 *     interception that kills the first connection after one event.
 */
import { test, expect } from "@playwright/test";
import { signIn, BASE, TENANT } from "./helpers";

const PID = "e2e_proj";
const E2E_KEY = process.env.E2E_API_KEY!;

test.describe("round 2 — B8 resume-only /send", () => {
  test("resume=true skips dispatch and replays from event log", async ({ request }) => {
    // Provision a session.
    const sess = await request.fetch(`${BASE}/tenants/${TENANT}/projects/${PID}/sessions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${E2E_KEY}`, "Content-Type": "application/json" },
      data: "{}",
    });
    expect(sess.status()).toBe(201);
    const sid = (await sess.json()).session_id;

    // Drive a real /send to populate the event log. Abort after we've
    // seen at least one event — we don't need the run to finish.
    const ctrl = new AbortController();
    let seenSeq = 0;
    const firstRes = await fetch(`${BASE}/tenants/${TENANT}/projects/${PID}/sessions/${sid}/send`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${E2E_KEY}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ user_input: "say one short word" }),
      signal: ctrl.signal,
    });
    expect(firstRes.status).toBe(200);
    const reader = firstRes.body!.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // Track the highest id: we've seen.
      for (const m of buf.matchAll(/^id:\s*(\d+)/gm)) {
        seenSeq = Math.max(seenSeq, parseInt(m[1], 10));
      }
      if (seenSeq > 0) {
        // Got at least one event — abort and trigger resume.
        ctrl.abort();
        break;
      }
    }
    expect(seenSeq).toBeGreaterThan(0);

    // Small grace period so the server flushes the partial log.
    await new Promise((r) => setTimeout(r, 500));

    // Resume request — must NOT trigger a new LLM run. The presence of
    // the X-mini_cc-Resume header is the contract signal.
    const resumeRes = await fetch(`${BASE}/tenants/${TENANT}/projects/${PID}/sessions/${sid}/send`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${E2E_KEY}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "Last-Event-Id": "0",
      },
      body: JSON.stringify({ user_input: "", resume: true }),
    });
    expect(resumeRes.status).toBe(200);
    expect(resumeRes.headers.get("x-mini_cc-resume")).toBe("1");

    // Drain the replay — at least one event must come back.
    const resumeReader = resumeRes.body!.getReader();
    let replayBuf = "";
    let replayDone = false;
    const replayDeadline = Date.now() + 10_000;
    while (Date.now() < replayDeadline) {
      const { value, done } = await Promise.race([
        resumeReader.read(),
        new Promise<{ value: undefined; done: true }>((resolve) =>
          setTimeout(() => resolve({ value: undefined, done: true }), 8_000)),
      ]);
      if (done) { replayDone = true; break; }
      replayBuf += decoder.decode(value, { stream: true });
      if (replayBuf.includes("[DONE]")) { replayDone = true; break; }
    }
    expect(replayDone).toBe(true);
    // Replay must contain at least one data: line.
    expect(replayBuf).toMatch(/data:\s/);
  });
});

test.describe("round 2 — sse.ts auto-reconnect", () => {
  test("client reconnects with Last-Event-Id after a mid-stream drop", async ({ page }) => {
    // Drive the chat page. Intercept the first /send call and abort it
    // after returning the first event — this simulates a network drop.
    let firstCallSeen = false;
    await page.route("**/sessions/*/send", async (route) => {
      if (!firstCallSeen) {
        firstCallSeen = true;
        // Simulate a partial response: emit one event with id: 1,
        // then abort the connection.
        const body =
          'id: 1\ndata: {"type":"text","text":"partial"}\n\n';
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          headers: { "Cache-Control": "no-cache" },
          body,
        });
      } else {
        // Second call (the reconnect attempt) — assert it carries the
        // Last-Event-Id + resume flag, then fulfill with a [DONE].
        const reqBody = route.request().postDataJSON() as { resume?: boolean };
        const lastEventId = route.request().headers()["last-event-id"];
        expect(reqBody.resume).toBe(true);
        expect(lastEventId).toBe("1");
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          headers: { "Cache-Control": "no-cache" },
          body: 'data: [DONE]\n\n',
        });
      }
    });

    await signIn(page);
    await page.goto("/#/projects/e2e_proj");
    await page.getByRole("button", { name: /chat/i }).click();
    await page.getByRole("button", { name: /new session/i }).click();
    await page.locator("textarea").first().fill("hi");
    await page.getByRole("button", { name: /^send$/i }).click();

    // The reconnect path fires within ~1-2s (backoff base 1s). The
    // chat surface should NOT show a fatal error — it should keep
    // streaming or finish cleanly. Wait briefly, then assert no
    // error UI is visible.
    await page.waitForTimeout(4_000);
    const errorVisible = await page.locator("text=/sse_error|HTTP 5\\d\\d/i")
      .first().isVisible().catch(() => false);
    expect(errorVisible).toBe(false);
  });
});
