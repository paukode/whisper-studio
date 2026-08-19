import React, { useCallback, useRef, useState } from 'react';

/**
 * Drag-and-drop, paperclip, and clipboard-paste file attachment for the chat
 * composer. Owns the hidden file-input ref and the drag-over highlight, and
 * routes dropped, picked, and pasted files through the shared chip uploader
 * so they get the same "(uploading…)" chip and failure toast.
 */

// Clipboard images arrive under the browser's generic name ("image.png", or
// no name at all), so consecutive screenshot pastes would all read the same.
// Stamp those with the paste time; files copied from the Finder keep their
// real names.
function namePastedFile(file: File, index: number): File {
  if (file.name && file.name !== 'image.png') return file;
  const ext = (file.type.split('/')[1] || 'png').split('+')[0];
  const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-');
  const suffix = index > 0 ? `-${index + 1}` : '';
  return new File([file], `pasted-${stamp}${suffix}.${ext}`, { type: file.type });
}
export function useComposerDragDrop(uploadFilesAsChips: (files: File[]) => Promise<void> | void) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragOver, setIsDragOver] = useState(false);

  const handleFileSelect = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files ?? []);
      if (files.length === 0) return;
      e.target.value = '';
      await uploadFilesAsChips(files);
    },
    [uploadFilesAsChips],
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);
  const handleDragLeave = useCallback(() => setIsDragOver(false), []);
  const handleDrop = useCallback(
    async (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      const files = Array.from(e.dataTransfer.files);
      if (files.length === 0) return;
      await uploadFilesAsChips(files);
    },
    [uploadFilesAsChips],
  );

  // Files pasted from the clipboard (a copied screenshot, or a file copied in
  // the Finder) become attachment chips like a drop would. Text-only pastes
  // fall through to the default insert; when a file is present its companion
  // text flavor (the filename, or the HTML of a copied web image) stays out
  // of the input.
  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.files ?? []);
      if (files.length === 0) return;
      e.preventDefault();
      void uploadFilesAsChips(files.map(namePastedFile));
    },
    [uploadFilesAsChips],
  );

  return {
    fileInputRef,
    isDragOver,
    handleFileSelect,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    handlePaste,
  };
}
