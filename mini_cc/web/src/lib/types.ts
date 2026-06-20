export interface TenantProfile {
  apiKey: string;
  tenantId: string;
  label: string;
  baseUrl: string;
}

export interface ProjectOut {
  project_id: string;
  tenant_id: string;
  display_name: string;
  created_at: string;
}

export interface TreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified: number;
}

export interface FileContent {
  path: string;
  name: string;
  size: number;
  modified: number;
  is_text: boolean;
  truncated: boolean;
  content: string | null;
}

export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}

export type SendEvent =
  | { type: "text"; text: string }
  | { type: "tool_use"; name: string; input: Record<string, unknown>; id: string }
  | { type: "tool_result"; tool_use_id: string; content: string }
  | { type: "done" }
  | { type: "error"; message: string }
  | { type: "max_tokens_escalation"; max_tokens: number }
  | { type: "cron_fired"; job_id: string; prompt: string }
  | { type: "background_notification" };
