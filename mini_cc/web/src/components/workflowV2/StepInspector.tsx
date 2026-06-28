import type {
  WorkflowV2Definition,
  WorkflowV2Run,
} from "../../lib/types";

/**
 * Right pane — step inspector. Shows the definition + run-time state
 * of the selected step:
 *   - step type, prompt, condition, config
 *   - inputs_schema / outputs_schema
 *   - run-time status, started_at, completed_at
 *   - output (pretty-printed JSON)
 *   - error (if failed)
 *
 * For checkpoint steps, surfaces the configured approvers list and the
 * resolver's identity once resolved.
 */
interface Props {
  run: WorkflowV2Run | null;
  def: WorkflowV2Definition | null;
  stepId: string | null;
}

export default function WorkflowV2StepInspector({ run, def, stepId }: Props) {
  if (!run || !def || !stepId) {
    return (
      <div className="p-4 text-sm text-ink-dim">
        select a step to inspect its inputs, outputs, and config.
      </div>
    );
  }

  const stepDef = def.steps.find((s) => s.id === stepId);
  const stepRun = run.step_runs.find((s) => s.step_id === stepId);
  if (!stepDef) {
    return (
      <div className="p-4 text-sm text-err">
        step <span className="font-mono">{stepId}</span> not in definition
      </div>
    );
  }

  return (
    <div className="p-4 space-y-4">
      <div>
        <div className="text-xs text-ink-dim uppercase tracking-wide">step</div>
        <div className="font-mono text-sm text-ink mt-1">{stepDef.id}</div>
        <div className="mt-1 inline-block text-xs px-1.5 py-0.5 rounded bg-bg-hover text-ink-dim font-mono">
          {stepDef.type}
        </div>
      </div>

      {stepDef.prompt && (
        <Section title="prompt">
          <pre className="text-xs text-ink-dim whitespace-pre-wrap break-words font-mono">
            {stepDef.prompt}
          </pre>
        </Section>
      )}

      {stepDef.condition && (
        <Section title="condition">
          <code className="text-xs text-amber-300 font-mono break-all">
            {stepDef.condition}
          </code>
        </Section>
      )}

      {/* Config — type-specific knobs. For checkpoint this includes
          approvers; for webhook_wait, webhook_id + event_filter; etc. */}
      {stepDef.config && Object.keys(stepDef.config).length > 0 && (
        <Section title="config">
          <KeyValueList data={stepDef.config as Record<string, unknown>} />
        </Section>
      )}

      {stepDef.inputs_schema && Object.keys(stepDef.inputs_schema).length > 0 && (
        <Section title="inputs_schema">
          <JsonBlock value={stepDef.inputs_schema} />
        </Section>
      )}

      {stepDef.outputs_schema && Object.keys(stepDef.outputs_schema).length > 0 && (
        <Section title="outputs_schema">
          <JsonBlock value={stepDef.outputs_schema} />
        </Section>
      )}

      {/* Run-time state */}
      {stepRun && (
        <>
          <div className="border-t border-border pt-3" />
          <Section title="status">
            <span className="text-sm text-ink font-mono">{stepRun.status}</span>
          </Section>
          {stepRun.started_at && (
            <Section title="started">
              <span className="text-xs text-ink-dim font-mono">
                {stepRun.started_at}
              </span>
            </Section>
          )}
          {stepRun.completed_at && (
            <Section title="completed">
              <span className="text-xs text-ink-dim font-mono">
                {stepRun.completed_at}
              </span>
            </Section>
          )}
          {stepRun.output != null && (
            <Section title="output">
              <JsonBlock value={stepRun.output} />
            </Section>
          )}
          {stepRun.error && (
            <Section title="error">
              <pre className="text-xs text-err whitespace-pre-wrap break-words font-mono">
                {stepRun.error}
              </pre>
            </Section>
          )}
        </>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-ink-dim uppercase tracking-wide mb-1">
        {title}
      </div>
      <div>{children}</div>
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return (
    <pre className="text-xs text-ink-dim bg-bg rounded p-2 overflow-auto max-h-96 whitespace-pre-wrap break-words font-mono">
      {text}
    </pre>
  );
}

function KeyValueList({ data }: { data: Record<string, unknown> }) {
  return (
    <div className="space-y-1">
      {Object.entries(data).map(([k, v]) => (
        <div key={k} className="text-xs">
          <span className="text-ink-faint font-mono">{k}:</span>{" "}
          <span className="text-ink-dim font-mono">
            {typeof v === "string" ? v : JSON.stringify(v)}
          </span>
        </div>
      ))}
    </div>
  );
}
