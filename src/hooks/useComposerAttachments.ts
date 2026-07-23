import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';

/**
 * Owns the chat composer's attachment chips and every way a file gets onto
 * them: the paperclip picker, drag-drop, and the `/file:` workspace-file flow.
 *
 * Extracted from ChatInput.tsx so the component stays under the file-size
 * guardrail and the upload logic lives in one cohesive, reusable place.
 *
 * Chips show an instant `_uploading_…` placeholder that is swapped for the
 * real attachment id once `/api/upload` resolves (or dropped on failure), so
 * uploads always start at attach time and a later submit rarely has to wait.
 */
export interface ComposerAttachment {
  id: string;
  filename: string;
}

export interface UseComposerAttachmentsResult {
  attachments: ComposerAttachment[];
  setAttachments: React.Dispatch<React.SetStateAction<ComposerAttachment[]>>;
  /** Always-latest attachments, for reading post-upload state inside async
   *  submit logic without a stale closure. */
  attachmentsRef: React.MutableRefObject<ComposerAttachment[]>;
  uploadFilesAsChips: (files: File[]) => Promise<void>;
  attachWorkspaceFileAsChip: (path: string) => Promise<void>;
  uploadWorkspaceFile: (path: string) => Promise<ComposerAttachment | null>;
  removeAttachment: (idx: number) => void;
  /** Resolve once no chip is mid-upload (or after a safety timeout). */
  waitForUploads: () => Promise<boolean>;
}

export function useComposerAttachments(): UseComposerAttachmentsResult {
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);

  // Abort controllers for in-flight uploads, keyed by placeholder chip id, so
  // clicking × on a chip mid-upload cancels the network request, not just the
  // chip. One controller per uploadFilesAsChips batch — cancelling any chip in
  // a batch aborts that batch's shared request.
  const uploadControllers = useRef<Map<string, AbortController>>(new Map());

  // Always-latest attachments mirrored into a ref, so async submit logic and
  // click handlers read post-update state without a stale closure.
  const attachmentsRef = useRef(attachments);
  useEffect(() => { attachmentsRef.current = attachments; }, [attachments]);

  // Upload File objects to the composer as attachment chips. Each starts as an
  // instant placeholder (id prefixed `_uploading_`) that the chip renders with
  // an "uploading…" badge, then gets swapped for the real attachment id when
  // /api/upload resolves. Shared by the paperclip picker, drag-drop, the
  // context-menu "Add to Chat", and the /file: workspace flow.
  const uploadFilesAsChips = useCallback(async (files: File[]) => {
    if (files.length === 0) return;
    const stamp = Date.now();
    const placeholders = files.map((f, i) => ({
      // The filename stays clean (no suffix); the chip renders a separate,
      // never-truncated "uploading…" badge keyed off this `_uploading_` id.
      id: `_uploading_${stamp}_${i}`,
      filename: f.name,
    }));
    setAttachments((prev) => [...prev, ...placeholders]);

    const controller = new AbortController();
    for (const p of placeholders) uploadControllers.current.set(p.id, controller);

    const formData = new FormData();
    for (const file of files) formData.append('files', file);

    try {
      const response = await fetch('/api/upload', {
        method: 'POST',
        body: formData,
        signal: controller.signal,
      });
      if (!response.ok) {
        // Surface the backend's reason (e.g. "Unsupported file type",
        // "exceeds 50MB limit") rather than a blanket failure message.
        const detail = (await response.json().catch(() => null)) as { error?: string } | null;
        throw new Error(detail?.error || 'Upload failed');
      }
      const data = (await response.json()) as { attachments: ComposerAttachment[] };
      const items = data.attachments ?? [];
      setAttachments((prev) => [
        ...prev.filter((a) => !placeholders.some((p) => p.id === a.id)),
        ...items,
      ]);
    } catch (err) {
      // Drop the placeholders. An AbortError is a user-initiated cancel via the
      // × button, so it stays silent; anything else is a real failure.
      setAttachments((prev) => prev.filter((a) => !placeholders.some((p) => p.id === a.id)));
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        console.warn('File upload failed:', err);
        // A TypeError from fetch means the server was unreachable (e.g. the
        // backend isn't running) — say so plainly instead of "Failed to fetch".
        const message = err instanceof TypeError
          ? "Can't reach the server. Make sure the app's backend is running, then try again."
          : err instanceof Error ? err.message : 'File upload failed';
        useUIStore.getState().addToast({ type: 'error', message });
      }
    } finally {
      for (const p of placeholders) uploadControllers.current.delete(p.id);
    }
  }, []);

  // Fetch a workspace file's raw bytes and wrap them in a File so it can go
  // through the same /api/upload path as a user-picked file.
  const fetchWorkspaceFile = useCallback(async (path: string): Promise<File> => {
    const url = `/api/workspace/file?path=${encodeURIComponent(path)}&raw=true`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`File not found: ${path}`);
    const blob = await resp.blob();
    const name = path.split('/').pop() || path;
    return new File([blob], name, { type: blob.type || 'application/octet-stream' });
  }, []);

  // /file: dropdown select (and /file:path with no question) → attach a
  // workspace file to the composer as a chip and upload in the background.
  const attachWorkspaceFileAsChip = useCallback(async (path: string) => {
    try {
      const file = await fetchWorkspaceFile(path);
      await uploadFilesAsChips([file]);
    } catch (err) {
      useUIStore.getState().addToast({
        type: 'error',
        message: err instanceof Error ? err.message : 'Failed to attach file',
        duration: 3000,
      });
    }
  }, [fetchWorkspaceFile, uploadFilesAsChips]);

  // Fetch + upload a workspace file WITHOUT a composer chip; returns the
  // attachment record (or null on failure). Used by the non-blocking
  // "/file:path question" auto-send path.
  const uploadWorkspaceFile = useCallback(
    async (path: string): Promise<ComposerAttachment | null> => {
      try {
        const file = await fetchWorkspaceFile(path);
        const formData = new FormData();
        formData.append('files', file);
        const response = await fetch('/api/upload', { method: 'POST', body: formData });
        if (!response.ok) throw new Error('Upload failed');
        const data = (await response.json()) as { attachments: ComposerAttachment[] };
        return data.attachments?.[0] ?? null;
      } catch {
        return null;
      }
    },
    [fetchWorkspaceFile],
  );

  // Remove a chip. If it's an in-flight upload, abort its request first so the
  // network task is cancelled, not just the chip (the catch handler then drops
  // any sibling chips from the same batch).
  const removeAttachment = useCallback((idx: number) => {
    const target = attachmentsRef.current[idx];
    if (target?.id.startsWith('_uploading_')) {
      uploadControllers.current.get(target.id)?.abort();
    }
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  // Resolve once no chip is mid-upload (placeholder ids cleared) or after a
  // safety timeout. uploadFilesAsChips always either swaps a placeholder for
  // a real id or drops it on failure, so the absence of `_uploading_` ids
  // means every in-flight upload has settled.
  const waitForUploads = useCallback(async () => {
    const deadline = Date.now() + 20000;
    while (attachmentsRef.current.some((a) => a.id.startsWith('_uploading_'))) {
      if (Date.now() > deadline) return false;
      await new Promise((r) => setTimeout(r, 120));
    }
    return true;
  }, []);

  return {
    attachments,
    setAttachments,
    attachmentsRef,
    uploadFilesAsChips,
    attachWorkspaceFileAsChip,
    uploadWorkspaceFile,
    removeAttachment,
    waitForUploads,
  };
}
