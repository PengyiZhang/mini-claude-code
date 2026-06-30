import { useEffect, useRef } from "react";
import { useChat } from "../../lib/store";
import { runCommandForCard } from "../../lib/commands";
import { useAuth } from "../../lib/store";
import type { CardEvent } from "../../lib/types";

/**
 * Live-refresh hook: when a card carries `refresh_command` and
 * `refresh_interval_ms`, re-run that slash command on a timer and
 * replace the card in-place via `replaceCardEverywhere` (not addCard,
 * which would stack new assistant bubbles). Stops polling when the
 * card stops asking for refresh (e.g. /bg returns all-stopped) or the
 * component unmounts.
 *
 * The poll is best-effort: any fetch error is logged and ignored
 * (next tick tries again). Abort on unmount so a pending request
 * doesn't try to setState after the card is gone.
 */
export function useLiveRefresh(
  card: CardEvent,
  chatKey: string | undefined,
): void {
  const cmd = card.refresh_command;
  const intervalMs = card.refresh_interval_ms ?? 0;
  const profile = useAuth((s) => s.profiles.find(
    (p) => p.apiKey === useAuth.getState().activeApiKey) ?? null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!cmd || !intervalMs || !chatKey || !profile) return;
    const [name, ...rest] = cmd.replace(/^\//, "").split(/\s+/);
    const args = rest.join(" ");
    const [pid, sid] = chatKey.split("::");

    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const fresh = await runCommandForCard(profile, pid, sid, name, args);
        if (fresh && !cancelled) {
          useChat.getState().replaceCardEverywhere(chatKey, card.id, fresh);
        }
      } catch {
        // Swallow — a transient 5xx or network blip shouldn't break
        // the polling loop. Next tick will retry.
      }
    };

    const id = window.setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      abortRef.current?.abort();
    };
    // Re-arm when the card's refresh hint changes (e.g. /bg flips from
    // running → all-stopped and clears refresh_command).
  }, [cmd, intervalMs, chatKey, profile, card.id]);
}
