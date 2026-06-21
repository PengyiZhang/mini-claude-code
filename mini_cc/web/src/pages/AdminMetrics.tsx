import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import { useAdmin } from "../lib/adminStore";
import Sparkline from "../components/Sparkline";
import type { MetricSnapshot } from "../lib/types";

const REFRESH_MS = 5000;

export default function AdminMetrics() {
  const profile = useAdmin((s) => s.profile);
  const metrics = useAdmin((s) => s.metrics);
  const history = useAdmin((s) => s.metricsHistory);
  const refresh = useAdmin((s) => s.refreshMetrics);

  useEffect(() => {
    if (!profile) return;
    void refresh();
    const id = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile]);

  const rpsValues = useMemo(() => deriveRate(history, "http_requests_total"), [history]);
  const tokenInputSeries = useMemo(
    () => pickSeries(metrics, "anthropic_tokens_total", { kind: "input" }),
    [metrics],
  );
  const tokenOutputSeries = useMemo(
    () => pickSeries(metrics, "anthropic_tokens_total", { kind: "output" }),
    [metrics],
  );
  const tokenCacheReadSeries = useMemo(
    () => pickSeries(metrics, "anthropic_tokens_total", { kind: "cache_read" }),
    [metrics],
  );

  if (!profile) return null;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between border-b border-border bg-bg-panel/80 px-6 py-3 backdrop-blur">
        <Link to="/admin/metrics" className="flex items-center gap-2">
          <div className="size-7 rounded-md bg-gradient-to-br from-amber-400 to-rose-500 shadow-inner" />
          <div className="font-semibold tracking-tight">
            mini_cc · admin <span className="text-ink-dim">/ metrics</span>
          </div>
        </Link>
        <div className="flex items-center gap-3 text-sm">
          <span className="font-mono text-ink-dim">{profile.tenantId}</span>
          <Link
            to="/admin/keys"
            className="px-3 py-1 rounded border border-border hover:border-accent hover:bg-bg-hover"
          >
            keys
          </Link>
          <button
            onClick={() => refresh()}
            className="px-3 py-1 rounded border border-border hover:border-accent"
          >
            refresh
          </button>
        </div>
      </header>

      <main className="flex-1 p-6 space-y-6">
        {!metrics ? (
          <div className="text-sm text-ink-dim">no metrics yet</div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Card label="HTTP requests/min">
                <div className="flex items-end gap-3">
                  <div className="text-3xl font-semibold tabular-nums">
                    {(rpsValues.at(-1) ?? 0).toFixed(1)}
                  </div>
                  <Sparkline values={rpsValues} width={140} height={36} />
                </div>
              </Card>
              <Card label="In-flight requests">
                <div className="text-3xl font-semibold tabular-nums">
                  {metrics.gauges["http_in_flight_requests"]?.value ?? 0}
                </div>
              </Card>
              <Card label="Scraped at">
                <div className="text-xs font-mono text-ink-dim">{metrics.scrape_ts}</div>
              </Card>
            </div>

            <Section title="HTTP — by route">
              <RouteTable snap={metrics} />
            </Section>

            <Section title="Anthropic tokens — cumulative">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <TokenStat label="input" series={tokenInputSeries} tone="text-sky-300" />
                <TokenStat label="output" series={tokenOutputSeries} tone="text-emerald-300" />
                <TokenStat label="cache_read" series={tokenCacheReadSeries} tone="text-amber-300" />
                <TokenStat label="cache_create" series={pickSeries(metrics, "anthropic_tokens_total", { kind: "cache_create" })} tone="text-rose-300" />
              </div>
            </Section>

            <Section title="Anthropic requests">
              <SimpleCounterTable
                rows={groupCounterByLabel(metrics, "anthropic_request_total", "status")}
              />
            </Section>

            <Section title="HTTP duration (s) — histogram">
              <HistogramTable snap={metrics} name="http_request_duration_seconds" />
            </Section>

            <Section title="Anthropic duration (s) — histogram">
              <HistogramTable snap={metrics} name="anthropic_request_duration_seconds" />
            </Section>
          </>
        )}
      </main>
    </div>
  );
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-bg-card border border-border rounded p-4">
      <div className="text-xs uppercase tracking-wide text-ink-dim mb-2">{label}</div>
      {children}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="text-sm uppercase tracking-wide text-ink-dim mb-2">{title}</h3>
      <div className="bg-bg-card border border-border rounded">{children}</div>
    </section>
  );
}

function RouteTable({ snap }: { snap: MetricSnapshot }) {
  const fam = snap.counters["http_requests_total"];
  if (!fam) return <div className="p-3 text-sm text-ink-dim">no data</div>;
  // Group by route_template + method, sum statuses.
  const byKey = new Map<string, { method: string; route: string; count: number; status: Record<string, number> }>();
  for (const s of fam.series) {
    const method = s.labels.method ?? "";
    const route = s.labels.route_template ?? "";
    const status = s.labels.status ?? "";
    const key = `${method} ${route}`;
    const row = byKey.get(key) ?? { method, route, count: 0, status: {} };
    row.count += s.value ?? 0;
    row.status[status] = (row.status[status] ?? 0) + (s.value ?? 0);
    byKey.set(key, row);
  }
  const rows = [...byKey.values()].sort((a, b) => b.count - a.count);
  return (
    <table className="w-full text-sm">
      <thead className="bg-bg-panel text-ink-dim text-xs uppercase tracking-wide">
        <tr>
          <th className="text-left px-3 py-2">method</th>
          <th className="text-left px-3 py-2">route</th>
          <th className="text-right px-3 py-2">total</th>
          <th className="text-left px-3 py-2">by status</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={4} className="px-3 py-4 text-center text-ink-dim">
              no traffic yet
            </td>
          </tr>
        )}
        {rows.map((r) => (
          <tr key={`${r.method} ${r.route}`} className="border-t border-border">
            <td className="px-3 py-2 font-mono text-xs">{r.method}</td>
            <td className="px-3 py-2 font-mono text-xs">{r.route}</td>
            <td className="px-3 py-2 text-right tabular-nums">{r.count}</td>
            <td className="px-3 py-2 text-xs">
              {Object.entries(r.status).map(([st, c]) => (
                <span key={st} className="mr-2">
                  <span className="text-ink-dim">{st}=</span>
                  <span className="tabular-nums">{c}</span>
                </span>
              ))}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TokenStat({
  label,
  series,
  tone,
}: {
  label: string;
  series: { value?: number };
  tone: string;
}) {
  return (
    <div className="bg-bg border border-border rounded p-3">
      <div className="text-xs uppercase text-ink-dim">{label}</div>
      <div className={`text-2xl font-semibold tabular-nums ${tone}`}>
        {(series.value ?? 0).toLocaleString()}
      </div>
    </div>
  );
}

function SimpleCounterTable({
  rows,
}: {
  rows: { label: string; value: number }[];
}) {
  return (
    <table className="w-full text-sm">
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td className="px-3 py-3 text-center text-ink-dim">no data</td>
          </tr>
        )}
        {rows.map((r) => (
          <tr key={r.label} className="border-t border-border">
            <td className="px-3 py-2 font-mono text-xs">{r.label}</td>
            <td className="px-3 py-2 text-right tabular-nums">{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function HistogramTable({ snap, name }: { snap: MetricSnapshot; name: string }) {
  const fam = snap.histograms[name];
  if (!fam) return <div className="p-3 text-sm text-ink-dim">no data</div>;
  return (
    <table className="w-full text-sm">
      <thead className="bg-bg-panel text-ink-dim text-xs uppercase tracking-wide">
        <tr>
          <th className="text-left px-3 py-2">labels</th>
          <th className="text-right px-3 py-2">count</th>
          <th className="text-right px-3 py-2">sum (s)</th>
          <th className="text-left px-3 py-2">distribution</th>
        </tr>
      </thead>
      <tbody>
        {fam.series.length === 0 && (
          <tr>
            <td colSpan={4} className="px-3 py-3 text-center text-ink-dim">
              no observations yet
            </td>
          </tr>
        )}
        {fam.series.map((s, i) => {
          const labelStr = Object.entries(s.labels)
            .map(([k, v]) => `${k}=${v}`)
            .join(" ");
          return (
            <tr key={`${name}-${i}`} className="border-t border-border">
              <td className="px-3 py-2 font-mono text-xs">{labelStr || "—"}</td>
              <td className="px-3 py-2 text-right tabular-nums">{s.count ?? 0}</td>
              <td className="px-3 py-2 text-right tabular-nums">
                {(s.sum ?? 0).toFixed(3)}
              </td>
              <td className="px-3 py-2">
                <BucketBar
                  buckets={fam.buckets}
                  counts={s.bucket_counts ?? []}
                  total={s.count ?? 0}
                />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function BucketBar({
  buckets,
  counts,
  total,
}: {
  buckets: number[];
  counts: number[];
  total: number;
}) {
  if (total === 0) return null;
  // Render a single horizontal stacked bar across buckets.
  return (
    <div className="flex gap-px h-3 max-w-[280px]">
      {buckets.map((b, i) => {
        const c = counts[i] ?? 0;
        const prev = i > 0 ? (counts[i - 1] ?? 0) : 0;
        const widthPct = ((c - prev) / total) * 100;
        if (widthPct <= 0) return null;
        return (
          <div
            key={b}
            title={`≤${b}s: ${c}`}
            style={{ width: `${widthPct}%` }}
            className="bg-accent/60"
          />
        );
      })}
    </div>
  );
}

function pickSeries(
  snap: MetricSnapshot | null,
  name: string,
  match: Record<string, string>,
): { value?: number } {
  if (!snap) return {};
  const fam = snap.counters[name];
  if (!fam) return {};
  for (const s of fam.series) {
    if (Object.entries(match).every(([k, v]) => s.labels[k] === v)) return s;
  }
  return {};
}

function groupCounterByLabel(
  snap: MetricSnapshot | null,
  name: string,
  labelKey: string,
): { label: string; value: number }[] {
  if (!snap) return [];
  const fam = snap.counters[name];
  if (!fam) return [];
  const out: { label: string; value: number }[] = [];
  for (const s of fam.series) {
    out.push({ label: s.labels[labelKey] ?? "—", value: s.value ?? 0 });
  }
  return out.sort((a, b) => b.value - a.value);
}

/** Compute per-interval request rate (req/min) across the history buffer. */
function deriveRate(
  hist: { ts: number; snapshot: MetricSnapshot }[],
  counterName: string,
): number[] {
  if (hist.length < 2) return [];
  const totals: { ts: number; total: number }[] = hist.map((h) => ({
    ts: h.ts,
    total: sumCounter(h.snapshot, counterName),
  }));
  const rates: number[] = [];
  for (let i = 1; i < totals.length; i++) {
    const dT = (totals[i].ts - totals[i - 1].ts) / 1000; // seconds
    const dC = totals[i].total - totals[i - 1].total;
    if (dT > 0) rates.push((dC / dT) * 60);
  }
  return rates;
}

function sumCounter(snap: MetricSnapshot, name: string): number {
  const fam = snap.counters[name];
  if (!fam) return 0;
  return fam.series.reduce((a, s) => a + (s.value ?? 0), 0);
}
