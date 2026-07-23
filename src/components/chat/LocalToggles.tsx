import React from 'react';
import { LocalThinkingToggle } from './LocalThinkingToggle';
import { LocalToolsDropdown } from './LocalToolsDropdown';

interface LocalTogglesProps {
  /** Forwarded to the tools dropdown so it can close the other toolbar
   *  dropdowns when it opens (single-open behaviour). */
  onOpen?: () => void;
}

/** Toolbar group for local-model options (thinking toggle + tools scope
 *  dropdown). Each child renders only when the selected on-device model
 *  advertises that capability, so this is invisible for cloud models. */
export const LocalToggles: React.FC<LocalTogglesProps> = ({ onOpen }) => (
  <>
    <LocalThinkingToggle />
    <LocalToolsDropdown onOpen={onOpen} />
  </>
);
