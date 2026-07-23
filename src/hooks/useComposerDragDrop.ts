import React, { useCallback, useRef, useState } from 'react';

/**
 * Drag-and-drop + paperclip file attachment for the chat composer. Owns the
 * hidden file-input ref and the drag-over highlight, and routes both dropped
 * and picked files through the shared chip uploader so they get the same
 * "(uploading…)" chip and failure toast.
 */
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

  return {
    fileInputRef,
    isDragOver,
    handleFileSelect,
    handleDragOver,
    handleDragLeave,
    handleDrop,
  };
}
