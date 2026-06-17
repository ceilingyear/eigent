# Components Directory

This directory contains shared UI components and larger feature components.

## Direct Children

- Feature directories: `AddWorker`, `BottomBar`, `BrowserAgentWorkspace`, `ChatBox`, `Dialog`, `ErrorBoundary`, `Folder`, `GlobalSearch`, `GroupedHistoryView`, `Halo`, `HistorySidebar`, `InstallStep`, `IntegrationList`, `Layout`, `MenuButton`, `Navigation`, `SearchInput`, `SideBar`, `TaskState`, `Terminal`, `TerminalAgentWorkspace`, `Toast`, `TopBar`, `Trigger`, `WindowControls`, `WorkFlow`, `WorkspaceMenu`, `animate-ui`, `ui`, and `update`.
- Direct files: `AnimationJson.tsx`, `SearchHistoryDialog.tsx`, and `ThemeProvider.tsx`.

## Public Surface

Feature directories expose components through their local `index.tsx` files where present. Direct files are imported individually by consumers.

## Implementation Notes

- `SearchHistoryDialog.tsx` is a global search/history dialog and can open grouped history projects through `loadProjectFromHistory`.
- `GroupedHistoryView` and `HistorySidebar` own grouped history list interactions; `ChatBox` owns active project chat rendering.

## Modification Constraints

- Prefer existing feature directories for related UI changes.
- Do not place page-specific stateful components in this root unless they are already root-level shared surfaces.
- Keep static history viewing separate from explicit replay controls.
