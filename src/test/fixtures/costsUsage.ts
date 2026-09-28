/**
 * Test data for the Costs tab. costs-usage.json is the contract-shaped
 * GET /api/costs/usage report for 2026-09-01 to 2026-09-23, by week, split by
 * model (two partial weeks and one zero-filled week).
 *
 * usageFor() answers any request from that same spend the way the server
 * must: each fixture bucket's figures are dated on the bucket's last day, and
 * the report is rebuilt for the requested range, granularity and split, with
 * buckets zero-filled from range.from to range.to and rows and totals counting
 * only what falls inside. Asked for the fixture's own range, granularity and
 * split, it reproduces the JSON.
 *
 * The JSON is plain data so a backend test can hold the real endpoint's
 * response to the same shape.
 */
import rawUsage from './costs-usage.json?raw';
import type {
  UsageBucket,
  UsageGranularity,
  UsageKeyFigures,
  UsageResponse,
  UsageRow,
  UsageSplit,
} from '@/types/costs';

export const GPT_NOTE =
  'Estimate at list rates. AWS billed GPT on Bedrock at $0 for this account as of 2026-09-23.';

export const LOCAL_MODEL = 'local_lmstudio_community_deepseek_r1_0528_qwen3_8b_mlx_4bit__4bit';

export function usageFixture(): UsageResponse {
  return JSON.parse(rawUsage) as UsageResponse;
}

/* ── UTC calendar days (YYYY-MM-DD), independent of the tab's own helpers ── */

const DAY_MS = 86_400_000;
const toDate = (iso: string) => new Date(`${iso}T00:00:00Z`);
const toIso = (d: Date) => d.toISOString().slice(0, 10);
const plusDays = (iso: string, n: number) => toIso(new Date(toDate(iso).getTime() + n * DAY_MS));

/** First and last day of the whole day, Monday-to-Sunday week or calendar
 *  month that `day` falls in. */
function naturalBucket(day: string, granularity: UsageGranularity): { start: string; end: string } {
  if (granularity === 'day') return { start: day, end: day };
  const d = toDate(day);
  if (granularity === 'week') {
    const sinceMonday = (d.getUTCDay() + 6) % 7;
    return { start: plusDays(day, -sinceMonday), end: plusDays(day, 6 - sinceMonday) };
  }
  const y = d.getUTCFullYear();
  const m = d.getUTCMonth();
  return { start: toIso(new Date(Date.UTC(y, m, 1))), end: toIso(new Date(Date.UTC(y, m + 1, 0))) };
}

/** Empty buckets covering from..to without a gap, the first and last clipped
 *  to the range and marked partial when the clip cuts a week or month. */
export function zeroFilledBuckets(
  from: string,
  to: string,
  granularity: UsageGranularity,
): UsageBucket[] {
  const out: UsageBucket[] = [];
  for (let start = from; start <= to; ) {
    const natural = naturalBucket(start, granularity);
    const end = natural.end < to ? natural.end : to;
    const partial = natural.start !== start || natural.end !== end;
    out.push({ start, end, partial, cost_usd: 0, by_key: {} });
    start = plusDays(end, 1);
  }
  return out;
}

/* ── The fixture's spend, and what each split calls it ── */

interface Spend {
  day: string;
  model: string;
  figures: UsageKeyFigures;
  estimated_calls: number;
}

/** Every fixture figure, dated on its bucket's last day. A model's estimated
 *  calls sit on its first entry. */
function fixtureSpend(fixture: UsageResponse): Spend[] {
  const estimated = new Map(fixture.rows.map((r) => [r.key, r.estimated_calls]));
  const out: Spend[] = [];
  for (const bucket of fixture.buckets) {
    for (const [model, figures] of Object.entries(bucket.by_key)) {
      out.push({ day: bucket.end, model, figures, estimated_calls: estimated.get(model) ?? 0 });
      estimated.set(model, 0);
    }
  }
  return out;
}

interface SplitKey {
  key: string;
  label: string;
  deleted: boolean;
}

const LIVE: SplitKey = { key: 'sess-live', label: 'Refactor the importer', deleted: false };
// A deleted session: its row is gone, so the server has no title and sends the key.
const GONE: SplitKey = { key: 'sess-gone', label: 'sess-gone', deleted: true };
const OLD: SplitKey = { key: 'sess-old', label: 'sess-old', deleted: true };
// Spend under an id that never was a saved session; the server names it.
const DREAM: SplitKey = { key: 'dream', label: 'Memory dream (not a saved session)', deleted: true };

const SESSION_OF: Record<string, SplitKey> = {
  'gpt5.6-sol': LIVE,
  sonnet5: LIVE,
  [LOCAL_MODEL]: LIVE,
  'gpt6-astra': GONE,
  'opus5.0': GONE,
  'haiku4.5': DREAM,
  'nova-sonic': OLD,
};

const CHAT: SplitKey = { key: 'chat', label: 'Chat', deleted: false };
const AGENT: SplitKey = { key: 'agent', label: 'Agent', deleted: false };
const VOICE: SplitKey = { key: 'voice', label: 'Voice', deleted: false };

const SOURCE_OF: Record<string, SplitKey> = {
  'gpt5.6-sol': CHAT,
  'opus5.0': CHAT,
  sonnet5: CHAT,
  [LOCAL_MODEL]: CHAT,
  'gpt6-astra': AGENT,
  'haiku4.5': AGENT,
  'nova-sonic': VOICE,
};

function splitKeyOf(split: UsageSplit, model: UsageRow): SplitKey {
  if (split === 'model') return { key: model.key, label: model.label, deleted: false };
  const table = split === 'session' ? SESSION_OF : SOURCE_OF;
  const found = table[model.key];
  if (!found) throw new Error(`costsUsage fixture: no ${split} for model ${model.key}`);
  return found;
}

/* ── The report for one request ── */

/** The report a correct server sends for `url`, from the fixture's spend.
 *  Missing parameters take the fixture's own values. */
export function usageFor(url: string): UsageResponse {
  const fixture = usageFixture();
  const q = new URL(url, 'http://localhost').searchParams;
  const from = q.get('from') ?? fixture.range.from;
  const to = q.get('to') ?? fixture.range.to;
  const granularity = (q.get('granularity') ?? fixture.granularity) as UsageGranularity;
  const split = (q.get('split') ?? fixture.split) as UsageSplit;

  const models = new Map(fixture.rows.map((r) => [r.key, r]));
  const buckets = zeroFilledBuckets(from, to, granularity);
  const rows = new Map<string, UsageRow & { cacheRead: number }>();

  for (const spend of fixtureSpend(fixture)) {
    if (spend.day < from || spend.day > to) continue;
    const model = models.get(spend.model);
    if (!model) throw new Error(`costsUsage fixture: bucket key ${spend.model} has no row`);
    const sk = splitKeyOf(split, model);
    const f = spend.figures;

    const bucket = buckets.find((b) => b.start <= spend.day && spend.day <= b.end)!;
    const slot = (bucket.by_key[sk.key] ??= { cost_usd: 0, prompt_tokens: 0, output_tokens: 0, calls: 0 });
    slot.cost_usd += f.cost_usd;
    slot.prompt_tokens += f.prompt_tokens;
    slot.output_tokens += f.output_tokens;
    slot.calls += f.calls;
    bucket.cost_usd += f.cost_usd;

    let row = rows.get(sk.key);
    if (!row) {
      row = { ...sk, cost_usd: 0, share: 0, prompt_tokens: 0, cached_pct: 0, output_tokens: 0, calls: 0, estimated_calls: 0, note: '', cacheRead: 0 };
      rows.set(sk.key, row);
    }
    row.cost_usd += f.cost_usd;
    row.prompt_tokens += f.prompt_tokens;
    row.output_tokens += f.output_tokens;
    row.calls += f.calls;
    row.estimated_calls += spend.estimated_calls;
    row.cacheRead += (f.prompt_tokens * model.cached_pct) / 100;
    row.note ||= model.note;
  }

  const all = [...rows.values()];
  const sum = (field: 'cost_usd' | 'prompt_tokens' | 'output_tokens' | 'calls' | 'estimated_calls' | 'cacheRead') =>
    all.reduce((n, r) => n + r[field], 0);
  const cost = sum('cost_usd');
  const prompt = sum('prompt_tokens');
  const cacheRead = Math.round(sum('cacheRead'));
  const ft = fixture.totals;

  return {
    range: { from, to, timezone: 'UTC' },
    granularity,
    split,
    totals: {
      cost_usd: cost,
      prompt_tokens: prompt,
      cache_read_tokens: cacheRead,
      // The contract figures carry no per-bucket cache writes or savings, so
      // these follow the fixture's totals in proportion.
      cache_write_tokens: Math.round((ft.cache_write_tokens * prompt) / ft.prompt_tokens),
      output_tokens: sum('output_tokens'),
      calls: sum('calls'),
      estimated_calls: sum('estimated_calls'),
      // Every fixture model has a rate (nova-sonic included), or is on-device.
      unpriced_calls: 0,
      cache_hit_rate: prompt > 0 ? cacheRead / prompt : 0,
      cache_savings_usd: (ft.cache_savings_usd * cost) / ft.cost_usd,
    },
    buckets,
    rows: all
      .map(({ cacheRead: read, ...r }) => ({
        ...r,
        share: cost > 0 ? r.cost_usd / cost : 0,
        cached_pct: r.prompt_tokens > 0 ? (read / r.prompt_tokens) * 100 : 0,
      }))
      .sort((a, b) => b.cost_usd - a.cost_usd || a.key.localeCompare(b.key)),
  };
}
