/**
 * Playwright globalSetup: provisions a fresh e2e tenant + API key + project
 * against the running backend. Writes the key into process.env so tests
 * can read it via helpers.ts.
 *
 * Assumes a server is already running on the e2e port (the developer
 * starts it in a separate terminal). If E2E_API_KEY is already set in
 * the environment, skip key generation — useful for repeat runs.
 */
import { execSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const BASE = process.env.E2E_API_BASE ?? "http://127.0.0.1:8002";
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..", "..");
const DATA_DIR = process.env.E2E_DATA_DIR ?? resolve(REPO_ROOT, "mini_cc_data_e2e");

async function globalSetup() {
  let apiKey = process.env.E2E_API_KEY;
  if (!apiKey) {
    try {
      const out = execSync(
        `python -m mini_cc.server keygen e2e ` +
          `--scopes "projects:read" --scopes "projects:write" ` +
          `--scopes "sessions:read" --scopes "sessions:write" ` +
          `--scopes "files:read" --scopes "files:write"`,
        {
          env: { ...process.env, MINI_CC_DATA_DIR: DATA_DIR },
          cwd: REPO_ROOT,
        },
      )
        .toString()
        .trim();
      // keygen prints "mck_...  tenant=...  scopes=...  expires=..." — first token is the key.
      apiKey = out.split(/\s+/)[0];
    } catch (e) {
      throw new Error(
        `failed to generate e2e api key — is the server running with MINI_CC_DATA_DIR=${DATA_DIR}? ` +
          `underlying error: ${(e as Error).message}`,
      );
    }
    if (!apiKey.startsWith("mck_")) {
      throw new Error(`unexpected keygen output: ${apiKey}`);
    }
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

  // Provision a second key with `*` scope for the admin UI spec.
  let adminKey = process.env.E2E_ADMIN_KEY;
  if (!adminKey) {
    try {
      const out = execSync(
        `python -m mini_cc.server keygen e2e --scopes "*" --label e2e-admin`,
        {
          env: { ...process.env, MINI_CC_DATA_DIR: DATA_DIR },
          cwd: REPO_ROOT,
        },
      )
        .toString()
        .trim();
      adminKey = out.split(/\s+/)[0];
    } catch (e) {
      throw new Error(
        `failed to generate e2e admin api key: ${(e as Error).message}`,
      );
    }
    if (!adminKey.startsWith("mck_")) {
      throw new Error(`unexpected keygen output for admin: ${adminKey}`);
    }
  }

  void existsSync;
  process.env.E2E_API_KEY = apiKey;
  process.env.E2E_ADMIN_KEY = adminKey;
  process.env.E2E_API_BASE = BASE;
}

export default globalSetup;

