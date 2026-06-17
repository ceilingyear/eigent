# BottomBox Directory

## Overview

`BottomBox` owns the chat input surface shown at the bottom of `ChatBox`. It switches between input, splitting, confirmation, running, and finished visual states while delegating task execution decisions to the parent `ChatBox` container and stores.

## Direct Children

- `index.tsx`: Public entry for the bottom input container. It chooses the background state, renders queued messages, selects the matching header variant, forwards usage-limit banners, and always renders `Inputbox`.
- `BoxHeader.tsx`: Header variants for splitting and task confirmation. `BoxHeaderConfirm` renders the edit affordance, task subtitle, start-task button, and the optional auto-confirm countdown passed from `ChatBox`.
- `InputBox.tsx`: Main compact composer with textarea, file attachment controls, drag/drop handling, trigger creation entry, and expanded composer launch.
- `ExpandedInputBox.tsx`: Larger composer dialog that reuses `Inputbox` and displays worker context for longer prompts.
- `QueuedBox.tsx`: Collapsible queued-message display with per-message removal callback.
- `UsageLimitBanner.tsx`: Inline warning or danger banner for usage limits with action and dismiss callbacks.
- `BoxAction.tsx`: Placeholder action strip retained for future task actions; it currently renders no controls.

## Data Flow

- Parent `ChatBox/index.tsx` owns task state, model readiness, usage-limit state, queued messages, and countdown state.
- `BottomBox/index.tsx` only renders the current UI state and forwards callbacks such as `onStartTask`, `onEdit`, `onRemoveQueuedMessage`, and input handlers.
- The start-task countdown is display-only here. Store-level auto-confirm remains in `src/store/chatStore.ts`.

## Public Surface

- Default export: `BottomBox`.
- Re-exported types: `FileAttachment`, `QueuedMessage`.

## Modification Notes

- Keep business decisions in the parent `ChatBox` or stores; this directory should stay focused on the bottom input UI.
- Do not add a separate auto-confirm timer here. Use the countdown value passed by the parent.
