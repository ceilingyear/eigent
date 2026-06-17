# Lib Directory

This directory contains small cross-cutting frontend utilities and workflow helpers.

## Direct Children

- `fileUtils.ts`: file path and file display helpers.
- `htmlFontStyles.ts`: HTML font styling helpers.
- `htmlSanitization.ts`: HTML sanitization helpers.
- `index.ts`: barrel exports for shared lib helpers.
- `llm.ts`: LLM/model utility functions.
- `oauth.ts`: OAuth helpers.
- `providerModels.ts`: provider model metadata helpers.
- `queryClient.ts`: query client setup.
- `remoteSubAgent.ts`: remote sub-agent provider normalization/runtime helpers.
- `replay.ts`: navigation helpers for static history loading and explicit replay.
- `share.ts`: share workflow helper.
- `skillToolkit.ts`: skill/toolkit helpers.
- `toolkitIcons.tsx`: toolkit icon helpers.
- `utils.ts`: general utilities.

## Public Surface

`index.ts` re-exports helpers used across the app. `replay.ts` exposes two distinct flows: static history loading and explicit replay.

## Implementation Notes

- `loadProjectFromHistory` is for static history viewing. It delegates to `projectStore.loadProjectFromHistory` and navigates only after hydration completes.
- `replayProject` and related replay helpers are for user-triggered playback and may use replay-specific store paths.

## Modification Constraints

- Keep one-off page logic in the page/component instead of adding thin wrappers here.
- Preserve the boundary between static history viewing and replay playback.
