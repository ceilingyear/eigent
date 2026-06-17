# Store Directory

This directory contains Zustand stores and store factories for application state.

## Direct Children

- `activityLogStore.ts`: activity log state.
- `authStore.ts`: authentication, account, worker, and model selection state.
- `chatStore.ts`: per-chat task state, message state, task execution, replay, static history hydration, files, tokens, and SSE lifecycle.
- `globalStore.ts`: global UI preferences such as history view type.
- `installationStore.ts`: installation/setup progress state.
- `pageTabStore.ts`: tab visibility and unviewed-state tracking.
- `projectStore.ts`: project containers, project-level queues, active project/chat routing, replay project creation, and history project hydration.
- `sidebarStore.ts`: sidebar UI state.
- `skillsStore.ts`: skill-related state.
- `triggerStore.ts`: trigger configuration state.
- `triggerTaskStore.ts`: trigger execution/task state.
- `workflowViewportStore.ts`: workflow viewport state.

## Public Surface

Each file exports its own store hook or factory. `projectStore.ts` also exports `Project` and `ProjectStore` types for consumers such as `src/lib/replay.ts`.

## Implementation Notes

- `chatStore.ts` owns task-level state mutation. Code that needs to materialize task messages, task cards, files, or status should use chat store APIs instead of mutating task objects from a page.
- `projectStore.ts` owns project and chat-store creation. History loading should create project/chat containers there, then delegate task hydration to `chatStore`.
- Static history viewing must use `chatStore.loadHistoryTask`; it must not call `chatStore.replay`, `startTask(type='replay')`, or `handleConfirmTask`.
- Explicit replay remains separate and may continue to use the replay/playback path.

## Modification Constraints

- Keep changes scoped to the store that owns the state being changed.
- Do not add cross-store abstractions for one-off state writes.
- Avoid resetting or deleting unrelated store state when hydrating historical projects.
