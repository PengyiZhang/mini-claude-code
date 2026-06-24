/**
 * Smoke test for workflow_render.ts — runnable directly via
 * `node --experimental-strip-types workflow_render.smoke.ts`.
 *
 * Not bundled with the app; a minimal regression net so the algorithm
 * stays sane across edits.
 */
import { workflowToMermaid, workflowToYAML } from "../src/lib/workflow_render.ts";

let failures = 0;
function check(name: string, cond: boolean, extra?: string) {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    console.error(`  ✗ ${name}${extra ? " — " + extra : ""}`);
    failures++;
  }
}

const wf = {
  id: "wf_demo",
  name: "research-and-draft",
  description: "research then draft",
  status: "running",
  current_step: "draft",
  steps: [
    { id: "research", prompt: "Find sources for {topic}." },
    {
      id: "draft",
      prompt: "Draft based on {research}.",
      condition: "research",
      on_failure: "skip",
      max_retries: 0,
    },
    {
      id: "review",
      prompt: "Review the draft.",
      parallel_with: "draft",
    },
  ],
  results: { research: "ok" },
  state: { topic: "AI agents" },
};

const m = workflowToMermaid(wf);
check("mermaid starts with flowchart TD", m.startsWith("flowchart TD"));
check("mermaid lists every step node", m.includes("research[") && m.includes("draft[") && m.includes("review["));
check("mermaid shows done checkmark", m.includes("✓"));
check("mermaid forks parallel_with", m.includes("draft --> review"));
check("mermaid chains linear segments", m.includes("research --> draft"));
check("mermaid defines classDef styles", m.includes("classDef step"));

const y = workflowToYAML(wf);
check("yaml has name", y.includes("name: research-and-draft"));
check("yaml has status", y.includes("status: running"));
check("yaml has steps array", /^steps:/m.test(y) && /^  - id: research$/m.test(y));
check("yaml omits empty max_retries", !/max_retries:/.test(y.split("review")[0]));
// {topic} appears mid-scalar → no quoting needed (emitter only quotes
// leading-special chars, which is the YAML rule).
check("yaml leaves braces-embedded prompt unquoted", y.includes("prompt: Find sources for {topic}."));
check("yaml includes results section", /^results:/m.test(y) && /^  research:/m.test(y));
check("yaml includes state section", /^state:/m.test(y) && /topic:/.test(y));

// Empty workflow edge case
const empty = workflowToMermaid({ id: "x", name: "empty" });
check("empty workflow mermaid renders placeholder", empty.includes("no steps") || empty.includes("flowchart"));

// Quoting edge cases
const tricky = workflowToYAML({
  id: "x",
  name: "has: colon",
  steps: [{ id: "s1", prompt: 'quote " here' }],
});
check("yaml quotes name with colon", /name: "has: colon"/.test(tricky));
// Embedded " in a bare scalar is valid YAML — emitter leaves it alone.
check("yaml leaves embedded quote bare", /prompt: quote " here/.test(tricky));

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
