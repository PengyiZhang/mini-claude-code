/**
 * Minimal line-level diff utility — GitHub-style unified diff output.
 *
 * Used by MessageBubble to render edit_file/write_file tool calls as
 * visual diffs. Implements LCS-based diff (O(n*m) DP); adequate for
 * file-sized snippets, not whole-file large binaries.
 *
 * Smoke-tested via smoke/diff.smoke.ts (run with
 *   node --experimental-strip-types mini_cc/web/smoke/diff.smoke.ts).
 */

export type DiffOp = "equal" | "add" | "del";

export interface DiffLine {
  op: DiffOp;
  text: string;
}

/**
 * Compute line-level diff between two strings. Returns one DiffLine per
 * input line, in source order. 'add' = new line, 'del' = removed line,
 * 'equal' = unchanged.
 */
export function lineDiff(oldText: string, newText: string): DiffLine[] {
  const a = (oldText ?? "").split("\n");
  const b = (newText ?? "").split("\n");
  const n = a.length, m = b.length;
  // dp[i][j] = LCS length of a[i:] and b[j:]
  const dp: number[][] = Array.from({ length: n + 1 },
    () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) dp[i][j] = dp[i + 1][j + 1] + 1;
      else dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ op: "equal", text: a[i] });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ op: "del", text: a[i] });
      i++;
    } else {
      out.push({ op: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) { out.push({ op: "del", text: a[i++] }); }
  while (j < m) { out.push({ op: "add", text: b[j++] }); }
  return out;
}

/** Render DiffLine[] as a unified-diff string with +/-/space prefixes. */
export function formatUnifiedDiff(oldText: string, newText: string): string {
  return lineDiff(oldText, newText)
    .map(l => (l.op === "add" ? "+ " : l.op === "del" ? "- " : "  ") + l.text)
    .join("\n");
}

/** Stats: how many add/del lines. Useful for the diff header. */
export function diffStats(oldText: string, newText: string): {
  adds: number; dels: number;
} {
  let adds = 0, dels = 0;
  for (const l of lineDiff(oldText, newText)) {
    if (l.op === "add") adds++;
    else if (l.op === "del") dels++;
  }
  return { adds, dels };
}
