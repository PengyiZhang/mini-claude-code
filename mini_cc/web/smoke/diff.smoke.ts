/**
 * Smoke test for diff.ts — runnable directly via
 *   node --experimental-strip-types mini_cc/web/smoke/diff.smoke.ts
 */
import { lineDiff, formatUnifiedDiff, diffStats } from "../src/lib/diff.ts";

let failures = 0;
function check(name: string, cond: boolean, extra?: string) {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.error(`  ✗ ${name}${extra ? " — " + extra : ""}`); failures++; }
}

// identical → all equal
{
  const out = lineDiff("a\nb\nc", "a\nb\nc");
  check("identical → all equal", out.every(l => l.op === "equal"));
  check("identical → 3 lines", out.length === 3);
}

// pure append
{
  const out = lineDiff("a\nb", "a\nb\nc");
  check("append → 2 equal + 1 add",
    out.length === 3 && out[2].op === "add" && out[2].text === "c");
}

// pure delete
{
  const out = lineDiff("a\nb\nc", "a\nc");
  check("delete middle → del b",
    out.some(l => l.op === "del" && l.text === "b"));
}

// replace
{
  const out = lineDiff("foo", "bar");
  check("replace → 1 del + 1 add",
    out.length === 2 && out[0].op === "del" && out[1].op === "add");
}

// unified format
{
  const s = formatUnifiedDiff("x\ny", "x\nz");
  check("unified keeps common line",
    s.includes("  x"));
  check("unified marks del with -",
    s.includes("- y"));
  check("unified marks add with +",
    s.includes("+ z"));
}

// stats
{
  const s = diffStats("a\nb\nc", "a\nX\nc\nD");
  check("stats counts adds/dels",
    s.adds === 2 && s.dels === 1);
}

// empty inputs
{
  check("empty/empty → empty",
    lineDiff("", "").length === 1);  // [""] one empty line
  check("from empty → all add",
    lineDiff("", "new").filter(l => l.op === "add").length === 1);
}

if (failures === 0) console.log("\nAll diff smoke tests passed.");
else { console.error(`\n${failures} diff smoke test failures.`); process.exit(1); }
