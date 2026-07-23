import React, { useState, useEffect, useRef, useCallback } from 'react';
import { get, post, put } from '@/api/client';
import { dialogConfirm } from '@/stores/uiStore';
import { pickFact } from '@/components/common/buddyFacts';

const AI_FACTS_KEY = 'buddy_ai_facts';

/* ── Backend shape (GET/POST /api/buddy) ── */
interface Bones {
  rarity: 'common' | 'uncommon' | 'rare' | 'epic' | 'legendary';
  species: string;
  eye: 'dot' | 'star' | 'cross' | 'ring' | 'spiral' | 'sleepy';
  hat: string;
  shiny: boolean;
  stats: Record<string, number>;
}
interface BuddyResponse {
  hatched: boolean;
  name?: string;
  personality?: string;
  bones: Bones;
  color: string;
  stars: string;
}

interface BuddyState { hidden: boolean; x: number; y: number; fw: number; fh: number; }

const STORAGE_KEY = 'buddy_state';

function loadBuddyState(): BuddyState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const p: Partial<BuddyState> = JSON.parse(raw);
      return {
        hidden: p.hidden ?? false,
        x: typeof p.x === 'number' ? p.x : -1,
        y: typeof p.y === 'number' ? p.y : -1,
        fw: typeof p.fw === 'number' ? p.fw : -1,
        fh: typeof p.fh === 'number' ? p.fh : -1,
      };
    }
  } catch { /* ignore */ }
  return { hidden: false, x: -1, y: -1, fw: -1, fh: -1 };
}
function saveBuddyState(s: BuddyState): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch { /* ignore */ }
}

/* ── SVG sub-parts, parameterized by the deterministic roll ── */

const EAR_SPECIES: Record<string, 'cat' | 'bunny' | 'horns' | 'bird' | 'antenna' | 'arms'> = {
  cat: 'cat', rabbit: 'bunny', dragon: 'horns',
  owl: 'bird', duck: 'bird', goose: 'bird', penguin: 'bird',
  robot: 'antenna', cactus: 'arms',
};

function Ears({ species, color }: { species: string; color: string }) {
  const kind = EAR_SPECIES[species];
  if (kind === 'cat') return <><path d="M20 18 L16 6 L28 14 Z" fill={color} /><path d="M44 18 L48 6 L36 14 Z" fill={color} /></>;
  if (kind === 'bunny') return <><ellipse cx="25" cy="8" rx="4" ry="12" fill={color} /><ellipse cx="39" cy="8" rx="4" ry="12" fill={color} /></>;
  if (kind === 'horns') return <><path d="M22 14 L18 4 L27 11 Z" fill="#f1e6c4" /><path d="M42 14 L46 4 L37 11 Z" fill="#f1e6c4" /></>;
  if (kind === 'bird') return <><path d="M30 8 q2 -5 4 0 q2 -5 4 0 Z" fill={color} /></>;
  if (kind === 'antenna') return <><line x1="32" y1="14" x2="32" y2="4" stroke={color} strokeWidth="2" /><circle cx="32" cy="3" r="2.5" fill="var(--accent)" /></>;
  if (kind === 'arms') return <><path d="M14 38 q-6 0 -6 -8" fill="none" stroke={color} strokeWidth="4" strokeLinecap="round" /><path d="M50 38 q6 0 6 -8" fill="none" stroke={color} strokeWidth="4" strokeLinecap="round" /></>;
  return null;
}

function Eye({ cx, type }: { cx: number; type: Bones['eye'] }) {
  const cy = 34;
  const dark = '#1c1a1f';
  switch (type) {
    case 'star':
      return <path d={`M${cx} ${cy - 4} l1.2 2.6 2.8.3 -2.1 1.9 .6 2.8 -2.5 -1.4 -2.5 1.4 .6 -2.8 -2.1 -1.9 2.8 -.3 z`} fill={dark} />;
    case 'cross':
      return <g stroke={dark} strokeWidth="1.6" strokeLinecap="round"><line x1={cx - 2.5} y1={cy - 2.5} x2={cx + 2.5} y2={cy + 2.5} /><line x1={cx + 2.5} y1={cy - 2.5} x2={cx - 2.5} y2={cy + 2.5} /></g>;
    case 'ring':
      return <circle cx={cx} cy={cy} r="3" fill="none" stroke={dark} strokeWidth="1.6" />;
    case 'spiral':
      return <path d={`M${cx} ${cy} m-3 0 a3 3 0 1 1 3 3 a1.6 1.6 0 1 1 -1.6 -1.6`} fill="none" stroke={dark} strokeWidth="1.4" />;
    case 'sleepy':
      return <path d={`M${cx - 3} ${cy} q3 3 6 0`} fill="none" stroke={dark} strokeWidth="1.8" strokeLinecap="round" />;
    default:
      return <><circle cx={cx} cy={cy} r="3" fill={dark} /><circle cx={cx + 1} cy={cy - 1} r="1" fill="#fff" /></>;
  }
}

function Hat({ hat }: { hat: string }) {
  switch (hat) {
    case 'crown': return <path d="M24 14 L24 6 L29 11 L32 4 L35 11 L40 6 L40 14 Z" fill="#ffd43b" stroke="#e8a90a" strokeWidth="0.5" />;
    case 'tophat': return <><rect x="26" y="2" width="12" height="11" rx="1" fill="#222" /><rect x="22" y="12" width="20" height="3" rx="1.5" fill="#222" /></>;
    case 'wizard': return <><path d="M32 0 L24 15 L40 15 Z" fill="#5c4bd6" /><circle cx="29" cy="11" r="1" fill="#ffd43b" /><circle cx="35" cy="9" r="0.8" fill="#ffd43b" /></>;
    case 'beanie': return <><path d="M22 14 a10 8 0 0 1 20 0 Z" fill="#e24b4a" /><rect x="21" y="13" width="22" height="3" rx="1.5" fill="#b5352f" /><circle cx="32" cy="4" r="2.5" fill="#fff" /></>;
    case 'halo': return <ellipse className="buddy-halo" cx="32" cy="6" rx="9" ry="2.6" fill="none" stroke="#ffe066" strokeWidth="2" />;
    case 'propeller': return <><line x1="32" y1="13" x2="32" y2="6" stroke="#888" strokeWidth="1.5" /><g className="buddy-propeller" style={{ transformOrigin: '32px 5px' }}><rect x="24" y="4" width="16" height="2" rx="1" fill="var(--accent)" /></g></>;
    case 'tinyduck': return <><ellipse cx="32" cy="9" rx="5" ry="4" fill="#ffd43b" /><circle cx="34" cy="8" r="0.8" fill="#222" /><path d="M37 9 l3 1 -3 1 Z" fill="#f08c00" /></>;
    default: return null;
  }
}

function BuddyCreature({ bones, color, petting }: { bones: Bones; color: string; petting: boolean }) {
  return (
    <svg
      className={`buddy-creature${petting ? ' petting' : ''}`}
      data-rarity={bones.rarity}
      data-shiny={bones.shiny ? '1' : '0'}
      width="72" height="72" viewBox="0 0 64 64"
      aria-hidden="true"
    >
      <ellipse className="buddy-shadow" cx="32" cy="58" rx="14" ry="3" fill="rgba(0,0,0,0.18)" />
      <g className="buddy-bob">
        <Ears species={bones.species} color={color} />
        <Hat hat={bones.hat} />
        {/* body */}
        <path d="M14 36 a18 18 0 0 1 36 0 q0 16 -18 16 q-18 0 -18 -16 Z" fill={color} />
        {/* cheeks */}
        <circle cx="20" cy="40" r="2.4" fill="#fff" opacity="0.28" />
        <circle cx="44" cy="40" r="2.4" fill="#fff" opacity="0.28" />
        {/* eyes (blink wraps them) */}
        <g className="buddy-eyes">
          <Eye cx={26} type={bones.eye} />
          <Eye cx={38} type={bones.eye} />
        </g>
        {/* mouth */}
        <path d="M29 41 q3 3 6 0" fill="none" stroke="#1c1a1f" strokeWidth="1.4" strokeLinecap="round" />
        {/* shiny sparkles */}
        {bones.shiny && (
          <g className="buddy-sparkles" fill="#fff">
            <path className="s1" d="M12 18 l1 2 2 1 -2 1 -1 2 -1 -2 -2 -1 2 -1 z" />
            <path className="s2" d="M52 24 l.8 1.6 1.6.8 -1.6.8 -.8 1.6 -.8 -1.6 -1.6 -.8 1.6 -.8 z" />
          </g>
        )}
      </g>
    </svg>
  );
}

export const BuddyWidget: React.FC = () => {
  const [buddy, setBuddy] = useState<BuddyResponse | null>(null);
  const [hidden, setHidden] = useState(() => loadBuddyState().hidden);
  // Speech bubble. `fact: true` styles it as a "did you know?" card; false is a
  // short pet acknowledgement.
  const [speech, setSpeech] = useState<{ text: string; fact: boolean } | null>(null);
  const [petting, setPetting] = useState(false);
  const [showCard, setShowCard] = useState(false);
  // Opt-in: when on, click pulls a fresh AI fact (/api/buddy/fact) instead of
  // one from the curated local pack. Persisted, default OFF (no network).
  const [aiFacts, setAiFacts] = useState(() => {
    try { return localStorage.getItem(AI_FACTS_KEY) === '1'; } catch { return false; }
  });
  // companion feature flag, null until resolved; the widget is dormant when false.
  const [companionEnabled, setCompanionEnabled] = useState<boolean | null>(null);
  const [position, setPosition] = useState<{ right: number; bottom: number }>(() => {
    const s = loadBuddyState();
    return { right: s.x >= 0 ? s.x : 20, bottom: s.y >= 0 ? s.y : 96 };
  });
  // User-resizable fact-bubble size, persisted. null = use the CSS default
  // until the user drags the bubble's corner grip.
  const [factSize, setFactSize] = useState<{ w: number; h: number } | null>(() => {
    const s = loadBuddyState();
    return s.fw > 0 && s.fh > 0 ? { w: s.fw, h: s.fh } : null;
  });

  const dragging = useRef(false);
  // Whole-body drag uses a small threshold so a click (pet + fact) is never
  // mistaken for a drag. pendingDrag arms on mousedown; movedRef records that a
  // real drag happened so the trailing click is suppressed.
  const pendingDrag = useRef<{ x: number; y: number } | null>(null);
  const movedRef = useRef(false);
  const dragStart = useRef({ mouseX: 0, mouseY: 0, right: 20, bottom: 96 });
  const speechTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const petTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastFact = useRef<string | undefined>(undefined);
  const mountedRef = useRef(true);
  // Fact-bubble resize state. The grip lives at the bubble's top-left, so a
  // drag up/left enlarges it (the widget is bottom-right anchored).
  const resizing = useRef(false);
  const resizeStart = useRef({ mouseX: 0, mouseY: 0, w: 0, h: 0 });
  const bubbleRef = useRef<HTMLDivElement | null>(null);

  /* Resolve the companion flag on mount. */
  useEffect(() => {
    mountedRef.current = true;
    void (async () => {
      try {
        const flags = await get<Record<string, { enabled?: boolean }>>('/api/feature-flags');
        if (mountedRef.current) setCompanionEnabled(!!flags?.companion?.enabled);
      } catch {
        if (mountedRef.current) setCompanionEnabled(false);
      }
    })();
    return () => { mountedRef.current = false; };
  }, []);

  /* Load (and auto-hatch) the buddy once enabled. No polling; the creature
   *  animates entirely in CSS, so this fires exactly once. */
  useEffect(() => {
    if (!companionEnabled) return;
    void (async () => {
      try {
        let data = await get<BuddyResponse>('/api/buddy');
        if (!data.hatched) {
          data = await post<BuddyResponse>('/api/buddy/hatch', {});
        }
        if (mountedRef.current) setBuddy({ ...data, hatched: true });
      } catch { /* buddy API unavailable, stay dormant */ }
    })();
  }, [companionEnabled]);

  const enableCompanion = useCallback(() => {
    void (async () => {
      const ok = await dialogConfirm({
        title: 'Show the companion?',
        message:
          'A small creature lives in the corner of the UI. Purely cosmetic, with ' +
          'no AI calls and no effect on your chats. It’s unique to this machine. ' +
          'Toggle it off any time.',
        confirmText: 'Show it',
      });
      if (ok !== true) return;
      try {
        await put('/api/feature-flags/companion', { enabled: true });
        setCompanionEnabled(true);
        setHidden(false);
        saveBuddyState({ ...loadBuddyState(), hidden: false });
      } catch (err) { console.warn('Failed to enable companion:', err); }
    })();
  }, []);

  const handleToggleClick = useCallback(() => {
    if (companionEnabled === false) { enableCompanion(); return; }
    setHidden((prev) => {
      const next = !prev;
      saveBuddyState({ ...loadBuddyState(), hidden: next });
      return next;
    });
  }, [companionEnabled, enableCompanion]);

  /* Persist the AI-facts opt-in. */
  useEffect(() => {
    try { localStorage.setItem(AI_FACTS_KEY, aiFacts ? '1' : '0'); } catch { /* ignore */ }
  }, [aiFacts]);

  /* (Re)arm the auto-dismiss for a shown fact. Hovering or resizing the bubble
   *  pauses it so the fact never vanishes mid-read. */
  const scheduleClose = useCallback((ms: number) => {
    if (speechTimer.current) clearTimeout(speechTimer.current);
    speechTimer.current = setTimeout(() => setSpeech(null), ms);
  }, []);

  /* Click = pet + fact. The bounce is the "pet"; the bubble is the fact. Facts
   *  come from the curated local pack by default; with the opt-in toggle on, a
   *  fresh AI fact is fetched (falling back to the pack on any error). */
  const revealFact = useCallback(async () => {
    if (!buddy) return;
    setPetting(true);
    if (petTimer.current) clearTimeout(petTimer.current);
    petTimer.current = setTimeout(() => setPetting(false), 500);

    let fact = pickFact(lastFact.current);
    if (aiFacts) {
      try {
        const res = await get<{ fact?: string }>('/api/buddy/fact');
        if (res?.fact) fact = res.fact;
      } catch { /* fall back to the curated pack */ }
    }
    if (!mountedRef.current) return;
    lastFact.current = fact;
    setSpeech({ text: fact, fact: true });
    scheduleClose(8000);
  }, [buddy, aiFacts, scheduleClose]);

  /* Begin a fact-bubble resize from the corner grip. Seeds from the live box
   *  so it works even before any size has been persisted. */
  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const rect = bubbleRef.current?.getBoundingClientRect();
    resizeStart.current = {
      mouseX: e.clientX,
      mouseY: e.clientY,
      w: Math.round(rect?.width ?? factSize?.w ?? 280),
      h: Math.round(rect?.height ?? factSize?.h ?? 80),
    };
    resizing.current = true;
    if (speechTimer.current) clearTimeout(speechTimer.current); // never close mid-resize
    document.body.style.userSelect = 'none';
  }, [factSize]);

  /* A trailing click fired at the end of a real drag is suppressed. */
  const handleCreatureClick = useCallback(() => {
    if (movedRef.current) { movedRef.current = false; return; }
    void revealFact();
  }, [revealFact]);

  /* Drag handling. */

  /* Explicit grip handle — drags immediately. */
  const handleDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragging.current = true;
    movedRef.current = false;
    dragStart.current = { mouseX: e.clientX, mouseY: e.clientY, right: position.right, bottom: position.bottom };
    document.body.style.userSelect = 'none';
  }, [position]);

  /* Whole-body drag — arms a pending drag that only engages past a small
   *  threshold, so a plain click still pets + reveals a fact. */
  const handleBodyMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    pendingDrag.current = { x: e.clientX, y: e.clientY };
    movedRef.current = false;
    dragStart.current = { mouseX: e.clientX, mouseY: e.clientY, right: position.right, bottom: position.bottom };
  }, [position]);

  useEffect(() => {
    const DRAG_THRESHOLD = 5;
    const move = (e: MouseEvent) => {
      // Resizing the fact bubble takes priority over widget drag.
      if (resizing.current) {
        const MIN_W = 200, MIN_H = 44;
        const maxW = Math.round(window.innerWidth * 0.8);
        const maxH = Math.round(window.innerHeight * 0.6);
        const w = Math.min(maxW, Math.max(MIN_W, resizeStart.current.w + (resizeStart.current.mouseX - e.clientX)));
        const h = Math.min(maxH, Math.max(MIN_H, resizeStart.current.h + (resizeStart.current.mouseY - e.clientY)));
        setFactSize({ w, h });
        return;
      }
      // Promote a pending whole-body press into a drag once it moves enough.
      if (pendingDrag.current && !dragging.current) {
        const adx = Math.abs(e.clientX - pendingDrag.current.x);
        const ady = Math.abs(e.clientY - pendingDrag.current.y);
        if (adx > DRAG_THRESHOLD || ady > DRAG_THRESHOLD) {
          dragging.current = true;
          document.body.style.userSelect = 'none';
        }
      }
      if (!dragging.current) return;
      movedRef.current = true;
      const dx = dragStart.current.mouseX - e.clientX;
      const dy = dragStart.current.mouseY - e.clientY;
      setPosition({ right: Math.max(0, dragStart.current.right + dx), bottom: Math.max(0, dragStart.current.bottom + dy) });
    };
    const up = () => {
      if (resizing.current) {
        resizing.current = false;
        document.body.style.userSelect = '';
        setFactSize((sz) => { if (sz) saveBuddyState({ ...loadBuddyState(), fw: sz.w, fh: sz.h }); return sz; });
        // Resume the auto-dismiss now that the user has stopped resizing.
        if (speechTimer.current) clearTimeout(speechTimer.current);
        speechTimer.current = setTimeout(() => setSpeech(null), 3500);
        return;
      }
      pendingDrag.current = null;
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.userSelect = '';
      setPosition((pos) => { saveBuddyState({ ...loadBuddyState(), x: pos.right, y: pos.bottom }); return pos; });
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    return () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); };
  }, []);

  useEffect(() => () => {
    if (speechTimer.current) clearTimeout(speechTimer.current);
    if (petTimer.current) clearTimeout(petTimer.current);
  }, []);

  const showBody = !!buddy && !hidden && companionEnabled === true;

  return (
    <>
      {showBody && buddy && (
        <div
          className="buddy-widget"
          style={{ position: 'fixed', right: position.right, bottom: position.bottom, zIndex: 1000 }}
          onMouseEnter={() => setShowCard(true)}
          onMouseLeave={() => setShowCard(false)}
        >
          <button
            className="buddy-drag-handle"
            title="Drag to move"
            aria-label="Drag companion"
            onMouseDown={handleDragStart}
            type="button"
          >
            <svg width="14" height="8" viewBox="0 0 14 8" fill="currentColor" aria-hidden="true">
              <circle cx="3" cy="2" r="1" /><circle cx="7" cy="2" r="1" /><circle cx="11" cy="2" r="1" />
              <circle cx="3" cy="6" r="1" /><circle cx="7" cy="6" r="1" /><circle cx="11" cy="6" r="1" />
            </svg>
          </button>

          {speech && (
            <div
              ref={bubbleRef}
              className={`buddy-speech${speech.fact ? ' buddy-speech-fact' : ''}`}
              style={speech.fact && factSize ? { width: factSize.w, height: factSize.h } : undefined}
              onMouseEnter={() => { if (speechTimer.current) clearTimeout(speechTimer.current); }}
              onMouseLeave={() => { if (!resizing.current) scheduleClose(3000); }}
            >
              {speech.fact && (
                <span
                  className="buddy-speech-resize"
                  title="Drag to resize"
                  onMouseDown={handleResizeStart}
                  aria-hidden="true"
                >
                  <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
                    <line x1="2" y1="6" x2="6" y2="2" /><line x1="2" y1="10.5" x2="10.5" y2="2" />
                  </svg>
                </span>
              )}
              {speech.fact && (
                <span className="buddy-speech-tag">
                  {buddy.name ? `${buddy.name} says · did you know?` : 'Did you know?'}
                </span>
              )}
              {speech.text}
            </div>
          )}

          {showCard && !speech && (
            <div className="buddy-card">
              <div className="buddy-card-name">
                {buddy.name}
                <span className="buddy-card-stars" style={{ color: buddy.color }}>{buddy.stars}</span>
              </div>
              <div className="buddy-card-species">
                {buddy.bones.rarity} {buddy.bones.species}{buddy.bones.shiny ? ' · shiny ✦' : ''}
              </div>
              {buddy.personality && <div className="buddy-card-personality">{buddy.personality}</div>}
              <div className="buddy-card-hint">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M12 2l2.2 6.2L20 10l-5 4 1.6 6L12 16.6 7.4 20 9 14l-5-4 5.8-1.8z" />
                </svg>
                Click to pet (fact)
              </div>
              <label className="buddy-card-toggle">
                <input
                  type="checkbox"
                  checked={aiFacts}
                  onChange={(e) => setAiFacts(e.target.checked)}
                />
                Fresh facts <span className="buddy-card-toggle-hint">(AI)</span>
              </label>
            </div>
          )}

          <div
            className="buddy-creature-wrap"
            title={buddy.name ? `${buddy.name}: click to pet (fact)` : 'Click to pet (fact)'}
            onMouseDown={handleBodyMouseDown}
            onClick={handleCreatureClick}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void revealFact(); } }}
          >
            <BuddyCreature bones={buddy.bones} color={buddy.color} petting={petting} />
          </div>
        </div>
      )}

      <button
        className="buddy-toggle-btn"
        title={companionEnabled === false ? 'Show companion' : hidden ? 'Show companion' : 'Hide companion'}
        aria-label={companionEnabled === false ? 'Show companion' : hidden ? 'Show companion' : 'Hide companion'}
        onClick={handleToggleClick}
        type="button"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9" />
          <path d="M8 14s1.5 2 4 2 4-2 4-2" />
          <line x1="9" y1="9" x2="9.01" y2="9" /><line x1="15" y1="9" x2="15.01" y2="9" />
        </svg>
      </button>
    </>
  );
};
