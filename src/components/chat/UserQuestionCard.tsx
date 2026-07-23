import React, { useState, useCallback, useMemo, useRef } from 'react';
import type { ChatMessage as ChatMessageType } from '@/types/chat';
import { getActiveChatStore } from '@/stores/sessionRuntimes';

/** Match only the conventional "Other"/"Other (please specify)" sentinel — not
 *  any real choice that merely begins with the word (e.g. "Otherton, Jane",
 *  "Other Inc."). Anchored on "other" + end / "(" / ":" / ellipsis. */
export function isOtherChoice(o: string): boolean {
  return /^other\b\s*($|\(|:|…|\.\.\.)/i.test(o.trim());
}

/**
 * Decide whether a question is asking about a filesystem location, and if so,
 * inject a "Browse…" option that opens the macOS native folder picker.
 *
 * For pathy questions we also drop "Other (please specify)" — Browse covers
 * the same need (pick an arbitrary path) with a better UX. For non-pathy
 * questions we leave Other alone since the user might genuinely want to
 * type free-form text (custom level, language, etc.).
 *
 * If the AI already emitted a Browse option, we leave the list untouched
 * (it's the AI's intent and may include extra context in the label).
 */
export function withBrowseOption(question: string, options: string[]): string[] {
  if (options.some((o) => o.toLowerCase().includes('browse'))) return options;

  // An option is a filesystem path only if it *starts* with a path prefix
  // (/, ~/, ./, ../) or is a single slash-bearing token with no spaces
  // (e.g. "src/components/Foo.tsx"). This deliberately excludes prose that
  // merely contains a slash — "portfolio/landing page", "HTML/CSS/JS",
  // "vs/and" — which previously triggered a bogus Browse… button.
  const isPathOption = (o: string) => {
    const t = o.trim();
    return /^(~|\.{0,2})?\//.test(t) || (t.includes('/') && !/\s/.test(t));
  };

  const q = question.toLowerCase();
  const looksPathy =
    /\b(folder|directory|path|location|where (?:should|do|to)|save|save (?:to|the)|store|destination)\b/.test(q) ||
    options.some(isPathOption);

  if (!looksPathy) return options;

  // Drop "Other (please specify)" — Browse… replaces it for path questions,
  // since the native picker can reach any folder anyway.
  const withoutOther = options.filter((o) => !isOtherChoice(o));
  return [...withoutOther, 'Browse…'];
}

/** Interactive user question card matching vanilla renderUserQuestion */
export const UserQuestionCard: React.FC<{
  question: string;
  options: string[];
  answered?: boolean;
  message: ChatMessageType;
}> = ({ question, options: rawOptions, answered, message }) => {
  const options = React.useMemo(() => withBrowseOption(question, rawOptions), [question, rawOptions]);
  const [showOtherInput, setShowOtherInput] = useState(false);
  const [otherText, setOtherText] = useState('');
  const [selectedOption, setSelectedOption] = useState<string | null>(null);
  const isAnswered = answered || selectedOption !== null;

  const submitAnswer = useCallback((answer: string) => {
    setSelectedOption(answer);

    // Mark as answered in the store
    const messages = getActiveChatStore().getState().messages;
    const idx = messages.indexOf(message);
    if (idx >= 0 && message.userQuestion) {
      const updated = [...messages];
      updated[idx] = {
        ...message,
        userQuestion: { ...message.userQuestion, answered: true },
      };
      getActiveChatStore().getState().setMessages(updated);
    }

    // Submit via the continuation path (approvedToolResult) so Bedrock
    // sees a proper tool_result for the ask_user_question tool_use, not a
    // fresh user message that leaves the tool_use unmatched.
    window.dispatchEvent(new CustomEvent('whisper-submit-answer', {
      detail: {
        answers: [{
          tool_use_id: message.userQuestion?.toolUseId ?? '',
          content: answer,
        }],
      },
    }));
  }, [message]);

  const handleOtherSubmit = useCallback(() => {
    const val = otherText.trim();
    if (!val) return;
    submitAnswer(val);
  }, [otherText, submitAnswer]);

  return (
    <div className="user-question-wrap">
      <div className="user-question-text" style={{
        marginBottom: 10,
        lineHeight: 1.5,
      }}>
        {question}
      </div>
      <div className="user-question-options" style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 8,
      }}>
        {!showOtherInput && options.map((opt) => {
          const isOther = isOtherChoice(opt);
          const isSelected = selectedOption === opt;
          return (
            <button
              key={opt}
              className={`user-question-btn${isSelected ? ' selected' : ''}`}
              style={{
                padding: '6px 14px',
                borderRadius: 6,
                border: '1px solid var(--border)',
                background: isSelected ? 'var(--accent)' : 'var(--bg-secondary)',
                color: isSelected ? '#fff' : 'var(--text-primary)',
                cursor: isAnswered ? 'default' : 'pointer',
                opacity: isAnswered && !isSelected ? 0.5 : 1,
                fontSize: '0.85em',
              }}
              disabled={isAnswered}
              onClick={() => {
                if (isOther) {
                  setShowOtherInput(true);
                } else if (opt.toLowerCase().includes('browse')) {
                  // Open native macOS folder picker
                  void (async () => {
                    try {
                      const resp = await fetch('/api/workspace/pick-folder');
                      const data = (await resp.json()) as { path?: string | null; cancelled?: boolean };
                      if (data.path) {
                        submitAnswer(data.path);
                      }
                    } catch (err) {
                      console.error('Folder picker failed:', err);
                    }
                  })();
                } else {
                  submitAnswer(opt);
                }
              }}
              type="button"
            >
              {opt}
            </button>
          );
        })}
        {showOtherInput && !isAnswered && (
          <div className="user-question-other-row" style={{
            display: 'flex',
            gap: 8,
            width: '100%',
          }}>
            <input
              type="text"
              className="user-question-other-input"
              placeholder="Type your answer\u2026"
              aria-label="Type your answer"
              value={otherText}
              onChange={(e) => setOtherText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleOtherSubmit(); }}
              autoFocus
              style={{
                flex: 1,
                padding: '6px 10px',
                borderRadius: 6,
                border: '1px solid var(--border)',
                background: 'var(--bg-primary)',
                color: 'var(--text-primary)',
                fontSize: '0.85em',
              }}
            />
            <button
              className="user-question-btn selected"
              onClick={handleOtherSubmit}
              type="button"
              style={{
                padding: '6px 14px',
                borderRadius: 6,
                border: '1px solid var(--accent)',
                background: 'var(--accent)',
                color: '#fff',
                cursor: 'pointer',
                fontSize: '0.85em',
              }}
            >
              Submit
            </button>
          </div>
        )}
        {showOtherInput && isAnswered && (
          <span style={{ fontSize: '0.85em', color: 'var(--text-muted)' }}>
            Answered: {selectedOption}
          </span>
        )}
      </div>
    </div>
  );
};

/** Multi-question card.
 *
 * Renders every ask_user_question call from the same streaming round. A single
 * question shows clean answer chips and submits the moment one is picked; 2+
 * questions stack vertically (labelled "Question N of M") with one submit
 * button enabled once all are answered. On submit, all answers batch into a
 * single continuation so Bedrock receives a tool_result for every tool_use it
 * emitted, and each question is marked answered in the store so a re-render or
 * session restore keeps the card disabled.
 */
export const UserQuestionGroupCard: React.FC<{ message: ChatMessageType }> = ({ message }) => {
  // Memoize so the `?? []` fallback doesn't make a new array every render.
  const questions = useMemo(() => message.userQuestions ?? [], [message.userQuestions]);
  // Restored-from-persistence guard: if already answered, start submitted so
  // the card can't fire a duplicate continuation after a re-render / reload.
  const alreadyAnswered = questions.length > 0 && questions.every((q) => q.answered);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  // Always-visible "type your own" text, keyed by toolUseId.
  const [customText, setCustomText] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(alreadyAnswered);
  // Hard idempotency guard: a ref (not the `submitted` state) so an async
  // Browse-picker resolution can't fire a second submit through a stale closure.
  const submittedRef = useRef(alreadyAnswered);

  const total = questions.length;
  // A single question submits the moment it's answered (no submit button); 2+
  // stack into one form submitted together.
  const single = total === 1;
  const allAnswered = total > 0 && questions.every((q) => answers[q.toolUseId] !== undefined);

  // Batch every answer into ONE continuation so Bedrock receives a tool_result
  // for each ask_user_question tool_use it emitted. Idempotent.
  const submit = useCallback((finalAnswers: Record<string, string>) => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    setSubmitted(true);
    // Persist answered:true so a later re-render / restore keeps the card
    // disabled instead of re-enabling it and allowing a duplicate continuation.
    const msgs = getActiveChatStore().getState().messages;
    const idx = msgs.indexOf(message);
    if (idx >= 0 && message.userQuestions) {
      const updated = [...msgs];
      updated[idx] = {
        ...message,
        userQuestions: message.userQuestions.map((q) => ({ ...q, answered: true })),
      };
      getActiveChatStore().getState().setMessages(updated);
    }
    window.dispatchEvent(new CustomEvent('whisper-submit-answer', {
      detail: {
        answers: questions.map((q) => ({
          tool_use_id: q.toolUseId,
          content: finalAnswers[q.toolUseId] ?? '',
        })),
      },
    }));
  }, [questions, message]);

  const setAnswer = useCallback((toolUseId: string, value: string) => {
    setAnswers((prev) => ({ ...prev, [toolUseId]: value }));
    if (single) submit({ [toolUseId]: value });  // one question needs no submit step
  }, [single, submit]);

  const submitAll = useCallback(() => {
    if (allAnswered && !submitted) submit(answers);
  }, [allAnswered, submitted, answers, submit]);

  if (total === 0) return null;

  return (
    <div className={`user-question-card${submitted ? ' submitted' : ''}`}>
      <div className="user-question-card-head">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" /><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        {single ? 'A quick question' : `${total} quick questions`}
      </div>
      {questions.map((q, qi) => {
        // The always-visible "Or type your own" input replaces the explicit
        // "Other" option, so drop any "other…" choice the model offered.
        const allOpts = withBrowseOption(q.question, q.options);
        const filteredOpts = allOpts.filter((o) => !isOtherChoice(o));
        // The always-on input replaces the "Other" sentinel — but never empty
        // the model's choices, so fall back to the full set if every option matched.
        const opts = filteredOpts.length ? filteredOpts : allOpts;
        const answered = answers[q.toolUseId];
        const custom = customText[q.toolUseId] ?? '';
        // Confirm in text only for answers that aren't a visible option row
        // (a typed answer or a Browse… folder path).
        const showAnsweredNote = answered !== undefined && !opts.includes(answered);
        return (
          <div key={q.toolUseId || qi} className="user-question-block">
            {!single && <div className="user-question-num">Question {qi + 1} of {total}</div>}
            <div className="user-question-text">{q.question}</div>
            <div className="user-question-opts">
              {opts.map((opt) => {
                const isBrowse = opt.toLowerCase().includes('browse');
                const isSelected = answered === opt;
                return (
                  <button
                    key={opt}
                    type="button"
                    className={`uq-opt${isSelected ? ' selected' : ''}`}
                    disabled={submitted}
                    onClick={() => {
                      if (submitted) return;
                      if (isBrowse) {
                        void (async () => {
                          try {
                            const resp = await fetch('/api/workspace/pick-folder');
                            const data = (await resp.json()) as { path?: string | null; cancelled?: boolean };
                            if (data.path) setAnswer(q.toolUseId, data.path);
                          } catch (err) {
                            console.error('Folder picker failed:', err);
                          }
                        })();
                        return;
                      }
                      setAnswer(q.toolUseId, opt);
                    }}
                  >
                    <span className="uq-opt-mark" aria-hidden="true" />
                    <span className="uq-opt-label">{opt}</span>
                    {isSelected && (
                      <svg className="uq-opt-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                        <polyline points="20 6 9 17 4 12" />
                      </svg>
                    )}
                  </button>
                );
              })}
            </div>
            {/* Always-visible custom answer — type and hit Enter (or the arrow)
             *  to answer freely without picking an option. */}
            <div className="uq-custom">
              <input
                type="text"
                className="uq-custom-input"
                placeholder="Or type your own answer…"
                aria-label="Type your own answer"
                value={custom}
                disabled={submitted}
                onChange={(e) => setCustomText((p) => ({ ...p, [q.toolUseId]: e.target.value }))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    const v = custom.trim();
                    if (v) setAnswer(q.toolUseId, v);
                  }
                }}
              />
              <button
                type="button"
                className="uq-custom-send"
                disabled={submitted || !custom.trim()}
                onClick={() => { const v = custom.trim(); if (v) setAnswer(q.toolUseId, v); }}
                aria-label="Submit your answer"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" />
                </svg>
              </button>
            </div>
            {showAnsweredNote && <span className="uq-answered">Answered: {answered}</span>}
          </div>
        );
      })}

      {/* Multi-question rounds submit once, when every question is answered. */}
      {!single && (
        <div className="uq-submit-row">
          <button
            type="button"
            className="uq-submit-btn"
            disabled={!allAnswered || submitted}
            onClick={submitAll}
          >
            {submitted ? 'Submitted' : allAnswered ? `Submit ${total} answers` : `Answer all ${total} to submit`}
          </button>
        </div>
      )}
    </div>
  );
};
