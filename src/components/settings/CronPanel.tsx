import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  fetchCronJobs, createCronJob, updateCronJob, deleteCronJob, toggleCronJob,
  runCronJob, stopCronJob, previewSchedule, fetchCronHistory,
  type CronJob, type CronSchedule, type CronRun,
} from '@/api/cron';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';

/** Seconds before a primed Delete reverts to its resting label. */
const DELETE_CONFIRM_TIMEOUT_MS = 4000;

// ── Small time helpers ────────────────────────────────────────────────────────

/** Re-renders every `intervalMs` so countdowns tick without per-second props. */
function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

function formatCountdown(iso: string | null | undefined, now: number): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  let d = Math.round((t - now) / 1000);
  if (d <= 0) return 'due now';
  const days = Math.floor(d / 86400); d -= days * 86400;
  const h = Math.floor(d / 3600); d -= h * 3600;
  const m = Math.floor(d / 60); const s = d - m * 60;
  if (days > 0) return `in ${days}d ${h}h`;
  if (h > 0) return `in ${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `in ${m}m ${String(s).padStart(2, '0')}s`;
  return `in ${s}s`;
}

function formatAgo(iso: string | null | undefined, now: number): string {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  let d = Math.round((now - t) / 1000);
  if (d < 0) d = 0;
  if (d < 45) return 'just now';
  const m = Math.floor(d / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  return `${days}d ago`;
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.round(s % 60)).padStart(2, '0')}s`;
}

// ── Status chip ───────────────────────────────────────────────────────────────

type ChipKind = 'active' | 'paused' | 'running' | 'error' | 'orphan';

function chipFor(job: CronJob): { kind: ChipKind; label: string } {
  if (job.orphaned || job.session_exists === false) return { kind: 'orphan', label: 'Orphaned' };
  if (!job.enabled) return { kind: 'paused', label: 'Paused' };
  if (job.run_state === 'running') return { kind: 'running', label: 'Running…' };
  if (job.run_state === 'stopped') return { kind: 'paused', label: 'Stopped' };
  if (job.run_state === 'failed') return { kind: 'error', label: 'Error' };
  return { kind: 'active', label: 'Active' };
}

const CHIP_COLORS: Record<ChipKind, { fg: string; bg: string }> = {
  active: { fg: '#2f9e63', bg: 'rgba(47,158,99,0.14)' },
  paused: { fg: '#8a95a7', bg: 'rgba(138,149,167,0.16)' },
  running: { fg: '#3c86cf', bg: 'rgba(60,134,207,0.16)' },
  error: { fg: '#d1495b', bg: 'rgba(209,73,91,0.16)' },
  orphan: { fg: '#c9862a', bg: 'rgba(201,134,42,0.16)' },
};

const StatusChip: React.FC<{ job: CronJob }> = ({ job }) => {
  const { kind, label } = chipFor(job);
  const c = CHIP_COLORS[kind];
  return (
    <span
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 5,
        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.04em',
        textTransform: 'uppercase', padding: '2px 9px', borderRadius: 20,
        color: c.fg, background: c.bg,
      }}
    >
      <span style={{
        width: 6, height: 6, borderRadius: '50%', background: 'currentColor',
        animation: kind === 'running' ? 'cronPulse 1.4s ease-in-out infinite' : undefined,
      }} />
      {label}
    </span>
  );
};

// ── Schedule form ─────────────────────────────────────────────────────────────

type DayMode = 'daily' | 'weekdays' | 'weekends' | 'custom';
const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const;

interface FormState {
  name: string;
  prompt: string;
  type: 'interval' | 'cron' | 'at';
  everyMinutes: number;
  hour: number;
  minute: number;
  dayMode: DayMode;
  customDays: string[];
  runAt: string; // datetime-local value 'YYYY-MM-DDTHH:MM'
}

const EMPTY_FORM: FormState = {
  name: '', prompt: '', type: 'cron',
  everyMinutes: 60, hour: 9, minute: 0,
  dayMode: 'daily', customDays: [], runAt: '',
};

function dowFromForm(f: FormState): string {
  if (f.dayMode === 'daily') return '*';
  if (f.dayMode === 'weekdays') return 'mon-fri';
  if (f.dayMode === 'weekends') return 'sat,sun';
  return f.customDays.length ? f.customDays.join(',') : '*';
}

function dayModeFromDow(dow: string | undefined): { mode: DayMode; days: string[] } {
  const d = (dow || '*').toLowerCase();
  if (d === '*') return { mode: 'daily', days: [] };
  if (d === 'mon-fri') return { mode: 'weekdays', days: [] };
  if (d === 'sat,sun' || d === 'sat-sun') return { mode: 'weekends', days: [] };
  return { mode: 'custom', days: d.split(',').map((x) => x.trim()).filter(Boolean) };
}

function formToSchedule(f: FormState): CronSchedule {
  if (f.type === 'interval') {
    return { type: 'interval', every_minutes: Math.max(1, Math.round(f.everyMinutes)) };
  }
  if (f.type === 'at') {
    return { type: 'at', run_at: f.runAt };
  }
  return { type: 'cron', hour: f.hour, minute: f.minute, day_of_week: dowFromForm(f) };
}

function scheduleToForm(job: CronJob): FormState {
  const sch = job.schedule || { type: 'interval' };
  const base: FormState = { ...EMPTY_FORM, name: job.name, prompt: job.prompt, type: sch.type };
  if (sch.type === 'interval') {
    base.everyMinutes = Math.max(1, Math.round((sch.seconds ?? 3600) / 60));
  } else if (sch.type === 'cron') {
    base.hour = parseInt(String(sch.hour ?? 9).split(',')[0], 10) || 0;
    base.minute = parseInt(String(sch.minute ?? 0).split(',')[0], 10) || 0;
    const { mode, days } = dayModeFromDow(sch.day_of_week);
    base.dayMode = mode; base.customDays = days;
  } else if (sch.type === 'at') {
    base.runAt = (sch.run_at ?? '').slice(0, 16);
  }
  return base;
}

const SEG_TYPES: Array<{ id: FormState['type']; label: string }> = [
  { id: 'interval', label: 'Every N minutes' },
  { id: 'cron', label: 'At a time' },
  { id: 'at', label: 'One-time' },
];

interface ScheduleFormProps {
  initial: FormState;
  systemTz: string;
  saving: boolean;
  onSave: (name: string, prompt: string, schedule: CronSchedule) => void;
  onCancel: () => void;
}

const ScheduleForm: React.FC<ScheduleFormProps> = ({ initial, systemTz, saving, onSave, onCancel }) => {
  const [form, setForm] = useState<FormState>(initial);
  const [preview, setPreview] = useState<{ label?: string; next_run?: string | null; error?: string }>({});
  const patch = (p: Partial<FormState>) => setForm((f) => ({ ...f, ...p }));

  // Live "next run" preview, debounced so typing doesn't spam the endpoint.
  const schedule = useMemo(() => formToSchedule(form), [form]);
  const scheduleKey = JSON.stringify(schedule);
  useEffect(() => {
    let cancelled = false;
    // A one-time schedule with no datetime yet has nothing to preview.
    if (form.type === 'at' && !form.runAt) {
      const t0 = window.setTimeout(() => { if (!cancelled) setPreview({}); }, 0);
      return () => { cancelled = true; window.clearTimeout(t0); };
    }
    const t = window.setTimeout(() => {
      previewSchedule(schedule)
        .then((r) => { if (!cancelled) setPreview(r); })
        .catch(() => { if (!cancelled) setPreview({ error: 'Could not preview schedule' }); });
    }, 300);
    return () => { cancelled = true; window.clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheduleKey, form.type, form.runAt]);

  const toggleDay = (day: string) => {
    setForm((f) => {
      const has = f.customDays.includes(day);
      return { ...f, dayMode: 'custom', customDays: has ? f.customDays.filter((d) => d !== day) : [...f.customDays, day] };
    });
  };

  const canSave = form.name.trim() && form.prompt.trim() && (form.type !== 'at' || form.runAt) && !preview.error;

  return (
    <div className="settings-editor" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div>
        <label className="cron-lbl">Task name</label>
        <input className="settings-input" placeholder="e.g. GenAI hot topics"
          value={form.name} onChange={(e) => patch({ name: e.target.value })} />
      </div>
      <div>
        <label className="cron-lbl">Prompt</label>
        <textarea className="settings-input" rows={3} placeholder="Search the web for today's biggest generative-AI stories and write a 5-bullet briefing"
          value={form.prompt} onChange={(e) => patch({ prompt: e.target.value })} />
      </div>

      <div>
        <label className="cron-lbl">Schedule</label>
        <div style={{ display: 'inline-flex', gap: 4, background: 'var(--bg-tertiary, rgba(128,128,128,0.12))', padding: 3, borderRadius: 9 }}>
          {SEG_TYPES.map((seg) => (
            <button key={seg.id} type="button"
              onClick={() => patch({ type: seg.id })}
              className="btn btn-sm"
              style={{
                background: form.type === seg.id ? 'var(--accent, #0d8a93)' : 'transparent',
                color: form.type === seg.id ? '#fff' : 'var(--text-secondary, inherit)',
                border: 'none',
              }}>
              {seg.label}
            </button>
          ))}
        </div>
      </div>

      {form.type === 'interval' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="cron-lbl" style={{ margin: 0 }}>Run every</span>
          <input type="number" min={1} className="settings-input" style={{ width: 90 }}
            value={form.everyMinutes}
            onChange={(e) => patch({ everyMinutes: parseInt(e.target.value, 10) || 1 })} />
          <span className="settings-hint" style={{ margin: 0 }}>minutes</span>
        </div>
      )}

      {form.type === 'cron' && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="cron-lbl" style={{ margin: 0 }}>Time</span>
            <input type="number" min={0} max={23} className="settings-input" style={{ width: 70 }}
              value={form.hour} onChange={(e) => patch({ hour: clamp(e.target.value, 0, 23) })} />
            <span>:</span>
            <input type="number" min={0} max={59} className="settings-input" style={{ width: 70 }}
              value={form.minute} onChange={(e) => patch({ minute: clamp(e.target.value, 0, 59) })} />
          </div>
          <div>
            <label className="cron-lbl">Days</label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {(['daily', 'weekdays', 'weekends'] as DayMode[]).map((m) => (
                <button key={m} type="button" className="btn btn-sm"
                  onClick={() => patch({ dayMode: m, customDays: [] })}
                  style={dayChipStyle(form.dayMode === m)}>
                  {m === 'daily' ? 'Every day' : m[0].toUpperCase() + m.slice(1)}
                </button>
              ))}
              {WEEKDAYS.map((d) => (
                <button key={d} type="button" className="btn btn-sm"
                  onClick={() => toggleDay(d)}
                  style={dayChipStyle(form.dayMode === 'custom' && form.customDays.includes(d))}>
                  {d[0].toUpperCase() + d.slice(1)}
                </button>
              ))}
            </div>
          </div>
          <p className="settings-hint" style={{ margin: 0 }}>Runs in your system timezone ({systemTz}).</p>
        </>
      )}

      {form.type === 'at' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span className="cron-lbl" style={{ margin: 0 }}>Run once at</span>
          <input type="datetime-local" className="settings-input" style={{ width: 220 }}
            value={form.runAt} onChange={(e) => patch({ runAt: e.target.value })} />
          <span className="settings-hint" style={{ margin: 0 }}>({systemTz})</span>
        </div>
      )}

      <div style={{
        padding: '10px 12px', borderRadius: 9, fontSize: 12.5,
        border: '1px dashed', borderColor: preview.error ? 'rgba(209,73,91,0.5)' : 'rgba(13,138,147,0.45)',
        background: preview.error ? 'rgba(209,73,91,0.08)' : 'rgba(13,138,147,0.08)',
        color: preview.error ? '#d1495b' : 'var(--text-secondary, inherit)',
      }}>
        {preview.error ? `⚠ ${preview.error}`
          : preview.label
            ? <>✓ <strong>{preview.label}</strong>{preview.next_run ? <> — next run {new Date(preview.next_run).toLocaleString()}</> : null}</>
            : 'Set a schedule to preview the next run.'}
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        <button className="btn btn-primary btn-sm" type="button" disabled={!canSave || saving}
          onClick={() => onSave(form.name.trim(), form.prompt.trim(), schedule)}>
          {saving ? 'Saving…' : 'Save task'}
        </button>
        <button className="btn btn-sm" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
};

function clamp(v: string, lo: number, hi: number): number {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return lo;
  return Math.max(lo, Math.min(hi, n));
}

function dayChipStyle(on: boolean): React.CSSProperties {
  return {
    background: on ? 'rgba(13,138,147,0.18)' : 'transparent',
    color: on ? 'var(--accent, #0d8a93)' : 'var(--text-secondary, inherit)',
    borderColor: on ? 'var(--accent, #0d8a93)' : undefined,
  };
}

// ── History drawer ────────────────────────────────────────────────────────────

const HistoryDrawer: React.FC<{ jobId: string; now: number; onRetry: () => void }> = ({ jobId, now, onRetry }) => {
  const { data, isLoading } = useQuery({
    queryKey: ['cron-history', jobId],
    queryFn: () => fetchCronHistory(jobId, 50),
    refetchInterval: 15_000,
  });
  const [expanded, setExpanded] = useState<string | null>(null);
  const runs: CronRun[] = data?.runs ?? [];

  return (
    <div style={{ marginTop: 10, border: '1px solid var(--border, rgba(128,128,128,0.2))', borderRadius: 10, overflow: 'hidden' }}>
      <div style={{ padding: '8px 12px', fontSize: 12, fontWeight: 700, borderBottom: '1px solid var(--border, rgba(128,128,128,0.2))' }}>
        Run history <span className="settings-hint">· last {runs.length}</span>
      </div>
      {isLoading && <div className="settings-hint" style={{ padding: 12 }}>Loading…</div>}
      {!isLoading && runs.length === 0 && <div className="settings-hint" style={{ padding: 12 }}>No runs yet.</div>}
      {runs.map((r) => {
        const failed = r.status === 'failed';
        const running = r.status === 'running';
        const dot = failed ? '#d1495b' : running ? '#3c86cf' : '#2f9e63';
        const open = expanded === r.run_id;
        const firstLine = (r.text || '').split('\n').find((l) => l.trim()) || (running ? 'running…' : '(no output)');
        return (
          <div key={r.run_id} style={{ borderBottom: '1px solid var(--border, rgba(128,128,128,0.12))' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px', fontSize: 12.5, cursor: 'pointer' }}
              onClick={() => setExpanded(open ? null : r.run_id)}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: dot, flex: '0 0 auto' }} />
              <span style={{ fontVariantNumeric: 'tabular-nums', opacity: 0.7, flex: '0 0 auto' }}>{formatAgo(r.started_at, now)}</span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{firstLine}</span>
              {r.duration_ms != null && <span className="settings-hint" style={{ flex: '0 0 auto', margin: 0 }}>{formatDuration(r.duration_ms)}</span>}
              {failed && (
                <button className="btn btn-sm" type="button" style={{ flex: '0 0 auto' }}
                  onClick={(e) => { e.stopPropagation(); onRetry(); }}>Retry</button>
              )}
            </div>
            {open && r.text && (
              <div style={{ padding: '4px 12px 12px 30px', fontSize: 12.5 }}>
                <MarkdownRenderer content={r.text} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

// ── Job card ──────────────────────────────────────────────────────────────────

interface JobCardProps {
  job: CronJob;
  now: number;
  onEdit: () => void;
  onToggle: () => void;
  onRun: () => void;
  onStop: () => void;
  onDelete: () => void;
  onReassign: () => void;
  onRetry: () => void;
  deleteArmed: boolean;
  busy: boolean;
}

const JobCard: React.FC<JobCardProps> = ({
  job, now, onEdit, onToggle, onRun, onStop, onDelete, onReassign, onRetry, deleteArmed, busy,
}) => {
  const [showHistory, setShowHistory] = useState(false);
  const switchSession = useSessionStore((s) => s.switchSession);
  const closeSettings = useUIStore((s) => s.closeSettings);
  const isLegacyInterval = job.schedule?.type === 'interval' && (job.schedule.seconds ?? 0) >= 3600;
  const { kind } = chipFor(job);

  const openOwningSession = () => {
    if (!job.session_id || job.session_exists === false) return;
    void switchSession(job.session_id);
    closeSettings();
  };

  return (
    <div className="settings-item" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
          <span className="settings-item-name">{job.name}</span>
          <StatusChip job={job} />
        </div>
        {!job.orphaned && job.session_exists !== false && (
          <button type="button" role="switch" aria-checked={job.enabled} onClick={onToggle} disabled={busy}
            title={job.enabled ? 'Disable' : 'Enable'}
            style={{
              width: 34, height: 19, borderRadius: 20, position: 'relative', flex: '0 0 auto',
              border: 'none', cursor: 'pointer', padding: 0,
              background: job.enabled ? 'var(--accent, #0d8a93)' : 'rgba(128,128,128,0.4)',
            }}>
            <span style={{
              position: 'absolute', top: 2, left: job.enabled ? 17 : 2, width: 15, height: 15,
              borderRadius: '50%', background: '#fff', transition: 'left 0.12s',
            }} />
          </button>
        )}
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 18px', fontSize: 12.5 }}>
        <span style={{ fontFamily: 'var(--font-mono, ui-monospace, monospace)', color: 'var(--accent, #0d8a93)' }}>
          🗓 {job.schedule_label || '—'}
        </span>
        <span style={{ opacity: 0.85 }}>
          ⏱ {job.enabled
            ? (job.next_run ? formatCountdown(job.next_run, now) || 'scheduled' : 'scheduled')
            : (kind === 'orphan' ? 'its chat was removed' : 'paused — no next run')}
        </span>
        <span style={{ opacity: 0.7 }}>
          {job.run_state === 'failed' ? '⚠ ' : '✓ '}
          last run {formatAgo(job.last_run, now)}
          {typeof job.run_count === 'number' && job.run_count > 0 ? ` · ${job.run_count} runs` : ''}
        </span>
      </div>

      {job.prompt && (
        <div className="settings-item-desc" style={{ borderTop: '1px solid var(--border, rgba(128,128,128,0.12))', paddingTop: 8 }}>
          {job.prompt.slice(0, 140)}{job.prompt.length > 140 ? '…' : ''}
        </div>
      )}

      {isLegacyInterval && job.schedule.type === 'interval' && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '8px 11px', borderRadius: 9,
          fontSize: 12, background: 'rgba(201,134,42,0.1)', border: '1px solid rgba(201,134,42,0.3)', color: '#c9862a',
        }}>
          <span>⚡ This runs on a fixed interval, not a set time.</span>
          <button type="button" className="btn btn-sm" style={{ marginLeft: 'auto' }} onClick={onEdit}>Switch to a daily time →</button>
        </div>
      )}

      {(job.session_title || job.session_exists === false) && (
        <div className="settings-hint" style={{ margin: 0 }}>
          {job.session_exists === false
            ? <>Results land in <strong>Scheduled Reports</strong> until reassigned.</>
            : <>Results appear in <button type="button" className="cron-link" onClick={openOwningSession}
                style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: 'var(--accent, #0d8a93)', fontWeight: 600 }}>
                {job.session_title}</button></>}
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
        {job.orphaned || job.session_exists === false ? (
          <button className="btn btn-sm btn-primary" type="button" onClick={onReassign} disabled={busy}>⤴ Reassign to this session</button>
        ) : (
          <>
            {job.run_state === 'running' ? (
              <button className="btn btn-sm" type="button" onClick={onStop} disabled={busy}
                style={{ color: 'var(--accent-record, #d1495b)' }}>■ Stop</button>
            ) : (
              <button className="btn btn-sm" type="button" onClick={onRun} disabled={busy}>▶ Run now</button>
            )}
            <button className="btn btn-sm" type="button" onClick={onEdit} disabled={busy}>✎ Edit</button>
          </>
        )}
        <button className="btn btn-sm" type="button" onClick={() => setShowHistory((v) => !v)}>🕘 History</button>
        <button className="btn btn-sm" type="button" onClick={onDelete} disabled={busy}
          style={{ color: deleteArmed ? '#fff' : 'var(--accent-record, #d1495b)', background: deleteArmed ? 'var(--accent-record, #d1495b)' : undefined, marginLeft: 'auto' }}>
          {deleteArmed ? 'Confirm' : 'Delete'}
        </button>
      </div>

      {showHistory && <HistoryDrawer jobId={job.id} now={now} onRetry={onRetry} />}
    </div>
  );
};

// ── Panel ─────────────────────────────────────────────────────────────────────

export const CronPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const now = useNow(1000);
  const addToast = useUIStore((s) => s.addToast);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);

  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const deleteTimer = useRef<number | null>(null);

  const { data, error: fetchError } = useQuery({
    queryKey: ['cron-jobs'],
    queryFn: fetchCronJobs,
    refetchInterval: 30_000,
  });
  const jobs = data?.jobs ?? [];
  const systemTz = data?.system_timezone ?? 'system';
  const schedulerActive = data?.scheduler_active ?? false;

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['cron-jobs'] });
    void queryClient.invalidateQueries({ queryKey: ['cron-recent-runs'] });
  }, [queryClient]);

  const createMut = useMutation({
    mutationFn: (v: { name: string; prompt: string; schedule: CronSchedule }) =>
      createCronJob({ ...v, session_id: currentSessionId ?? '' }),
    onSuccess: () => { setAdding(false); invalidate(); addToast({ type: 'success', message: 'Task scheduled', duration: 2500 }); },
    onError: (e: unknown) => addToast({ type: 'error', message: e instanceof Error ? e.message : 'Failed to create task', duration: 4000 }),
  });

  const updateMut = useMutation({
    mutationFn: (v: { id: string; body: { name?: string; prompt?: string; schedule?: CronSchedule; session_id?: string } }) =>
      updateCronJob(v.id, v.body),
    onSuccess: () => { setEditingId(null); invalidate(); addToast({ type: 'success', message: 'Task updated', duration: 2500 }); },
    onError: (e: unknown) => addToast({ type: 'error', message: e instanceof Error ? e.message : 'Failed to update task', duration: 4000 }),
  });

  const toggleMut = useMutation({
    mutationFn: (id: string) => toggleCronJob(id),
    onSuccess: invalidate,
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteCronJob(id),
    onSuccess: invalidate,
  });

  const runMut = useMutation({
    mutationFn: (id: string) => runCronJob(id),
    onSuccess: () => { invalidate(); addToast({ type: 'success', message: 'Running now — result will appear in the session', duration: 3000 }); },
    onError: (e: unknown) => addToast({ type: 'error', message: e instanceof Error ? e.message : 'Could not run task', duration: 4000 }),
  });

  const stopMut = useMutation({
    mutationFn: (id: string) => stopCronJob(id),
    onSuccess: () => { invalidate(); addToast({ type: 'info', message: 'Stop requested — the run will end within a moment', duration: 3000 }); },
    onError: (e: unknown) => addToast({ type: 'error', message: e instanceof Error ? e.message : 'Could not stop run', duration: 4000 }),
  });

  useEffect(() => () => { if (deleteTimer.current) window.clearTimeout(deleteTimer.current); }, []);

  const handleDelete = useCallback((id: string) => {
    if (pendingDelete === id) {
      if (deleteTimer.current) window.clearTimeout(deleteTimer.current);
      setPendingDelete(null);
      deleteMut.mutate(id);
      return;
    }
    if (deleteTimer.current) window.clearTimeout(deleteTimer.current);
    setPendingDelete(id);
    deleteTimer.current = window.setTimeout(() => setPendingDelete(null), DELETE_CONFIRM_TIMEOUT_MS);
  }, [pendingDelete, deleteMut]);

  const activeCount = jobs.filter((j) => j.enabled && !j.orphaned).length;
  const pausedCount = jobs.filter((j) => !j.enabled && !j.orphaned).length;
  const orphanCount = jobs.filter((j) => j.orphaned || j.session_exists === false).length;

  const editingJob = editingId ? jobs.find((j) => j.id === editingId) : null;

  return (
    <div className="settings-form" style={{ maxWidth: 620 }}>
      <style>{`@keyframes cronPulse{0%,100%{opacity:1}50%{opacity:.3}} .cron-lbl{display:block;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;opacity:.7;margin-bottom:6px}`}</style>

      <div className="settings-toolbar" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <button className="btn btn-primary btn-sm" type="button" onClick={() => { setAdding(true); setEditingId(null); }} disabled={adding}>
          + New task
        </button>
        <span className="settings-hint">
          {activeCount} active{pausedCount ? ` · ${pausedCount} paused` : ''}{orphanCount ? ` · ${orphanCount} orphaned` : ''}
          {' · '}{schedulerActive ? 'scheduler running' : 'scheduler off (pip install apscheduler)'}
        </span>
      </div>

      {fetchError && <p className="settings-empty" role="alert">Could not load scheduled tasks. Requires <code>pip install apscheduler</code>.</p>}

      {adding && (
        <ScheduleForm
          initial={EMPTY_FORM}
          systemTz={systemTz}
          saving={createMut.isPending}
          onSave={(name, prompt, schedule) => createMut.mutate({ name, prompt, schedule })}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="settings-list" style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 12 }}>
        {jobs.length === 0 && !adding && !fetchError && (
          <p className="settings-empty">No scheduled tasks yet. Create one, or ask in chat: “every weekday at 9am, summarize AI news.”</p>
        )}
        {jobs.map((job) => (
          editingId === job.id && editingJob ? (
            <ScheduleForm
              key={job.id}
              initial={scheduleToForm(editingJob)}
              systemTz={systemTz}
              saving={updateMut.isPending}
              onSave={(name, prompt, schedule) => updateMut.mutate({ id: job.id, body: { name, prompt, schedule } })}
              onCancel={() => setEditingId(null)}
            />
          ) : (
            <JobCard
              key={job.id}
              job={job}
              now={now}
              deleteArmed={pendingDelete === job.id}
              busy={toggleMut.isPending || deleteMut.isPending}
              onEdit={() => { setEditingId(job.id); setAdding(false); }}
              onToggle={() => toggleMut.mutate(job.id)}
              onRun={() => runMut.mutate(job.id)}
              onStop={() => stopMut.mutate(job.id)}
              onDelete={() => handleDelete(job.id)}
              onReassign={() => {
                if (!currentSessionId) { addToast({ type: 'info', message: 'Open a session first, then reassign.', duration: 3000 }); return; }
                updateMut.mutate({ id: job.id, body: { session_id: currentSessionId } });
              }}
              onRetry={() => runMut.mutate(job.id)}
            />
          )
        ))}
      </div>
    </div>
  );
};
