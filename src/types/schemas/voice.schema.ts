import { z } from 'zod';
import { TeamProgressEventSchema } from '@/types/schemas/chat.schema';

/** JSON events the /ws/voice socket sends (audio arrives as binary frames,
 *  not through this schema). Mirrors server/voice/session.py's emit vocabulary. */
export const VoiceServerEventSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('ready'), model_id: z.string(), voice_id: z.string() }),
  z.object({ type: z.literal('state'), state: z.enum(['listening', 'speaking', 'thinking']) }),
  z.object({ type: z.literal('user_transcript'), text: z.string(), typed: z.boolean().optional() }),
  z.object({
    type: z.literal('assistant_text'),
    text: z.string(),
    final: z.boolean(),
    /** Which utterance the text belongs to; one final per utterance. */
    utterance_id: z.string().optional(),
  }),
  z.object({ type: z.literal('interrupted') }),
  z.object({
    type: z.literal('tool_call'),
    tool_use_id: z.string(),
    name: z.string(),
    input: z.record(z.string(), z.unknown()),
  }),
  z.object({
    type: z.literal('assistant_step'),
    /** Which delegated run the step belongs to (several may run at once). */
    run_id: z.string().optional(),
    name: z.string(),
    status: z.enum(['running', 'ok', 'error']),
    detail: z.string(),
    /** Full tool input (agent tools always, others when small) and, for agent
     *  tools, the full result, so the agent cards can anchor and label. */
    input: z.record(z.string(), z.unknown()).optional(),
    output: z.string().optional(),
  }),
  z.object({
    /** Progress of an agent spawned by a delegated run: the chat engine's own
     *  team_progress event, tagged with the run it belongs to. */
    type: z.literal('team_progress'),
    run_id: z.string(),
    event: TeamProgressEventSchema,
  }),
  z.object({
    /** The user hung up while delegated runs were still going: they keep
     *  running and their events keep arriving on this socket until ended. */
    type: z.literal('draining'),
    runs: z.array(z.object({ run_id: z.string(), request: z.string() })),
  }),
  z.object({
    type: z.literal('tool_result'),
    tool_use_id: z.string(),
    name: z.string(),
    output: z.string(),
    /** paused: the delegated assistant stopped for the user's decision;
     *  working: it continues in the background. In both the output is
     *  guidance for Sonic, not an answer to show; answers arrive as
     *  assistant_answer. */
    status: z.enum(['ok', 'error', 'paused', 'working']),
  }),
  z.object({
    /** A delegated run's written answer, whenever it lands (quick or after
     *  minutes in the background). */
    type: z.literal('assistant_answer'),
    run_id: z.string(),
    request: z.string(),
    output: z.string(),
    /** stopped: the run was cancelled; the output is a marker, the steps the trace. */
    status: z.enum(['ok', 'error', 'stopped']),
  }),
  z.object({ type: z.literal('client_action'), action: z.string(), value: z.string() }),
  z.object({
    type: z.literal('assistant_request'),
    kind: z.enum(['approval_request', 'user_question', 'workspace_prompt']),
    run_id: z.string().optional(),
    tool_use_id: z.string(),
    action: z.string(),
    category: z.string(),
    summary: z.string(),
    question: z.string(),
    options: z.array(z.string()),
    risk_hint: z.string().nullable().optional(),
    detail: z.string(),
  }),
  z.object({ type: z.literal('assistant_request_resolved'), tool_use_id: z.string(), decision: z.string() }),
  z.object({
    type: z.literal('usage'),
    input_speech: z.number(),
    input_text: z.number(),
    output_speech: z.number(),
    output_text: z.number(),
    total_tokens: z.number(),
  }),
  z.object({ type: z.literal('renewing') }),
  z.object({ type: z.literal('renewed') }),
  z.object({ type: z.literal('error'), message: z.string() }),
  z.object({ type: z.literal('ended'), reason: z.string() }),
  z.object({ type: z.literal('pong') }),
]);

export type VoiceServerEvent = z.infer<typeof VoiceServerEventSchema>;

export const VoiceOptionSchema = z.object({
  id: z.string(),
  label: z.string(),
  locale: z.string(),
  polyglot: z.string(),
});

/** GET /api/voice/status */
export const VoiceStatusSchema = z.object({
  available: z.boolean(),
  reason: z.string().nullable().optional(),
  model_id: z.string(),
  region: z.string(),
  voice_id: z.string(),
  endpointing: z.string(),
  voices: z.array(VoiceOptionSchema),
});

export type VoiceStatusResponse = z.infer<typeof VoiceStatusSchema>;
