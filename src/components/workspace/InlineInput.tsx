import React, { useCallback, useEffect, useRef, useState } from 'react';

export interface InlineInputProps {
  /** Initial value for the input field. */
  initialValue?: string;
  /** Placeholder text when the input is empty. */
  placeholder?: string;
  /** Called when the user confirms the input (Enter key). */
  onConfirm: (value: string) => void;
  /** Called when the user cancels the input (Escape key or blur). */
  onCancel: () => void;
}

/**
 * Inline input field used for rename and new-file/new-folder operations
 * within the file tree. Automatically focuses on mount and selects the
 * filename portion (before the last dot) for rename convenience.
 */
export const InlineInput: React.FC<InlineInputProps> = ({
  initialValue = '',
  placeholder,
  onConfirm,
  onCancel,
}) => {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();

    // Select the filename portion (before the last dot) for rename convenience
    if (initialValue) {
      const dotIdx = initialValue.lastIndexOf('.');
      if (dotIdx > 0) {
        el.setSelectionRange(0, dotIdx);
      } else {
        el.select();
      }
    }
  }, [initialValue]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const trimmed = value.trim();
        if (trimmed) {
          onConfirm(trimmed);
        } else {
          onCancel();
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    },
    [value, onConfirm, onCancel],
  );

  const handleBlur = useCallback(() => {
    const trimmed = value.trim();
    if (trimmed && trimmed !== initialValue) {
      onConfirm(trimmed);
    } else {
      onCancel();
    }
  }, [value, initialValue, onConfirm, onCancel]);

  return (
    <input
      ref={inputRef}
      className="file-tree-inline-input"
      type="text"
      value={value}
      placeholder={placeholder}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={handleKeyDown}
      onBlur={handleBlur}
      aria-label={initialValue ? `Rename ${initialValue}` : 'New name'}
    />
  );
};
