import { useCallback, useEffect, useState } from "react";
import type { ChannelOut, SessionMeta, TenantProfile } from "../lib/types";
import {
  ApiError,
  channelWebhookUrl,
  createChannel,
  deleteChannel,
  listChannels,
  listSessionMetas,
} from "../lib/api";

/**
 * Sidebar "channels" panel — manages bidirectional IM channel bindings
 * (Feishu today, Slack/Discord tomorrow). Backend lives in
 * mini_cc/channels/ + server/routes/channels.py.
 *
 * The core UX is the webhook URL: when a user registers a Feishu app,
 * they need the URL to paste into Feishu's "事件订阅" console. So each
 * binding row features a copy button right next to the URL.
 *
 * Write frequency here is near-zero (one-time setup), so we don't poll —
 * manual refresh + auto-refresh after create/delete is enough.
 */

type Kind = "feishu";
type Transport = "ws" | "webhook";

interface FieldDef {
  name: string;
  label: string;
  required?: boolean;
  secret?: boolean;
  placeholder?: string;
  hint?: string;
}

// Kind → field schema. Adding a new kind is just one entry here + the
// backend register_channel_kind call. Drives both the create form and
// the list-row config display.
const KIND_CONFIG: Record<Kind, {
  label: string;
  badgeTone: string;
  fields: FieldDef[];
  supported_transports: Transport[];
}> = {
  feishu: {
    label: "飞书 / Feishu",
    badgeTone: "bg-emerald-500/20 text-emerald-300 border-emerald-500/40",
    supported_transports: ["ws", "webhook"],
    fields: [
      { name: "app_id", label: "App ID", required: true, placeholder: "cli_xxx" },
      { name: "app_secret", label: "App Secret", required: true, secret: true },
      { name: "encrypt_key", label: "Encrypt Key", secret: true, hint: "可选 — 来自飞书「事件订阅」页（仅 webhook 模式需要）" },
      { name: "verification_token", label: "Verification Token", hint: "可选 — 来自飞书「事件订阅」页（仅 webhook 模式需要）" },
      { name: "chat_id", label: "Chat ID", hint: "可选，留空 = inbound-only（不推送）" },
    ],
  },
};

const TRANSPORT_TONES: Record<Transport, string> = {
  ws: "bg-sky-500/20 text-sky-300 border-sky-500/40",
  webhook: "bg-violet-500/20 text-violet-300 border-violet-500/40",
};

const ALL_EVENT_TYPES = [
  // assistant_message = one per assistant turn, fired via on_event. This
  // is what channel dispatchers route to IM push (Feishu reply etc.) —
  // must be on by default or the chat stays silent even though the
  // session transcript captures the reply.
  { key: "assistant_message", label: "assistant_message — 一轮 agent 回复（推送 IM 用）" },
  { key: "text", label: "text — agent 流式文本（SSE delta）" },
  { key: "teammate_message", label: "teammate_message — teammate 间消息" },
  { key: "lead_nudged", label: "lead_nudged — watcher 触发 lead turn" },
  { key: "tool_result", label: "tool_result — 工具执行结果" },
];

const DEFAULT_EVENT_TYPES = [
  "assistant_message",
  "text",
  "teammate_message",
  "lead_nudged",
];

export default function ChannelsPanel({
  profile,
  pid,
}: {
  profile: TenantProfile;
  pid: string;
}) {
  const [items, setItems] = useState<ChannelOut[]>([]);
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showModify, setShowModify] = useState(false);
  const [modifyBinding, setModifyBinding] = useState<ChannelOut | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const [cs, ss] = await Promise.all([
        listChannels(profile, pid),
        listSessionMetas(profile, pid).catch(() => [] as SessionMeta[]),
      ]);
      setItems(cs);
      setSessions(ss);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [profile, pid]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleDelete(b: ChannelOut) {
    if (!confirm(`delete channel ${b.id}?`)) return;
    try {
      await deleteChannel(profile, pid, b.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function handleModify(b: ChannelOut) {
    setModifyBinding(b);
    setShowModify(true);
  }

  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs text-ink-dim uppercase tracking-wide">channels</span>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setShowCreate(true)}
            className="text-xs px-2 py-0.5 rounded border border-border hover:border-accent"
            title="register a new channel"
          >
            ＋ new
          </button>
          <button
            onClick={() => void refresh()}
            disabled={loading}
            className="text-xs px-2 py-0.5 rounded border border-border hover:border-accent disabled:opacity-50"
          >
            {loading ? "…" : "↻"}
          </button>
        </div>
      </div>

      {err && (
        <div className="text-xs text-err bg-err/10 border border-err/40 rounded px-2 py-1">
          {err}
        </div>
      )}

      {items.length === 0 ? (
        <div className="text-xs text-ink-faint italic">(no channels)</div>
      ) : (
        <div className="space-y-2">
          {items.map((b) => (
            <ChannelRow key={b.id} binding={b} onDelete={() => void handleDelete(b)} onModify={() => void handleModify(b)} />
          ))}
        </div>
      )}

      {showCreate && (
        <CreateModal
          sessions={sessions}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            void refresh();
          }}
          onError={(e) => setErr(e)}
          onSubmit={async (vals) => {
            await createChannel(profile, pid, vals);
          }}
          kinds={Object.fromEntries(
            Object.entries(KIND_CONFIG).map(([k, v]) => [k, v.supported_transports]),
          ) as Record<Kind, Transport[]>}
        />
      )}
      {showModify && modifyBinding && (
        <ModifyModal
          sessions={sessions}
          binding={modifyBinding}
          onClose={() => {
            setShowModify(false);
            setModifyBinding(null);
          }}
          onModified={() => {
            setShowModify(false);
            setModifyBinding(null);
            void refresh();
          }}
          onError={(e) => setErr(e)}
          onSubmit={async (vals) => {
            await createChannel(profile, pid, vals);
          }}
          kinds={Object.fromEntries(
            Object.entries(KIND_CONFIG).map(([k, v]) => [k, v.supported_transports]),
          ) as Record<Kind, Transport[]>}
        />
      )}  

    </div>
  );
}

function ChannelRow({
  binding,
  onDelete,
  onModify,
}: {
  binding: ChannelOut;
  onDelete: () => void;
  onModify: () => void;
}) {
  const kind = binding.kind as Kind;
  const enable = binding.enable;
  const kindMeta = KIND_CONFIG[kind];
  const [copied, setCopied] = useState(false);
  const webhookUrl = channelWebhookUrl(binding.id);
  // ws-mode bindings don't need the public webhook URL — they hold an
  // outbound long connection. The URL still works (inbound webhook is
  // a no-op + url_verification echo), but copy-to-clipboard is only
  // useful for webhook-mode bindings.
  const transport = binding.transport ?? "webhook";

  async function copy() {
    try {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Older browsers / insecure context — fallback to prompt so the
      // user can still copy manually.
      window.prompt("copy webhook url", webhookUrl);
    }
  }

  const chatId = binding.config.chat_id;
  const sessionBound = binding.session_id;

  return (
    <div className="bg-bg-card border border-border rounded p-2 space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded border ${kindMeta?.badgeTone ?? ""}`}
        >
          {binding.kind}
        </span>
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded border ${TRANSPORT_TONES[transport]}`}
          title={transport === "ws"
            ? "WebSocket 长连接 — 无需公网 URL"
            : "Webhook — 飞书 POST 到公网 URL"}
        >
          {transport}
        </span>

        <span
          className={`text-[10px] px-1.5 py-0.5 rounded border ${TRANSPORT_TONES[transport]}`}
        >
          {enable === "true" ? "enabled" : "disabled"}
        </span>
        <span className="font-mono text-xs text-ink-dim truncate">{binding.id}</span>

        <button
          onClick={onModify}
          className="ml-auto text-xs text-ink-faint hover:text-err"
          title="modify"
        >
          ✍
        </button>

        <button
          onClick={onDelete}
          className="ml-auto text-xs text-ink-faint hover:text-err"
          title="delete"
        >
          ✕
        </button>
      </div>

      {transport === "ws" ? (
        <div className="text-[11px] text-ink-faint italic">
          ws long connection — no public URL needed
        </div>
      ) : (
        <div className="flex items-center gap-1 text-xs">
          <span className="text-ink-dim shrink-0">webhook:</span>
          <code className="text-[11px] text-ink-dim truncate flex-1" title={webhookUrl}>
            {webhookUrl}
          </code>
          <button
            onClick={copy}
            className="px-1.5 py-0.5 rounded border border-border hover:border-accent shrink-0"
            title="copy webhook url"
          >
            {copied ? "✓" : "📋"}
          </button>
        </div>
      )}

      {chatId ? (
        <div className="text-[11px] text-ink-faint">
          target chat_id: <code className="text-ink-dim">{String(chatId)}</code>
        </div>
      ) : (
        <div className="text-[11px] text-ink-faint italic">inbound-only (no chat_id)</div>
      )}

      {sessionBound && (
        <div className="text-[11px] text-ink-faint">
          session: <code className="text-ink-dim">{sessionBound}</code>
        </div>
      )}

      {binding.event_types.length > 0 && (
        <div className="text-[11px] text-ink-faint">
          events: {binding.event_types.join(", ")}
        </div>
      )}

      <div className="text-[10px] text-ink-faint">
        created {fmtIso(binding.created_at)}
      </div>
    </div>
  );
}

function CreateModal({
  sessions,
  onClose,
  onCreated,
  onError,
  onSubmit,
  kinds,
}: {
  sessions: SessionMeta[];
  onClose: () => void;
  onCreated: () => void;
  onError: (msg: string) => void;
  onSubmit: (vals: {
    kind: string;
    enable: string;
    transport: Transport;
    config: Record<string, unknown>;
    session_id: string | null;
    event_types: string[];
  }) => Promise<void>;
  /** kind → supported transports. Drives the Transport radio + auto-fallback
   *  when a kind can't carry the currently-selected transport. */
  kinds: Record<Kind, Transport[]>;
}) {
  const [kind, setKind] = useState<Kind>("feishu");
  const [enable, setEnable] = useState<string>("true");
  const [transport, setTransport] = useState<Transport>("ws");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [sessionId, setSessionId] = useState<string>("");
  const [eventTypes, setEventTypes] = useState<string[]>(DEFAULT_EVENT_TYPES);
  const [busy, setBusy] = useState(false);

  const meta = KIND_CONFIG[kind];

  // If the user switches Kind to one that can't carry the current transport,
  // snap to the kind's first supported transport rather than letting the
  // submitted value silently mismatch what the backend will accept.
  function changeKind(next: Kind) {
    setKind(next);
    const sup = kinds[next] ?? ["webhook"];
    if (!sup.includes(transport)) {
      setTransport(sup[0]);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    // Validate required fields up-front so the user gets a clear message
    // instead of a 400 from the backend.
    for (const f of meta.fields) {
      if (f.required && !(config[f.name] ?? "").trim()) {
        onError(`${f.label} is required`);
        return;
      }
    }
    setBusy(true);
    try {
      const cfg: Record<string, unknown> = {};
      for (const f of meta.fields) {
        const v = (config[f.name] ?? "").trim();
        if (v) cfg[f.name] = v;
      }
      await onSubmit({
        kind,
        enable,
        transport,
        config: cfg,
        session_id: sessionId.trim() || null,
        event_types: eventTypes,
      });
      onCreated();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggleEvent(key: string) {
    setEventTypes((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
      <form
        onSubmit={submit}
        className="bg-bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4 max-h-[90vh] overflow-y-auto"
      >
        <div className="text-lg font-semibold">new channel</div>

        <Field label="Kind">
          <select
            value={kind}
            onChange={(e) => {
              changeKind(e.target.value as Kind);
              setConfig({});
            }}
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
          >
            {Object.entries(KIND_CONFIG).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Enable">
          <select
            value={enable}
            onChange={(e) => {
              setEnable(e.target.value);
            }}
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
          >
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </Field>

        <Field label="Transport">
          <div className="space-y-1">
            {(kinds[kind] ?? ["webhook"]).map((t) => (
              <label key={t} className="flex items-start gap-2 text-xs">
                <input
                  type="radio"
                  name="channel-transport"
                  value={t}
                  checked={transport === t}
                  onChange={() => setTransport(t)}
                  className="mt-0.5"
                />
                <span className="text-ink-dim">
                  <span className="text-ink">{t}</span>
                  {t === "ws" && <span className="text-ink-faint"> — 推荐，长连接，无需公网 URL</span>}
                  {t === "webhook" && <span className="text-ink-faint"> — 事件订阅 URL（需公网可达）</span>}
                </span>
              </label>
            ))}
            {transport === "webhook" && (
              <div className="text-[11px] text-ink-faint pl-5">
                注册后请把 webhook URL 填到飞书「事件订阅」页
              </div>
            )}
          </div>
        </Field>

        <div className="space-y-3">
          {meta.fields.map((f) => (
            <Field key={f.name} label={f.label + (f.required ? " *" : "")}>
              <input
                type={f.secret ? "password" : "text"}
                value={config[f.name] ?? ""}
                onChange={(e) => setConfig({ ...config, [f.name]: e.target.value })}
                placeholder={f.placeholder}
                className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono text-sm"
              />
              {f.hint && <div className="text-[11px] text-ink-faint">{f.hint}</div>}
            </Field>
          ))}
        </div>

        <Field label="Session (optional — blank = project default)">
          <select
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
          >
            <option value="">(project default)</option>
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Outbound event types">
          <div className="space-y-1">
            {ALL_EVENT_TYPES.map((ev) => (
              <label key={ev.key} className="flex items-start gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={eventTypes.includes(ev.key)}
                  onChange={() => toggleEvent(ev.key)}
                  className="mt-0.5"
                />
                <span className="text-ink-dim">{ev.label}</span>
              </label>
            ))}
          </div>
        </Field>

        <div className="text-[11px] text-ink-faint">
          注册后立刻能看到 webhook URL，把它填到飞书「事件订阅」里。
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 border border-border rounded hover:bg-bg-hover"
          >
            cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-3 py-1.5 bg-accent text-white rounded disabled:opacity-50"
          >
            {busy ? "…" : "create"}
          </button>
        </div>
      </form>
    </div>
  );
}

function ModifyModal({
  sessions,
  onClose,
  onModified,
  onError,
  onSubmit,
  kinds,
  binding,
}: {
  sessions: SessionMeta[];
  onClose: () => void;
  onModified: () => void;
  onError: (msg: string) => void;
  onSubmit: (vals: {
    id: string;
    kind: string;
    enable: string;
    transport: Transport;
    config: Record<string, unknown>;
    session_id: string | null;
    event_types: string[];
  }) => Promise<void>;
  /** kind → supported transports. Drives the Transport radio + auto-fallback
   *  when a kind can't carry the currently-selected transport. */
  kinds: Record<Kind, Transport[]>;
  binding: ChannelOut;
}) {
  const [id, setId] = useState<string>(binding.id);
  const [kind, setKind] = useState<Kind>(binding.kind as Kind);
  const [enable, setEnable] = useState<string>(binding.enable);
  const [transport, setTransport] = useState<Transport>(binding.transport as Transport ?? "ws");
  const [config, setConfig] = useState<Record<string, string>>(binding.config as Record<string, string>);
  const [sessionId, setSessionId] = useState<string>(binding.session_id ?? "");
  const [eventTypes, setEventTypes] = useState<string[]>(binding.event_types);
  const [busy, setBusy] = useState(false);

  const meta = KIND_CONFIG[kind];

  // If the user switches Kind to one that can't carry the current transport,
  // snap to the kind's first supported transport rather than letting the
  // submitted value silently mismatch what the backend will accept.
  function changeKind(next: Kind) {
    setKind(next);
    const sup = kinds[next] ?? ["webhook"];
    if (!sup.includes(transport)) {
      setTransport(sup[0]);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    // Validate required fields up-front so the user gets a clear message
    // instead of a 400 from the backend.
    for (const f of meta.fields) {
      if (f.required && !(config[f.name] ?? "").trim()) {
        onError(`${f.label} is required`);
        return;
      }
    }
    setBusy(true);
    try {
      const cfg: Record<string, unknown> = {};
      for (const f of meta.fields) {
        const v = (config[f.name] ?? "").trim();
        if (v) cfg[f.name] = v;
      }
      await onSubmit({
        id,
        kind,
        enable,
        transport,
        config: cfg,
        session_id: sessionId.trim() || null,
        event_types: eventTypes,
      });
      onModified();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggleEvent(key: string) {
    setEventTypes((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50">
      <form
        onSubmit={submit}
        className="bg-bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4 max-h-[90vh] overflow-y-auto"
      >
        <div className="text-lg font-semibold">new channel</div>

        <Field label="Kind">
          <select
            value={kind}
            onChange={(e) => {
              changeKind(e.target.value as Kind);
              setConfig({});
            }}
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
          >
            {Object.entries(KIND_CONFIG).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Enable">
          <select
            value={enable}
            onChange={(e) => {
              setEnable(e.target.value);
            }}
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
          >
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </Field>

        <Field label="Transport">
          <div className="space-y-1">
            {(kinds[kind] ?? ["webhook"]).map((t) => (
              <label key={t} className="flex items-start gap-2 text-xs">
                <input
                  type="radio"
                  name="channel-transport"
                  value={t}
                  checked={transport === t}
                  onChange={() => setTransport(t)}
                  className="mt-0.5"
                />
                <span className="text-ink-dim">
                  <span className="text-ink">{t}</span>
                  {t === "ws" && <span className="text-ink-faint"> — 推荐，长连接，无需公网 URL</span>}
                  {t === "webhook" && <span className="text-ink-faint"> — 事件订阅 URL（需公网可达）</span>}
                </span>
              </label>
            ))}
            {transport === "webhook" && (
              <div className="text-[11px] text-ink-faint pl-5">
                注册后请把 webhook URL 填到飞书「事件订阅」页
              </div>
            )}
          </div>
        </Field>

        <div className="space-y-3">
          {meta.fields.map((f) => (
            <Field key={f.name} label={f.label + (f.required ? " *" : "")}>
              <input
                type={f.secret ? "password" : "text"}
                value={config[f.name] ?? ""}
                onChange={(e) => setConfig({ ...config, [f.name]: e.target.value })}
                placeholder={f.placeholder}
                className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono text-sm"
              />
              {f.hint && <div className="text-[11px] text-ink-faint">{f.hint}</div>}
            </Field>
          ))}
        </div>

        <Field label="Session (optional — blank = project default)">
          <select
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
          >
            <option value="">(project default)</option>
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Outbound event types">
          <div className="space-y-1">
            {ALL_EVENT_TYPES.map((ev) => (
              <label key={ev.key} className="flex items-start gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={eventTypes.includes(ev.key)}
                  onChange={() => toggleEvent(ev.key)}
                  className="mt-0.5"
                />
                <span className="text-ink-dim">{ev.label}</span>
              </label>
            ))}
          </div>
        </Field>

        <div className="text-[11px] text-ink-faint">
          注册后立刻能看到 webhook URL，把它填到飞书「事件订阅」里。
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 border border-border rounded hover:bg-bg-hover"
          >
            cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-3 py-1.5 bg-accent text-white rounded disabled:opacity-50"
          >
            {busy ? "…" : "update"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <div className="text-xs uppercase tracking-wide text-ink-dim">{label}</div>
      {children}
    </label>
  );
}

function fmtIso(s: string): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}
