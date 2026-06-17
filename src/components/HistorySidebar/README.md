# HistorySidebar

This directory contains the sidebar history list.

## Direct Children

- `index.tsx`: sidebar history container, grouped history loading, project selection, deletion modal, and view rendering.
- `SearchInput.tsx`: search input used by the sidebar history list.

## Public Surface

`index.tsx` is consumed by layouts/sidebar surfaces. `SearchInput.tsx` is private to this directory.

## Implementation Notes

- Selecting a historical project should pass the grouped project task metadata into `loadProjectFromHistory`.
- Sidebar selection is a static history-viewing flow and should not trigger replay playback.

## Modification Constraints

- Keep search input local unless another directory has a confirmed reuse requirement.
- Keep history deletion state local to this sidebar component.
