/**
 * Playwright globalSetup: provisions a fresh e2e tenant + API key + project
 * against the running backend. Writes the key into process.env so tests
 * can read it via helpers.ts.
 *
 * Assumes a server is already running on the e2e port (the developer
 * starts it in a separate terminal). If E2E_API_KEY is already set in
 * the environment, skip key generation — useful for repeat runs.
 *
 * Data-dir robustness: the running server may have been started from
 * any cwd, so its data dir is one of several plausible locations. We
 * write the new key to ALL of them so whichever one the server uses
 * picks it up. (Idempotent — keygen just appends a new row to keys.json.)
 */
import { execSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const BASE = process.env.E2E_API_BASE ?? "http://127.0.0.1:8002";
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..", "..");

// Plausible data dirs the running server might be using. Order matters
// only for E2E_DATA_DIR defaulting (we surface the first existing one).
const CANDIDATE_DATA_DIRS = [
  process.env.E2E_DATA_DIR,
  resolve(REPO_ROOT, "mini_cc_data_e2e"),
  resolve(REPO_ROOT, "mini_cc", "web", "mini_cc_data_e2e"),
  resolve(REPO_ROOT, "mini_cc_data"),
  resolve(REPO_ROOT, "mini_cc", "web", "mini_cc_data"),
].filter((d): d is string => Boolean(d));

function keygen(opts: {
  label: string;
  scopes: string[];
}): string {
  const scopeArgs = opts.scopes.map((s) => `--scopes "${s}"`).join(" ");
  // Pick a probe endpoint the new key is actually allowed to hit:
  // admin:* keys hit admin/keys, project/session/file keys hit /projects,
  // * keys hit either. /health is always 200 but doesn't validate the
  // key, so we don't use it.
  const isAdmin = opts.scopes.some((s) => s.startsWith("admin:") || s === "*");
  const probePath = isAdmin
    ? `${BASE}/tenants/e2e/admin/keys`
    : `${BASE}/tenants/e2e/projects`;
  // Try each candidate data dir; first one whose new key actually
  // authenticates against the running server wins. This catches the
  // common dev mistake of starting the server from a different cwd
  // than the test runner.
  for (const dir of CANDIDATE_DATA_DIRS) {
    try {
      const out = execSync(
        `python -m mini_cc.server keygen e2e ${scopeArgs} ` +
          `--label "${opts.label}"`,
        {
          env: { ...process.env, MINI_CC_DATA_DIR: dir },
          cwd: REPO_ROOT,
          stdio: ["pipe", "pipe", "pipe"],
        },
      )
        .toString()
        .trim();
      const key = out.split(/\s+/)[0];
      if (!key.startsWith("mck_")) continue;
      // Verify the key actually works against the running server.
      // NB: on Windows, `curl -o /dev/null -w "%{http_code}"` exits
      // non-zero even on HTTP 200 (a write-error quirk), so we can't
      // rely on execSync's exit code. Use -w alone and ignore body.
      let probe = "";
      try {
        probe = execSync(
          `curl -sS -w "%{http_code}" ` +
            `${probePath} ` +
            `-H "Authorization: Bearer ${key}"`,
          { stdio: ["pipe", "pipe", "pipe"] },
        ).toString().trim();
      } catch {
        // curl may exit non-zero on Windows even for 200; rely on the
        // captured stdout instead.
      }
      // Probe ends with the http_code we asked for via -w.
      if (probe.endsWith('"200"') || probe.endsWith("200")) return key;
    } catch {
      // try next dir
    }
  }
  throw new Error(
    `keygen ${opts.label}: could not produce a key that authenticates ` +
      `against ${BASE}. Tried data dirs: ${CANDIDATE_DATA_DIRS.join(", ")}. ` +
      `Is the server running? Set E2E_DATA_DIR explicitly to override.`,
  );
}

async function globalSetup() {
  let apiKey = process.env.E2E_API_KEY;
  if (!apiKey) {
    apiKey = keygen({
      label: "e2e",
      scopes: [
        "projects:read", "projects:write",
        "sessions:read", "sessions:write",
        "files:read", "files:write",
      ],
    });
  }
  if (!apiKey.startsWith("mck_")) {
    throw new Error(`unexpected api key: ${apiKey}`);
  }

  // Create the e2e project (ignore errors — it may already exist).
  try {
    execSync(
      `curl -sS -X POST ${BASE}/tenants/e2e/projects ` +
        `-H "Authorization: Bearer ${apiKey}" ` +
        `-H "Content-Type: application/json" ` +
        `-d '{"project_id":"e2e_proj","display_name":"e2e"}'`,
      { stdio: "pipe" },
    );
  } catch {
    /* ignore */
  }

  // Provision an admin-scoped key for the admin UI spec.
  let adminKey = process.env.E2E_ADMIN_KEY;
  if (!adminKey) {
    adminKey = keygen({
      label: "e2e-admin",
      scopes: ["admin:read", "admin:write"],
    });
  }

  void existsSync;
  process.env.E2E_API_KEY = apiKey;
  process.env.E2E_ADMIN_KEY = adminKey;
  process.env.E2E_API_BASE = BASE;
}

export default globalSetup;

