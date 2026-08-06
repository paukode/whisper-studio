# Settings information architecture (proposal)

The Settings dialog has grown to 14 flat tabs that wrap onto two rows, and the
Models tab alone stacks five unrelated concerns. This proposes a restructure to
reduce crowding without hiding anything. Proposal only, not yet implemented.

## Current state

Flat, equal-weight tab strip (wraps to two rows):
API Keys · Model Mode · Models · Feature Flags · Skills · Workflows · MCP ·
Permissions · Hooks · Scheduled Tasks · Plugins · Stats · Costs · Live Preview.

The Models tab stacks: Transcription models, Indexing models, Local chat
models, chat-model visibility toggles, and the Discover browser, in one long
vertical scroll.

## Proposed structure

Replace the wrapping tab strip with a grouped left rail, and collapse 14 tabs
into ~7 items under 3 headings by merging siblings.

- Chat and models
  - Model mode
  - Models — internal sub-tabs: Chat / Discover / Transcription / Indexing
    (each a focused view instead of one long page)
- Tools and automation
  - MCP servers
  - Skills and plugins (merge Skills + Plugins, two sub-tabs)
  - Workflows and tasks (merge Workflows + Scheduled tasks + Hooks)
- Access and usage
  - Keys and permissions (merge API keys + Permissions)
  - Usage and advanced (merge Stats + Costs + Feature flags + Live preview)

## Why

- A left rail does not wrap and stays scannable as the app grows.
- Grouping lets users navigate by intent ("add a tool", "change the model")
  rather than scanning 14 equal-weight labels.
- The Models tab becomes four small tabs instead of one giant page.
- Rarely-touched panels (transcription/indexing downloads, feature flags, live
  preview) stop competing for attention with everyday settings.

## Quant picker (already being implemented)

One compact row per model: a dropdown of quants (recommended preselected, size
shown in each option) next to a single Download button, instead of the
expandable radio list.

## Open decisions

1. Merge groupings as above, or a different split?
2. Left rail vs. keeping a (single-row, non-wrapping) top tab bar with grouping.
3. Whether transcription/indexing model management moves out of the chat-model
   Models view entirely (e.g. under Voice, and under a workspace/index area).
