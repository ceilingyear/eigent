# HeaderBox Directory

## Overview

`HeaderBox` owns the top bar inside `ChatBox`. It shows the chat title, optional replay action, and current project token total.

## Direct Children

- `index.tsx`: Exports `HeaderBox`, which renders token totals through `AnimatedTokenNumber`, chooses the token icon from the current appearance, and conditionally shows the replay button when the active task is finished.

## Data Flow

- Parent `ChatBox/index.tsx` passes task status, total token count, replay loading state, and replay callback.
- Replay visibility is also guarded by Vite environment flags in `HeaderBox`: `VITE_ENABLE_REPLAY=false` or `VITE_DISABLE_REPLAY=true` hides the button.

## Public Surface

- Named export: `HeaderBox`.
- Props are local to `index.tsx`; no barrel export exists in this directory.

## Modification Notes

- Keep replay gating local to the button rendering path so disabled replay does not leave a visible inactive control.
- Token total display should remain passive; aggregation is owned by `ChatBox/index.tsx`.
