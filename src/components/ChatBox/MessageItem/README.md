# MessageItem Directory

## Overview

`MessageItem` contains the card-level renderers used for chat messages, markdown content, reasoning notices, and task completion prompts. These components receive prepared message data from the parent chat flow and focus on presentation plus local UI actions such as copying text or revealing attached files.

## Direct Children

- `AgentMessageCard.tsx`: Renders assistant/agent message markdown, copy action, typewriter completion tracking, and output file attachments. Attachment file names must stay inside the card width and may wrap for long names.
- `UserMessageCard.tsx`: Renders user text, skill-tag buttons, copy action, and compact user file attachments with overflow handling for extra files.
- `FeedbackCard.tsx`: Renders feedback-oriented message controls.
- `MarkDown.tsx`: Markdown renderer used for normal agent message content.
- `SummaryMarkDown.tsx`: Markdown renderer variant for summary content.
- `NoticeCard.tsx`: Renders expandable notice or reasoning content.
- `TaskCompletionCard.tsx`: Renders the task completion prompt and opens `TriggerDialog` for adding a trigger.

## Data Flow

- Parent chat grouping components pass message content, attachment arrays, and callbacks into these card components.
- File reveal actions call Electron IPC from the card click handlers.
- Message parsing and markdown rendering remain local to this directory; task state transitions are owned by `ChatBox` and stores.

## Public Surface

- Components are imported directly by nearby ChatBox components.
- There is no barrel export in this directory.

## Modification Notes

- Keep layout fixes local to the card that renders the affected content.
- Avoid adding state-store side effects here; this directory should remain presentation-focused.
