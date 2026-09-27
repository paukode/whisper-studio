import { queryFile } from '@/api/workspace';
import { useUIStore } from '@/stores/uiStore';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import type { EditorTab } from '@/types/workspace';

/** No edit of the user's is in the tab: it is clean, or still shows exactly
 *  what the last inline write put there. */
const holdsNoUserEdits = (tab: EditorTab) => !tab.isDirty || tab.content === tab.appliedContent;

/**
 * Bring an open editor tab in line with a write the backend applied inline
 * (the ws_auto_applied frame). The tab shows the written content, marked dirty
 * against what it held before, so the change stays visible and diffable.
 *
 * What lands on disk can differ from what was written: ruff fixes and formats
 * a Python file right after the write when the workspace configures ruff
 * (server/code_tools/ruff.py), and line endings are normalised. So the tab then
 * takes the content on disk, unless the user has typed in it since: saving the
 * tab must never put back what ruff changed.
 *
 * Only a tab of the root the write landed in is touched (`workspaceRoot`, the
 * root the frame's call was bound to, else the one connected now): a tab kept
 * from another workspace is a different file with the same relative path. And
 * a tab holding the user's unsaved edits keeps them, with a notice, rather than
 * losing them to the write.
 */
export function syncAutoAppliedTab(
  path: string,
  written: string | undefined,
  workspaceRoot?: string | null,
): void {
  const ws = useWorkspaceStore.getState();
  const root = workspaceRoot || useUIStore.getState().wsPath || undefined;
  const tab = ws.editorTabs.find((t) => t.path === path);
  if (!tab || tab.root !== root) return;
  if (!holdsNoUserEdits(tab)) {
    useUIStore.getState().addToast({
      type: 'warning',
      message:
        `The assistant wrote ${path.split('/').pop() || path} while it had unsaved edits in the ` +
        "editor. The tab keeps your edits; saving it would replace the assistant's version.",
      duration: 8000,
    });
    return;
  }
  const content = written ?? tab.content;
  ws.applyWrite(path, content);
  queryFile(path)
    .then((data) => {
      if (!('content' in data) || typeof data.content !== 'string' || data.content === content) {
        return;
      }
      const store = useWorkspaceStore.getState();
      const current = store.editorTabs.find((t) => t.path === path);
      if (current && current.root === root && current.content === content) {
        store.applyWrite(path, data.content);
      }
    })
    .catch(() => {
      /* the file may be gone; the tab keeps the written content */
    });
}
