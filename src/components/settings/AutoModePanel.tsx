import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get, post } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';
import { toError } from '@/utils/toError';

interface AutoModeRulesResponse {
  effective: {
    allow: string[];
    soft_deny: string[];
    environment: string[];
  };
  defaults: {
    allow: string[];
    soft_deny: string[];
    environment: string[];
  };
  enabled: boolean;
}

async function fetchAutoModeRules(): Promise<AutoModeRulesResponse> {
  return get<AutoModeRulesResponse>('/api/auto-mode/rules');
}

/** Content equality for two string arrays. The effective and default rule
 *  sets are always distinct array instances (they arrive as separate JSON
 *  fields), so a reference check (`a !== b`) is *always* true and would flag
 *  everything as "custom". Compare element by element instead. Exported for
 *  unit testing. */
export const sameArr = (a: string[], b: string[]): boolean =>
  a.length === b.length && a.every((x, i) => x === b[i]);

/**
 * Read-only view of the Auto Mode classifier rules.
 *
 * Auto Mode is the permission mode that lets the LLM decide whether a
 * tool call needs explicit approval, based on three rule sets: allow,
 * soft_deny, and environment. Custom rules in the user's config
 * override the defaults *per section* (an empty user-allow keeps the
 * default allow; a non-empty one replaces it).
 *
 * This panel surfaces the effective rules + the defaults so the user
 * can audit what Auto Mode will let through without having to grep
 * the server config file. Editing happens via config_set in the chat
 * (intentionally — these are sensitive rules that should be reviewed
 * with the LLM rather than tweaked in a quick form).
 */
export const AutoModePanel: React.FC = () => {
  const { data, isLoading, error } = useQuery({
    queryKey: ['auto-mode-rules'],
    queryFn: fetchAutoModeRules,
    staleTime: 30_000,
  });

  // Critique state. Independent of the read-only rules display so a
  // slow LLM round-trip doesn't block scrolling/reading the rules.
  const [critique, setCritique] = useState<string | null>(null);
  const [critiquing, setCritiquing] = useState(false);

  const runCritique = useCallback(async () => {
    setCritiquing(true);
    setCritique(null);
    try {
      const res = await post<{ critique?: string; error?: string }>(
        '/api/auto-mode/critique',
        {},
      );
      if (res.error) {
        useUIStore.getState().addToast({
          type: 'error',
          message: `Critique failed: ${res.error}`,
          duration: 4000,
        });
        setCritique(null);
      } else {
        setCritique(res.critique ?? '(no output)');
      }
    } catch (err) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Critique failed: ${toError(err).message}`,
        duration: 4000,
      });
    } finally {
      setCritiquing(false);
    }
  }, []);

  if (isLoading) {
    return (
      <div className="settings-form" style={{ maxWidth: 720 }}>
        <p className="settings-hint">Loading Auto Mode rules…</p>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="settings-form" style={{ maxWidth: 720 }}>
        <p className="settings-empty" role="alert">
          Could not load Auto Mode rules. Backend may be unavailable.
        </p>
      </div>
    );
  }

  const { effective, defaults, enabled } = data;
  const hasCustom = (
    !sameArr(effective.allow, defaults.allow) ||
    !sameArr(effective.soft_deny, defaults.soft_deny) ||
    !sameArr(effective.environment, defaults.environment)
  );

  return (
    <div className="settings-form" style={{ maxWidth: 720 }}>
      <p className="settings-hint">
        Auto Mode lets the LLM auto-approve tool calls that match the rules
        below. <strong>Current state:</strong>{' '}
        <span style={{ color: enabled ? 'var(--accent)' : 'var(--text-muted)' }}>
          {enabled ? 'enabled' : 'disabled'}
        </span>
        . Edit via <code>config_set</code> in chat.
      </p>

      <RuleSection
        title="Allow"
        description="Tool calls matching these rules run without confirmation."
        rules={effective.allow}
        isCustom={!sameArr(effective.allow, defaults.allow)}
        defaults={defaults.allow}
      />
      <RuleSection
        title="Soft Deny"
        description="Tool calls matching these rules require explicit user confirmation."
        rules={effective.soft_deny}
        isCustom={!sameArr(effective.soft_deny, defaults.soft_deny)}
        defaults={defaults.soft_deny}
      />
      <RuleSection
        title="Environment"
        description="Context hints supplied to the classifier (read-only metadata)."
        rules={effective.environment}
        isCustom={!sameArr(effective.environment, defaults.environment)}
        defaults={defaults.environment}
      />

      {!hasCustom && (
        <p className="settings-hint" style={{ marginTop: 12, fontStyle: 'italic' }}>
          No custom rules configured. The defaults shown above are in effect.
        </p>
      )}

      {hasCustom && (
        <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--border)' }}>
          <div className="settings-hint" style={{ marginBottom: 8 }}>
            Run an LLM critique of your custom rules. It'll flag overlaps,
            gaps, and risky allowances by comparing against the defaults.
          </div>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void runCritique()}
            disabled={critiquing}
          >
            {critiquing ? 'Critiquing…' : 'Critique my rules'}
          </button>
          {critique !== null && (
            <pre style={{
              marginTop: 12,
              padding: '10px 12px',
              border: '1px solid var(--border)',
              borderRadius: 6,
              background: 'var(--bg-primary)',
              fontFamily: 'inherit',
              fontSize: '0.9em',
              lineHeight: 1.5,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              color: 'var(--text-primary)',
              maxHeight: 360,
              overflow: 'auto',
            }}>
              {critique}
            </pre>
          )}
        </div>
      )}
    </div>
  );
};

interface RuleSectionProps {
  title: string;
  description: string;
  rules: string[];
  isCustom: boolean;
  defaults: string[];
}

const RuleSection: React.FC<RuleSectionProps> = ({
  title, description, rules, isCustom, defaults,
}) => {
  return (
    <div className="settings-list" style={{ marginTop: 16 }}>
      <div className="settings-hint" style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        marginBottom: 6,
        fontSize: '0.85em',
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '0.05em',
        color: 'var(--text-secondary)',
      }}>
        {title}
        {isCustom && (
          <span style={{
            fontSize: '0.85em',
            background: 'var(--accent-dim)',
            color: 'var(--accent)',
            padding: '1px 6px',
            borderRadius: 3,
            textTransform: 'none',
            letterSpacing: 0,
            fontWeight: 500,
          }}>
            custom
          </span>
        )}
      </div>
      <div className="settings-hint" style={{ marginBottom: 8 }}>{description}</div>
      {rules.length === 0 ? (
        <div className="settings-empty">No rules in this section.</div>
      ) : (
        <ul style={{
          margin: 0,
          padding: '0 0 0 18px',
          fontSize: '0.9em',
          lineHeight: 1.5,
          color: 'var(--text-primary)',
        }}>
          {rules.map((r, i) => (
            <li key={`${title}-${i}`}><code>{r}</code></li>
          ))}
        </ul>
      )}
      {isCustom && defaults.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: 'pointer', fontSize: '0.85em', color: 'var(--text-muted)' }}>
            View defaults this is replacing
          </summary>
          <ul style={{
            margin: '6px 0 0',
            padding: '0 0 0 18px',
            fontSize: '0.85em',
            lineHeight: 1.5,
            color: 'var(--text-muted)',
          }}>
            {defaults.map((r, i) => (
              <li key={`${title}-default-${i}`}><code>{r}</code></li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
};
