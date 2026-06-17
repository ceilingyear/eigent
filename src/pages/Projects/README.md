# Projects Page

This directory contains the history/projects route implementation.

## Direct Children

- `Project.tsx`: route component for the history/projects page. It renders grouped history, handles task/project deletion dialogs, share/replay actions, and opening historical projects.

## Public Surface

The route component is consumed by the router. It does not export shared UI components.

## Implementation Notes

- Grouped history data is loaded by `GroupedHistoryView`.
- `Project.tsx` should not perform a separate legacy `/api/v1/chat/histories` fetch for data that is not rendered.
- Opening a history project should call `loadProjectFromHistory` with the grouped task metadata so the store can hydrate each task statically.

## Modification Constraints

- Keep deletion callbacks coordinated with `GroupedHistoryView` so local grouped data remains the source of truth for the list.
- Do not introduce page-level polling unless the user explicitly requests live refresh behavior.
