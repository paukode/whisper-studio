import { describe, expect, it } from 'vitest';
import {
  ErrorResponseSchema,
  SessionSummarySchema,
  SessionListResponseSchema,
  AppConfigResponseSchema,
  ModelsResponseSchema,
  PermissionsResponseSchema,
  MCPServersResponseSchema,
  SkillsResponseSchema,
  SSEEventDataSchema,
  FileTreeEntrySchema,
  ListDirResponseSchema,
  RecentWorkspacesResponseSchema,
  BuddyGetResponseSchema,
} from './index';

// ── ErrorResponseSchema ──

describe('ErrorResponseSchema', () => {
  it('parses error with detail field', () => {
    const result = ErrorResponseSchema.safeParse({ detail: 'Not found' });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.detail).toBe('Not found');
  });

  it('parses error with message field', () => {
    const result = ErrorResponseSchema.safeParse({ message: 'Server error' });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.message).toBe('Server error');
  });

  it('accepts unknown extra fields (passthrough)', () => {
    const result = ErrorResponseSchema.safeParse({ detail: 'err', code: 42 });
    expect(result.success).toBe(true);
  });
});

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
      bedrock_region: 'us-west-2',
      chat_models: { opus: 'Claude Opus' },
      default_chat_model: 'opus',
      effort_level: 'high',
      brief_mode: true,
      permission_mode: 'plan',
      auto_mode: true,
    };
    const result = AppConfigResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.bedrock_region).toBe('us-west-2');
      expect(result.data.brief_mode).toBe(true);
    }
  });

  it('applies defaults for missing fields', () => {
    const result = AppConfigResponseSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.bedrock_region).toBe('us-east-1');
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
});

// ── FileTreeEntrySchema ──

describe('FileTreeEntrySchema', () => {
  it('parses a file entry', () => {
    const result = FileTreeEntrySchema.safeParse({ name: 'index.ts', path: 'src/index.ts', type: 'file' });
    expect(result.success).toBe(true);
  });

  it('parses a directory with children', () => {
    const input = {
      name: 'src',
      path: 'src',
      type: 'directory',
      children: [
        { name: 'index.ts', path: 'src/index.ts', type: 'file' },
      ],
    };
    const result = FileTreeEntrySchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.children).toHaveLength(1);
  });

  it('rejects invalid type', () => {
    const result = FileTreeEntrySchema.safeParse({ name: 'x', path: 'x', type: 'symlink' });
    expect(result.success).toBe(false);
  });
});

describe('ListDirResponseSchema', () => {
  it('parses list-dir response', () => {
    const input = {
      entries: [
        { name: 'a.ts', path: 'a.ts', type: 'file' },
        { name: 'lib', path: 'lib', type: 'directory' },
      ],
    };
    const result = ListDirResponseSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.entries).toHaveLength(2);
  });
});

// ── RecentWorkspacesResponseSchema ──

describe('RecentWorkspacesResponseSchema', () => {
  it('parses recent workspaces', () => {
    const result = RecentWorkspacesResponseSchema.safeParse({ workspaces: ['/home/user/project'] });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.workspaces).toHaveLength(1);
  });

  it('defaults to empty array', () => {
    const result = RecentWorkspacesResponseSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.workspaces).toEqual([]);
  });
});

// ── BuddyGetResponseSchema ──

describe('BuddyGetResponseSchema', () => {
  it('parses buddy state', () => {
    const result = BuddyGetResponseSchema.safeParse({ state: 'happy', animation: 'bounce' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.state).toBe('happy');
      expect(result.data.animation).toBe('bounce');
    }
  });

  it('defaults state to idle', () => {
    const result = BuddyGetResponseSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.state).toBe('idle');
  });
});
