/**
 * Team-report progress folding for the chat stream.
 *
 * Pure helpers: they translate the server's team_progress / team_results
 * SSE side-effects into a teamReports map. The map-level fold
 * (foldTeamProgressIntoMap) runs against the turn-local live report in the
 * chat store while the assistant message does not exist yet; the
 * message-level helpers serve the background /subagent stream, which
 * pre-creates its message and targets it by timestamp. No React here.
 */
import type { StoreGetter } from '@/stores/chatStore';
import type {
  ChatMessage,
  TeamAgentReport,
  TeamProgressEvent,
  TeamReportData,
  ToolUseEvent,
} from '@/types/chat';

/** Stable key under which an agent's progress is filed within a team report.
 *  Prefer agent_name (stable across the whole run, known from team_started).
 *  Fall back to agent_id (only known after the agent's `started` event). */
function _agentKey(ev: TeamProgressEvent): string | null {
  return (ev.agent_name ?? ev.agent_id) || null;
}

function _phaseToStatus(phase: TeamProgressEvent['phase']): TeamAgentReport['status'] {
  if (phase === 'completed') return 'completed';
  if (phase === 'turn_limit') return 'turn_limit';
  if (phase === 'failed') return 'failed';
  if (phase === 'stopped') return 'stopped';
  if (phase === 'started' || phase === 'turn_start' || phase === 'text'
      || phase === 'tool_call' || phase === 'tool_result') return 'running';
  return 'pending';
}

/** Upper bound on retained events per agent. Protects the store (and the
 *  persisted chat_history blob) from unbounded growth on chatty agents;
 *  the newest events win because the tail is what the user is watching. */
const MAX_EVENTS_PER_AGENT = 400;

/** Merge the final team_results side-effect payload into the rolling
 *  teamReports map. The side-effect carries the structured shape produced
 *  by execute_team_create:
 *    { team_id, team_name, description, agents: [{name, agent_id, agent_type,
 *      task, result, status, turns_used}, ...] }
 *  We mark the team as completed and attach each agent's terminal `result`
 *  text so the card expands to show the full finding.
 */
export function foldTeamResultsInto(
  prior: Record<string, TeamReportData> | undefined,
  payload: Record<string, unknown> | unknown,
): Record<string, TeamReportData> {
  const out: Record<string, TeamReportData> = { ...(prior ?? {}) };
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return out;
  const p = payload as {
    team_id?: string;
    team_name?: string;
    description?: string;
    agents?: Array<{
      name?: string;
      agent_id?: string;
      agent_type?: string;
      task?: string;
      result?: string;
      status?: string;
      turns_used?: number;
    }>;
  };
  const tid = p.team_id ?? '';
  if (!tid) return out;

  const prev = out[tid];
  const agents: Record<string, TeamAgentReport> = { ...(prev?.agents ?? {}) };
  const agentOrder: string[] = [...(prev?.agentOrder ?? [])];

  for (const a of p.agents ?? []) {
    const key = a.name ?? a.agent_id ?? '';
    if (!key) continue;
    const existing = agents[key];
    const merged: TeamAgentReport = existing
      ? {
          ...existing,
          agent_id: a.agent_id ?? existing.agent_id,
          agent_type: a.agent_type ?? existing.agent_type,
          turns_used: a.turns_used ?? existing.turns_used,
          result: a.result ?? existing.result,
          status:
            a.status === 'completed' ? 'completed'
            : a.status === 'failed' || a.status === 'error' ? 'failed'
            : existing.status,
        }
      : {
          name: a.name ?? a.agent_id ?? key,
          task: a.task ?? '',
          agent_type: a.agent_type ?? 'general',
          role: a.agent_type === 'coordinator' ? 'orchestrator' : 'team',
          status:
            a.status === 'completed' ? 'completed'
            : a.status === 'failed' || a.status === 'error' ? 'failed'
            : 'completed',
          agent_id: a.agent_id,
          turns_used: a.turns_used,
          result: a.result,
          events: [],
        };
    agents[key] = merged;
    if (!agentOrder.includes(key)) agentOrder.push(key);
  }

  out[tid] = {
    team_id: tid,
    team_name: p.team_name ?? prev?.team_name ?? '',
    description: p.description ?? prev?.description,
    status: 'completed',
    // Keep the live-fold timestamps — this payload arrives after
    // team_completed and must not wipe the elapsed display.
    started_at: prev?.started_at,
    completed_at: prev?.completed_at ?? (prev?.started_at ? Date.now() : undefined),
    agents,
    agentOrder,
  };
  return out;
}

/** Fold a single team_progress event into a teamReports map. Pure: returns
 *  a NEW map, or null if the event should be ignored (e.g. a non-team_started
 *  event with no team_id). This is the core reducer; it deliberately has no
 *  notion of messages so it can run against the turn-local live report in
 *  the chat store (while the assistant message doesn't exist yet) as well as
 *  against a committed message's map. */
export function foldTeamProgressIntoMap(
  prior: Record<string, TeamReportData> | undefined,
  ev: TeamProgressEvent,
): Record<string, TeamReportData> | null {
  const teamId = ev.team_id ?? '';
  if (!teamId && ev.phase !== 'team_started') return null;

  const teamReports: Record<string, TeamReportData> = { ...(prior ?? {}) };

  // team_started: create the scaffold.
  if (ev.phase === 'team_started') {
    const tid = ev.team_id ?? '';
    if (!tid) return null;
    const agents: Record<string, TeamAgentReport> = {};
    const agentOrder: string[] = [];
    for (const a of ev.agents ?? []) {
      const key = a.name;
      agents[key] = {
        name: a.name,
        task: a.task,
        agent_type: a.agent_type,
        role: a.role ?? 'team',
        status: 'pending',
        events: [],
      };
      agentOrder.push(key);
    }
    teamReports[tid] = {
      team_id: tid,
      team_name: ev.team_name ?? '',
      description: ev.description,
      status: 'running',
      started_at: Date.now(),
      agents,
      agentOrder,
    };
    return teamReports;
  }

  const existing = teamReports[teamId];
  if (!existing) {
    // event arrived before (or without) a team_started — synthesize a minimal
    // scaffold so we don't lose the data.
    teamReports[teamId] = {
      team_id: teamId,
      team_name: ev.team_name ?? '',
      description: ev.description,
      status: 'running',
      started_at: Date.now(),
      agents: {},
      agentOrder: [],
    };
  }

  // team_completed: just flip the status, keep agents untouched.
  if (ev.phase === 'team_completed') {
    const tr = teamReports[teamId];
    teamReports[teamId] = { ...tr, status: 'completed', completed_at: Date.now() };
    return teamReports;
  }

  // Per-agent event. Find (or create) the agent row.
  const tr = teamReports[teamId];
  const key = _agentKey(ev);
  if (!key) return null;
  const agents = { ...tr.agents };
  const agentOrder = [...tr.agentOrder];
  let agent = agents[key];
  if (!agent) {
    agent = {
      name: ev.agent_name ?? ev.agent_id ?? key,
      task: ev.task ?? '',
      agent_type: ev.agent_type ?? 'general',
      role: ev.agent_type === 'coordinator' ? 'orchestrator' : 'team',
      status: 'pending',
      events: [],
    };
    agents[key] = agent;
    agentOrder.push(key);
  }

  const events = [...agent.events, ev].slice(-MAX_EVENTS_PER_AGENT);
  const merged: TeamAgentReport = {
    ...agent,
    agent_id: ev.agent_id ?? agent.agent_id,
    agent_type: ev.agent_type ?? agent.agent_type,
    model: ev.model ?? agent.model,
    // A spawned child announces its parent on every event; latch it so the
    // card can badge the row even if a later event omits the field.
    parent_agent_id: ev.parent_agent_id ?? agent.parent_agent_id,
    // The started event carries the (full) task for rows that were not
    // pre-announced by team_started — spawned children in particular.
    task: agent.task || (ev.task ?? ''),
    status: _phaseToStatus(ev.phase),
    turns_used: ev.turns_used ?? agent.turns_used,
    events,
  };
  agents[key] = merged;
  teamReports[teamId] = { ...tr, agents, agentOrder };
  return teamReports;
}

/** Fold a single team_progress event into ONE message's teamReports map.
 *  Pure: returns an updated ChatMessage, or null if the event should be
 *  ignored for this message. Used by applyTeamProgressToMessage (the
 *  background /subagent stream, which pre-creates its assistant message). */
function foldTeamProgressEvent(message: ChatMessage, ev: TeamProgressEvent): ChatMessage | null {
  const folded = foldTeamProgressIntoMap(message.teamReports, ev);
  return folded ? { ...message, teamReports: folded } : null;
}

/** Mirror of the backend `_spawn_label` (server/agent_tools/spawn.py): first
 *  line of the task, capped at 60 chars with an ellipsis. Used to match a
 *  spawn_agent tool to its one-member team report while the tool is still
 *  running (before its result JSON carries the team_id). */
export function spawnLabel(task: string, maxLen = 60): string {
  const first = (task ?? '').trim().split('\n')[0]?.trim() || 'agent';
  return first.length <= maxLen ? first : first.slice(0, maxLen - 1).trimEnd() + '…';
}

/** All team reports produced by an agent-tool group, in tool order. Matches
 *  team_create AND inline spawn_agent (each inline spawn is a one-member team
 *  since PR #223): prefer the exact team_id from the tool's result JSON, fall
 *  back to a name match (team_name for team_create; the spawn label derived
 *  from the task for spawn_agent) while the tool is still running. Shared by
 *  ChatMessage (committed) and StreamingMessage (live). */
export function findMatchingTeamReports(
  tools: ToolUseEvent[],
  teamReports: Record<string, TeamReportData> | undefined,
): TeamReportData[] {
  if (!teamReports || Object.keys(teamReports).length === 0) return [];
  const matched: TeamReportData[] = [];
  const seen = new Set<string>();
  const add = (r: TeamReportData | undefined) => {
    if (r && !seen.has(r.team_id)) {
      seen.add(r.team_id);
      matched.push(r);
    }
  };
  for (const t of tools) {
    const isTeam = t.toolName === 'team_create';
    const isSpawn = t.toolName === 'spawn_agent';
    if (!isTeam && !isSpawn) continue;
    // Detached spawns have no scaffold — they render as background-task cards.
    if (isSpawn && (t.input?.detach as boolean | undefined)) continue;
    if (typeof t.result === 'string' && t.result.length > 0) {
      try {
        const parsed = JSON.parse(t.result) as { team_id?: string };
        if (parsed.team_id && teamReports[parsed.team_id]) {
          add(teamReports[parsed.team_id]);
          continue;
        }
      } catch {
        // not parseable yet — fall through to name match
      }
    }
    const name = isTeam
      ? ((t.input?.team_name as string | undefined) ?? '')
      : spawnLabel((t.input?.task as string | undefined) ?? '');
    if (!name) continue;
    add(Object.values(teamReports).find((r) => r.team_name === name && !seen.has(r.team_id)));
  }
  return matched;
}

/** First matching report (legacy singular shape); see findMatchingTeamReports. */
export function findMatchingTeamReport(
  tools: ToolUseEvent[],
  teamReports: Record<string, TeamReportData> | undefined,
): TeamReportData | null {
  return findMatchingTeamReports(tools, teamReports)[0] ?? null;
}

/** Apply a team_progress event to a SPECIFIC assistant message identified by
 *  timestamp. Used by the background /subagent stream, whose progress card
 *  must keep updating even after the user sends more messages (so it's no
 *  longer the last message). */
export function applyTeamProgressToMessage(
  storeGetter: StoreGetter,
  targetTimestamp: string,
  ev: TeamProgressEvent,
): void {
  const st = storeGetter();
  const msgs = st.messages;
  // Match the ASSISTANT message specifically: the /subagent handler adds a
  // user + assistant message that can share a millisecond timestamp, and a
  // bare timestamp match would otherwise land on the user message and bail.
  const idx = msgs.findIndex((m) => m.timestamp === targetTimestamp && m.role === 'assistant');
  if (idx < 0) return;
  const target = msgs[idx];
  const updated = foldTeamProgressEvent(target, ev);
  if (!updated) return;
  const next = [...msgs];
  next[idx] = updated;
  st.setMessages(next);
}
