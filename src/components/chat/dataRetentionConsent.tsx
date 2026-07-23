/**
 * Data-retention consent flow for Mythos-class models (e.g. Fable 5).
 *
 * Such models only work when the AWS account's Bedrock data-retention mode is
 * `provider_data_share`. Rather than flip that account-wide setting permanently,
 * we manage it just-in-time around model selection:
 *   - Switching TO a model flagged `requires_data_retention` ALWAYS shows the
 *     consent screen — even when retention is already on — so the change is
 *     explicitly acknowledged every time. The backend is only called when
 *     retention is actually off.
 *   - Switching AWAY to a non-retention model shows a matching split-panel
 *     asking whether to turn retention off (account returns to zero retention,
 *     mode "none").
 *
 * Both screens are chrome-less split-panel dialogs (kind 'open' + title:false —
 * no default header/footer). Styles live in static/modules/dialog.css under
 * `.wd-split`. EVERY model change in the app must route through
 * requestModelChange — never call setSelectedModel directly from UI code.
 */
import { put } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { DataRetentionResponseSchema } from '@/types/schemas';

function modelRequiresRetention(modelKey: string): boolean {
  const m = useSettingsStore.getState().models.find((x) => x.key === modelKey);
  return !!m?.requires_data_retention;
}

/* ── Checklist icons (inline, stroke style) ── */

const IconGlobe = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3c2.5 2.6 3.9 5.7 3.9 9S14.5 18.4 12 21c-2.5-2.6-3.9-5.7-3.9-9S9.5 5.6 12 3z" />
  </svg>
);

const IconClock = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.5 2" />
  </svg>
);

const IconEye = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z" />
    <circle cx="12" cy="12" r="2.8" />
  </svg>
);

const IconCloudOut = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 17.6A4.5 4.5 0 0 0 17.5 9.4 7 7 0 0 0 4 11.5 4 4 0 0 0 5 19h11" />
    <path d="M12 13v8M12 13l-3 3M12 13l3 3" />
  </svg>
);

const IconShield = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    <path d="M9 12l2 2 4-4" />
  </svg>
);

const IconBan = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M5.6 5.6l12.8 12.8" />
  </svg>
);

const IconRefresh = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 12a9 9 0 1 1-2.6-6.4" />
    <path d="M21 3v6h-6" />
  </svg>
);

/* ── Split-panel screens ── */

interface ScreenProps {
  onConfirm: () => void;
  onCancel: () => void;
}

function EnableScreen({ retentionOn, onConfirm, onCancel }: ScreenProps & { retentionOn: boolean }) {
  return (
    <div className="wd-split">
      <div className="wd-split__brand">
        <span className="wd-split__glyph">F5</span>
        <span className="wd-split__badge">Mythos class</span>
        <div className="wd-split__model">Fable 5</div>
        <div className="wd-split__tagline">
          Frontier capability, gated by an explicit data-retention opt-in.
        </div>
      </div>
      <div className="wd-split__content">
        <h2 className="wd-split__title">Before you switch&hellip;</h2>
        <p className="wd-split__lede">
          {retentionOn
            ? 'Data retention is already enabled on this account. Switching to this model keeps it in effect. Here is what that means:'
            : 'This model only runs with Bedrock data retention enabled on your AWS account. Here is exactly what that means:'}
        </p>
        <ul className="wd-checklist">
          <li>
            <span className="wd-checklist__icon">{IconGlobe}</span>
            <span className="wd-checklist__text">
              <strong>Account-wide</strong>
              Applies to every model on this AWS account, not just Fable 5.
            </span>
          </li>
          <li>
            <span className="wd-checklist__icon">{IconClock}</span>
            <span className="wd-checklist__text">
              <strong>30-day retention</strong>
              Prompts and responses are retained for 30 days.
            </span>
          </li>
          <li>
            <span className="wd-checklist__icon">{IconEye}</span>
            <span className="wd-checklist__text">
              <strong>Human review</strong>
              Shared with Anthropic and may be reviewed for misuse detection.
            </span>
          </li>
          <li>
            <span className="wd-checklist__icon">{IconCloudOut}</span>
            <span className="wd-checklist__text">
              <strong>Leaves AWS</strong>
              Retained data leaves AWS&rsquo;s security boundary.
            </span>
          </li>
        </ul>
        <p className="wd-split__note">
          Switching to a non-retention model later will offer to turn this back off
          (account returns to zero retention).
        </p>
        <div className="wd-split__actions">
          <button className="wd-split__cancel" type="button" onClick={onCancel}>
            Not now
          </button>
          <button className="wd-split__cta" type="button" onClick={onConfirm}>
            {retentionOn ? 'Continue' : 'Enable & continue'}
          </button>
        </div>
      </div>
    </div>
  );
}

function TurnOffScreen({ onConfirm, onCancel }: ScreenProps) {
  return (
    <div className="wd-split">
      <div className="wd-split__brand">
        <span className="wd-split__glyph">OFF</span>
        <span className="wd-split__badge">Zero retention</span>
        <div className="wd-split__model">Retention off</div>
        <div className="wd-split__tagline">
          Back to zero retention for the whole account.
        </div>
      </div>
      <div className="wd-split__content">
        <h2 className="wd-split__title">Turn off data retention?</h2>
        <p className="wd-split__lede">
          You switched away from a Mythos-class model. Turning retention off means:
        </p>
        <ul className="wd-checklist">
          <li>
            <span className="wd-checklist__icon">{IconShield}</span>
            <span className="wd-checklist__text">
              <strong>Zero retention</strong>
              The account returns to explicit zero retention: nothing retained or shared.
            </span>
          </li>
          <li>
            <span className="wd-checklist__icon">{IconBan}</span>
            <span className="wd-checklist__text">
              <strong>Fable 5 stops working</strong>
              Mythos-class models reject requests until retention is enabled again.
            </span>
          </li>
          <li>
            <span className="wd-checklist__icon">{IconRefresh}</span>
            <span className="wd-checklist__text">
              <strong>Re-enable anytime</strong>
              Selecting Fable 5 again brings back the consent screen.
            </span>
          </li>
        </ul>
        <p className="wd-split__note">
          Your model switch already happened. This choice only controls the account
          retention setting.
        </p>
        <div className="wd-split__actions">
          <button className="wd-split__cancel" type="button" onClick={onCancel}>
            Keep on
          </button>
          <button className="wd-split__cta" type="button" onClick={onConfirm}>
            Turn off retention
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Dialog plumbing ── */

/** Call the backend to flip retention. Returns true on success. */
async function applyRetention(enabled: boolean): Promise<boolean> {
  try {
    const res = await put<{ mode: string; enabled: boolean }>(
      '/api/data-retention',
      { enabled },
      { schema: DataRetentionResponseSchema },
    );
    useSettingsStore.getState().setDataRetentionEnabled(!!res.enabled);
    return true;
  } catch (err) {
    useUIStore.getState().addToast({
      type: 'error',
      message: err instanceof Error ? err.message : 'Failed to update data retention',
      duration: 7000,
    });
    return false;
  }
}

/** Show a chrome-less split-panel dialog. Resolves true only on explicit confirm. */
function showRetentionDialog(variant: 'enable' | 'turnoff'): Promise<boolean> {
  const retentionOn = useSettingsStore.getState().dataRetentionEnabled;
  return new Promise<boolean>((resolve) => {
    // The body buttons need the dialog id to resolve it, but the id only
    // exists after pushDialog returns — close over a mutable ref.
    const ref = { id: '' };
    const finish = (value: boolean) => {
      useUIStore.getState().resolveDialog(ref.id, value);
    };
    ref.id = useUIStore.getState().pushDialog({
      kind: 'open', // chrome-less: no default footer
      title: false, // no default header
      size: 'md',   // width is overridden by .whisper-dialog:has(.wd-split)
      body:
        variant === 'enable' ? (
          <EnableScreen retentionOn={retentionOn} onConfirm={() => finish(true)} onCancel={() => finish(false)} />
        ) : (
          <TurnOffScreen onConfirm={() => finish(true)} onCancel={() => finish(false)} />
        ),
      // Escape / overlay-click resolve with null → treated as decline.
      _resolve: (v: unknown) => resolve(v === true),
    });
  });
}

/**
 * Ensure retention is on for a model that requires it (send-time safety net).
 * Silent if already on; otherwise shows the consent screen and enables on
 * confirm. Returns true only if the model may now be used.
 */
export async function ensureRetentionEnabled(): Promise<boolean> {
  if (useSettingsStore.getState().dataRetentionEnabled) return true;

  const ok = await showRetentionDialog('enable');
  if (!ok) return false;

  const enabled = await applyRetention(true);
  if (enabled) {
    useUIStore.getState().addToast({
      type: 'success',
      message: 'Data retention enabled for this account.',
      duration: 3000,
    });
  }
  return enabled;
}

/**
 * Handle ANY model change in the UI (picker, slash command, future surfaces),
 * gating on data retention as needed. Returns whether the switch happened.
 *
 *  - target requires retention → ALWAYS show the consent screen (explicit
 *    acknowledgement on every switch, even when retention is already on);
 *    the backend is only called when retention is actually off. Cancel
 *    leaves the selection unchanged (the bound <select> reverts).
 *  - leaving a retention model while retention is on → switch, then show the
 *    matching turn-off screen.
 *  - otherwise → switch directly.
 */
export async function requestModelChange(targetKey: string): Promise<boolean> {
  const settings = useSettingsStore.getState();
  const current = settings.selectedModel;
  const targetModel = settings.models.find((m) => m.key === targetKey);
  const currentModel = settings.models.find((m) => m.key === current);
  // Re-selecting the current model is normally a no-op — EXCEPT an on-device
  // model that isn't resident yet (the default selection at startup is no longer
  // eager-loaded). In local/hybrid mode, re-selecting it is exactly how the user
  // loads the model to start a session, so let that fall through to the load.
  const needsLocalLoad = !!targetModel?.is_local && settings.loadedLocalModel !== targetKey;
  if (targetKey === current && !needsLocalLoad) return false;

  // On-device models: load the target into memory (with the progress banner)
  // before committing the switch; free the previous local model when leaving
  // it. Local models never need the data-retention gate.
  if (targetModel?.is_local) {
    const { loadLocalModel } = await import('@/api/localModel');
    // Load at the user's chosen context window so first load matches the slider.
    const ok = await loadLocalModel(targetKey, targetModel.name ?? targetKey, settings.localContextWindow);
    if (!ok) return false; // load failed — leave selection unchanged
    useSettingsStore.getState().setSelectedModel(targetKey);
    return true;
  }
  if (currentModel?.is_local) {
    // Leaving a local model for a cloud one — free the on-device weights.
    const { unloadLocalModel } = await import('@/api/localModel');
    void unloadLocalModel();
  }

  const targetNeeds = modelRequiresRetention(targetKey);
  const currentNeeds = modelRequiresRetention(current);

  if (targetNeeds) {
    const ok = await showRetentionDialog('enable');
    if (!ok) return false; // leave selection unchanged

    // Re-read state — the dialog was open for an arbitrary amount of time.
    if (!useSettingsStore.getState().dataRetentionEnabled) {
      const enabled = await applyRetention(true);
      if (!enabled) return false;
      useUIStore.getState().addToast({
        type: 'success',
        message: 'Data retention enabled for this account.',
        duration: 3000,
      });
    }
    useSettingsStore.getState().setSelectedModel(targetKey);
    return true;
  }

  if (currentNeeds && settings.dataRetentionEnabled) {
    useSettingsStore.getState().setSelectedModel(targetKey); // the switch itself proceeds
    const turnOff = await showRetentionDialog('turnoff');
    if (turnOff) {
      const ok = await applyRetention(false);
      if (ok) {
        useUIStore.getState().addToast({
          type: 'info',
          message: 'Data retention turned off. Account is at zero retention.',
          duration: 3000,
        });
      }
    }
    return true;
  }

  useSettingsStore.getState().setSelectedModel(targetKey);
  return true;
}
