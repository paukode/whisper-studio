import { describe, expect, it } from 'vitest';
import {
  SessionSummarySchema,
  SessionListResponseSchema,
  AppConfigResponseSchema,
  ModelsResponseSchema,
  PermissionsResponseSchema,
  MCPServersResponseSchema,
  SkillsResponseSchema,
  SSEEventDataSchema,
} from './index';

// ── SessionSummarySchema ──

describe('SessionSummarySchema', () => {
  it('parses a valid session summary', () => {
    const input = { id: '123', title: 'Test', date: '2024-01-01', segmentCount: 3, chatCount: 5 };
    const result = SessionSummarySchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.id).toBe('123');
      expect(result.data.segmentCount).toBe(3);
    }
  });

  it('applies defaults for optional fields', () => {
    const result = SessionSummarySchema.safeParse({ id: 'abc' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.title).toBe('Untitled');
      expect(result.data.date).toBe('');
      expect(result.data.segmentCount).toBe(0);
      expect(result.data.chatCount).toBe(0);
    }
  });

  it('rejects missing id', () => {
    const result = SessionSummarySchema.safeParse({ title: 'No ID' });
    expect(result.success).toBe(false);
  });
});

describe('SessionListResponseSchema', () => {
  it('parses an array of session summaries', () => {
    const input = [
      { id: '1', title: 'A' },
      { id: '2', title: 'B', segmentCount: 10 },
    ];
    const result = SessionListResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data).toHaveLength(2);
  });
});

// ── AppConfigResponseSchema ──

describe('AppConfigResponseSchema', () => {
  it('parses a full config response', () => {
    const input = {
      chat_models: { opus: 'Claude Opus' },
      effort_level: 'high',
      brief_mode: true,
      permission_mode: 'plan',
    };
    const result = AppConfigResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.brief_mode).toBe(true);
    }
  });

  it('applies defaults for missing fields', () => {
    const result = AppConfigResponseSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.effort_level).toBe('high');
      expect(result.data.brief_mode).toBe(false);
    }
  });
});

// ── ModelsResponseSchema ──

describe('ModelsResponseSchema', () => {
  it('parses models list', () => {
    const input = { models: [{ key: 'opus', name: 'Claude Opus' }], default: 'opus' };
    const result = ModelsResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.models).toHaveLength(1);
      expect(result.data.default).toBe('opus');
    }
  });
});

// ── PermissionsResponseSchema ──

describe('PermissionsResponseSchema', () => {
  it('parses permission mode', () => {
    const result = PermissionsResponseSchema.safeParse({ mode: 'plan' });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.mode).toBe('plan');
  });

  it('defaults to "default" mode', () => {
    const result = PermissionsResponseSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.mode).toBe('default');
  });
});

// ── MCPServersResponseSchema ──

describe('MCPServersResponseSchema', () => {
  it('parses servers map', () => {
    const input = {
      servers: {
        myServer: { command: 'node', args: ['server.js'], status: 'running' },
      },
    };
    const result = MCPServersResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.servers.myServer.status).toBe('running');
    }
  });
});

// ── SkillsResponseSchema ──

describe('SkillsResponseSchema', () => {
  it('parses skills list', () => {
    const input = {
      skills: [{ name: 'test-skill', enabled: true }],
      mcpTools: [{ name: 'tool1', description: 'A tool', server: 'srv' }],
    };
    const result = SkillsResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.skills).toHaveLength(1);
  });
});

// ── SSEEventDataSchema ──

describe('SSEEventDataSchema', () => {
  it('parses text streaming event', () => {
    const result = SSEEventDataSchema.safeParse({ text: 'Hello' });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.text).toBe('Hello');
  });

  it('parses approval event', () => {
    const input = {
      approval_request: {
        tool_use_id: 'tu_1',
        action: 'file_write',
        category: 'write',
        preview: 'diff',
        summary: 'Write /test.txt',
        payload: { path: '/test.txt', content: 'data' },
      },
    };
    const result = SSEEventDataSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.approval_request?.action).toBe('file_write');
      expect(result.data.approval_request?.preview).toBe('diff');
    }
  });

  it('parses usage event', () => {
    const input = { usage: { input_tokens: 100, output_tokens: 50 } };
    const result = SSEEventDataSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.usage?.input_tokens).toBe(100);
  });

  it('accepts unknown future fields (passthrough)', () => {
    const result = SSEEventDataSchema.safeParse({ text: 'hi', new_field: true });
    expect(result.success).toBe(true);
  });

  it('parses ws_auto_applied event', () => {
    const input = { ws_auto_applied: { path: '/a.ts', content: 'code' } };
    const result = SSEEventDataSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.ws_auto_applied?.path).toBe('/a.ts');
  });

  // Top-level passthrough (above) does NOT extend into nested objects: a plain
  // z.object drops unknown keys. workflow_preview.model_id was being stripped
  // here, so the approval card posted no model and every approved workflow ran
  // — and was priced — on the config default instead of the session's model.
  it('keeps workflow_preview.model_id so the approved run uses the session model', () => {
    const input = {
      workflow_preview: {
        script: 'export const meta = {}',
        name: 'fix-and-verify',
        model_id: 'openai.gpt-5.6-sol',
        budget_tokens: null,
      },
    };
    const result = SSEEventDataSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.workflow_preview?.model_id).toBe('openai.gpt-5.6-sol');
    }
  });
});
