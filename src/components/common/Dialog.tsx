import { useCallback, useEffect, useRef, useState } from 'react';
import { useUIStore, type DialogEntry, type DialogFormField } from '@/stores/uiStore';
import { useFocusTrap } from '@/hooks/useDismiss';

/** Renders the topmost dialog from the stack */
export const DialogHost: React.FC = () => {
  const stack = useUIStore((s) => s.dialogStack);
  const topDialog = stack[stack.length - 1] ?? null;

  if (!topDialog) return null;

  return <DialogRenderer key={topDialog.id} dialog={topDialog} />;
};

/** Single dialog renderer */
const DialogRenderer: React.FC<{ dialog: DialogEntry }> = ({ dialog }) => {
  const resolveDialog = useUIStore((s) => s.resolveDialog);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [formData, setFormData] = useState<Record<string, string | boolean>>(() => {
    if (dialog.kind !== 'form' || !dialog.fields) return {};
    const init: Record<string, string | boolean> = {};
    for (const f of dialog.fields) {
      init[f.name] = f.value ?? (f.type === 'checkbox' ? false : '');
    }
    return init;
  });

  // Wizard state
  const [wizardStep, setWizardStep] = useState(0);
  const [wizardData, setWizardData] = useState<Record<string, unknown>>({});

  // Play the overlay's exit animation (whisper-dialog-overlay--leaving, defined
  // in dialog.css) before the dialog is removed from the stack. The promise
  // resolution is deferred by the same 200ms so the fade-out completes first.
  const [leaving, setLeaving] = useState(false);
  const closeTimer = useRef<number | null>(null);
  const beginClose = useCallback(
    (result: unknown) => {
      if (closeTimer.current !== null) return; // already closing
      setLeaving(true);
      closeTimer.current = window.setTimeout(() => resolveDialog(dialog.id, result), 200);
    },
    [dialog.id, resolveDialog],
  );
  useEffect(() => () => {
    if (closeTimer.current !== null) clearTimeout(closeTimer.current);
  }, []);

  const handleCancel = useCallback(() => {
    beginClose(null);
  }, [beginClose]);

  const handleConfirm = useCallback(() => {
    if (dialog.kind === 'confirm') {
      beginClose(true);
    } else if (dialog.kind === 'form') {
      // Validate required fields
      if (dialog.fields) {
        for (const f of dialog.fields) {
          if (f.required && !formData[f.name]) {
            return; // Don't submit if required field missing
          }
        }
      }
      beginClose(formData);
    }
  }, [dialog, formData, beginClose]);

  // Escape key handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); handleCancel(); }
      if (e.key === 'Enter' && dialog.kind === 'confirm') {
        // Only treat a bare Enter as "confirm" when focus is NOT on a button.
        // If the user tabbed to (or auto-focus put them on) Cancel, let the
        // browser activate THAT button natively — otherwise Enter on a focused
        // Cancel would run the destructive confirm the user meant to decline.
        if (!(document.activeElement instanceof HTMLButtonElement)) {
          e.preventDefault();
          handleConfirm();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleCancel, handleConfirm, dialog.kind]);

  // Auto-focus first focusable (per wizard step). The trap below keeps Tab
  // inside the dialog and restores focus to the trigger on close — it skips
  // initial focus since this effect already owns it.
  useEffect(() => {
    const root = dialogRef.current;
    if (!root) return;
    if (dialog.kind === 'confirm') {
      // Default the focus (and therefore the native Enter target): the primary
      // Confirm button for a normal confirm, but the Cancel button for a
      // destructive (danger) one, so a reflexive Enter cannot fire an
      // irreversible action.
      const sel = dialog.danger
        ? '.whisper-dialog__footer .btn:not(.btn-primary)'
        : '.whisper-dialog__footer .btn-primary';
      const target =
        root.querySelector<HTMLElement>(sel) ??
        root.querySelector<HTMLElement>('button:not(.whisper-dialog__close)');
      target?.focus();
      return;
    }
    const focusable = root.querySelector<HTMLElement>(
      'input, select, textarea, button:not(.whisper-dialog__close)',
    );
    focusable?.focus();
  }, [wizardStep, dialog.kind, dialog.danger]);

  useFocusTrap(dialogRef, true, { initialFocus: false });

  const sizeClass = dialog.size ? ` whisper-dialog--${dialog.size}` : ' whisper-dialog--md';
  const dangerClass = dialog.danger ? ' whisper-dialog--danger' : '';

  const renderFormFields = (fields: DialogFormField[]) => (
    <>
      {fields.map((f) => (
        <div className="whisper-dialog__field" key={f.name}>
          <label className="whisper-dialog__label">{f.label}</label>
          {f.type === 'checkbox' ? (
            <label className="whisper-dialog__checkbox-wrap">
              <input
                type="checkbox"
                checked={!!formData[f.name]}
                onChange={(e) => setFormData((d) => ({ ...d, [f.name]: e.target.checked }))}
              />
              <span>{f.label}</span>
            </label>
          ) : f.type === 'select' ? (
            <select
              className="whisper-dialog__select"
              value={String(formData[f.name] ?? '')}
              onChange={(e) => setFormData((d) => ({ ...d, [f.name]: e.target.value }))}
            >
              {f.options?.map((opt) => {
                const val = typeof opt === 'string' ? opt : opt.value;
                const label = typeof opt === 'string' ? opt : opt.label;
                return <option key={val} value={val}>{label}</option>;
              })}
            </select>
          ) : f.type === 'textarea' ? (
            <textarea
              className="whisper-dialog__input whisper-dialog__input--textarea"
              value={String(formData[f.name] ?? '')}
              placeholder={f.placeholder}
              onChange={(e) => setFormData((d) => ({ ...d, [f.name]: e.target.value }))}
            />
          ) : (
            <input
              className="whisper-dialog__input"
              type={f.type || 'text'}
              value={String(formData[f.name] ?? '')}
              placeholder={f.placeholder}
              onChange={(e) => setFormData((d) => ({ ...d, [f.name]: e.target.value }))}
            />
          )}
        </div>
      ))}
    </>
  );

  const renderWizardSteps = () => {
    if (dialog.kind !== 'wizard' || !dialog.steps) return null;
    return (
      <div className="whisper-wizard-steps">
        {dialog.steps.map((_step, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && (
              <div className={`whisper-wizard-connector${i <= wizardStep ? ' whisper-wizard-connector--done' : ''}`} />
            )}
            <div
              className={`whisper-wizard-step${i === wizardStep ? ' whisper-wizard-step--active' : ''}${i < wizardStep ? ' whisper-wizard-step--done' : ''}`}
            >
              {i < wizardStep ? '\u2713' : i + 1}
            </div>
          </div>
        ))}
      </div>
    );
  };

  const renderBody = () => {
    if (dialog.kind === 'confirm') {
      // A rich `body` ReactNode (e.g. the data-retention consent screen) takes
      // precedence over a plain message string.
      return dialog.body ? <>{dialog.body}</> : <p>{dialog.message}</p>;
    }
    if (dialog.kind === 'form' && dialog.fields) {
      return renderFormFields(dialog.fields);
    }
    if (dialog.kind === 'wizard' && dialog.steps) {
      const currentStep = dialog.steps[wizardStep];
      return (
        <>
          {renderWizardSteps()}
          {currentStep?.fields && renderFormFields(currentStep.fields)}
          {currentStep?.body && <div>{currentStep.body}</div>}
        </>
      );
    }
    if (dialog.body) {
      return <div>{dialog.body}</div>;
    }
    return null;
  };

  const renderFooter = () => {
    if (dialog.kind === 'open') return null;
    if (dialog.kind === 'wizard' && dialog.steps) {
      const isLast = wizardStep === dialog.steps.length - 1;
      return (
        <div className="whisper-dialog__footer">
          <button className="btn" onClick={handleCancel} type="button">
            Cancel
          </button>
          {wizardStep > 0 && (
            <button
              className="btn"
              onClick={() => setWizardStep((s) => s - 1)}
              type="button"
            >
              Back
            </button>
          )}
          <button
            className="btn btn-primary"
            onClick={() => {
              const stepData = { ...wizardData, ...formData };
              setWizardData(stepData);
              if (isLast) {
                beginClose(stepData);
              } else {
                setWizardStep((s) => s + 1);
              }
            }}
            type="button"
          >
            {isLast ? 'Finish' : 'Next'}
          </button>
        </div>
      );
    }

    return (
      <div className="whisper-dialog__footer">
        <button
          className="btn"
          onClick={handleCancel}
          type="button"
        >
          {dialog.cancelText ?? 'Cancel'}
        </button>
        <button
          className="btn btn-primary"
          onClick={handleConfirm}
          type="button"
        >
          {dialog.confirmText ?? (dialog.kind === 'form' ? 'Submit' : 'Confirm')}
        </button>
      </div>
    );
  };

  return (
    <div
      className={`whisper-dialog-overlay${leaving ? ' whisper-dialog-overlay--leaving' : ''}`}
      onClick={handleCancel}
    >
      <div
        className={`whisper-dialog${sizeClass}${dangerClass}`}
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`dialog-title-${dialog.id}`}
        onClick={(e) => e.stopPropagation()}
      >
        {dialog.title !== false && (
          <div className="whisper-dialog__header">
            <h2 className="whisper-dialog__title" id={`dialog-title-${dialog.id}`}>{dialog.title || ''}</h2>
            <button className="whisper-dialog__close" onClick={handleCancel} type="button" aria-label="Close">
              &times;
            </button>
          </div>
        )}
        <div className="whisper-dialog__body">{renderBody()}</div>
        {renderFooter()}
      </div>
    </div>
  );
};
