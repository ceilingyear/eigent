# GroupedHistoryView

This directory contains the grouped history/project list UI.

## Direct Children

- `index.tsx`: grouped history container, view mode switching, filtering, empty project merging, delete/edit callbacks, and grouped list/grid rendering.
- `ProjectDialog.tsx`: project detail dialog.
- `ProjectGroup.tsx`: individual project group card/list item; opens existing projects or hydrates static history projects.
- `TaskItem.tsx`: task row/item rendering within a project group.

## Public Surface

`index.tsx` is the default component consumed by history pages and dialogs. Other files are private implementation details for this directory.

## Implementation Notes

- `ProjectGroup.tsx` must pass full task metadata when calling `loadProjectFromHistory`, not just task IDs.
- Static history opening should show the final state and must not trigger replay playback endpoints.
- Explicit replay actions are separate from project opening.

## Modification Constraints

- Keep grouped history fetching in `index.tsx`.
- Keep per-project click/loading state in `ProjectGroup.tsx`.
- Do not add background polling from this directory without a clear product requirement.
