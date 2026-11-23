import type { QueryClient } from '@tanstack/react-query';
import { useSettingsStore } from '@/stores/settingsStore';

/** Refetch every list that shows local chat models: Models > Chat (catalog and
 *  download status), Discover (Recommended and search), chat model visibility,
 *  and the composer picker. Any install or delete calls this, so the tabs can
 *  never show one model in two states. */
export async function refreshModelLists(queryClient: QueryClient): Promise<void> {
  await Promise.all(
    [
      ['models-manager-catalog'],
      ['models-manager-status'],
      ['models-disabled'],
      ['model-browse-search'],
      ['model-browse-recommended'],
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
  // The composer reads the zustand settings store, not react-query.
  await useSettingsStore.getState().loadModels();
}
