import React from 'react';
import { useDockStore } from '@/stores/dockStore';

/**
 * PlanCard: a compact card in the conversation for a plan saved via the
 * create_plan tool. The full plan lives in data/plans/ and opens in the dock's
 * plan panel. "Open" (re)opens it; "Approve & implement" dispatches the approve
 * event that ChatInput turns into an implementation turn.
 */
const btn: React.CSSProperties = {
  font: 'inherit', fontSize: 13, cursor: 'pointer', borderRadius: 6,
  padding: '4px 10px', border: '1px solid var(--border-strong)',
  background: 'transparent', color: 'var(--text-secondary)', flex: '0 0 auto',
};

export const PlanCard: React.FC<{ plan: { id: string; title: string; summary: string } }> = ({ plan }) => {
  const openPanel = useDockStore((s) => s.openPanel);
  const open = () =>
    openPanel({ id: `plan:${plan.id}`, kind: 'plan', title: plan.title || 'Plan', meta: { planId: plan.id } });
  const approve = () =>
    window.dispatchEvent(new CustomEvent('whisper-approve-plan', { detail: { planId: plan.id } }));

  return (
    <div
      style={{
        display: 'flex', alignItems: 'center', gap: 8, width: '100%',
        marginTop: 8, padding: '8px 12px',
        background: 'var(--bg-secondary)', color: 'var(--text-primary)',
        border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
      }}
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: '0 0 auto' }}>
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="9" y1="13" x2="15" y2="13" /><line x1="9" y1="17" x2="15" y2="17" />
      </svg>
      <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-muted)', flex: '0 0 auto' }}>Plan</span>
      <span style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={plan.title}>
        {plan.title}
      </span>
      <span style={{ flex: 1 }} />
      <button type="button" onClick={approve} style={btn} title="Read the saved plan and implement it">
        Approve &amp; implement
      </button>
      <button type="button" onClick={open} style={btn} title="Open the plan in the side pane">
        Open
      </button>
    </div>
  );
};

export default PlanCard;
