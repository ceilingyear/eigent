# 历史会话持续加载与重复请求排查报告

排查时间：2026-06-17  
范围：桌面端本地开发环境、`eigent_api` 最新容器日志、历史会话前端调用链

## 结论

历史会话页面本身没有看到固定轮询 `GET /api/v1/chat/histories/grouped?include_tasks=true` 的逻辑；真正异常的是“查看历史项目”进入了 replay/playback 链路。

当前代码注释写的是“加载最终状态、无动画”，但实现里仍调用了 `chatStore.replay(taskId, question, 0)`。这会触发 `startTask(type='replay')`，继续请求 `chat/snapshots`、`chat/steps/playback/*`、`providers?prefer=true` 等接口。历史查看因此变成了“重新回放任务”，如果 playback/SSE 没有正常结束或前端状态没有被完整落盘，页面就会一直显示 skeleton/loading。

## 日志证据

最新 `eigent_api` 日志显示，打开历史列表时只有一次 grouped history 请求：

```text
2026-06-17 09:30:30 GET /api/v1/chat/histories/grouped?include_tasks=true 200
```

随后在点击历史项目后，同一批历史 task 立即进入 playback 相关请求：

```text
2026-06-17 09:30:38 GET /api/v1/chat/snapshots?...=1781687512636-6665 200
2026-06-17 09:30:38 GET /api/v1/providers?prefer=true 200
2026-06-17 09:30:38 GET /api/v1/configs 200
2026-06-17 09:30:38 GET /api/v1/remote-sub-agent-providers?provider_name=gemini_agents&enabled=true 200
2026-06-17 09:30:38 GET /api/v1/chat/steps/playback/1781687512636-6665?delay_time=0 200

2026-06-17 09:30:38 GET /api/v1/chat/snapshots?...=1781687667692-5308 200
2026-06-17 09:30:38 GET /api/v1/providers?prefer=true 200
2026-06-17 09:30:38 GET /api/v1/configs 200
2026-06-17 09:30:38 GET /api/v1/remote-sub-agent-providers?provider_name=gemini_agents&enabled=true 200
2026-06-17 09:30:38 GET /api/v1/chat/steps/playback/1781687667692-5308?delay_time=0 200
```

之后仍有多次 `providers?prefer=true` 请求：

```text
2026-06-17 09:30:45 GET /api/v1/providers?prefer=true 200
2026-06-17 09:31:12 GET /api/v1/providers?prefer=true 200
2026-06-17 09:31:21 GET /api/v1/providers?prefer=true 200
2026-06-17 09:31:30 GET /api/v1/providers?prefer=true 200
2026-06-17 09:31:46 GET /api/v1/providers?prefer=true 200
2026-06-17 09:31:53 GET /api/v1/providers?prefer=true 200
2026-06-17 09:31:54 GET /api/v1/providers?prefer=true 200
```

说明问题不是简单的历史列表接口轮询，而是历史查看触发了 replay task 状态流和模型配置检查。

## 调用链

1. `src/components/GroupedHistoryView/index.tsx`
   - `loadProjects()` 调用 `fetchGroupedHistoryTasks(setProjects)`。
   - `useEffect(..., [refreshTrigger])` 只按 `refreshTrigger` 加载，不是持续轮询。

2. `src/components/GroupedHistoryView/ProjectGroup.tsx`
   - 点击项目后设置 `isLoadingProject=true`。
   - 调用 `loadProjectFromHistory(...)`。
   - 如果后续 replay/playback 没结束，用户会看到持续 loading。

3. `src/lib/replay.ts`
   - 注释声明：`Load project from history with final state (no animation)`。
   - 实际转调 `projectStore.loadProjectFromHistory(...)` 后再 `navigate('/')`。

4. `src/store/projectStore.ts`
   - `loadProjectFromHistory` 创建 `ProjectType.REPLAY` 项目。
   - 日志文案声明 `final state, no replay`。
   - 但每个 task 又执行：

```ts
await chatStore.getState().replay(taskId, question, 0);
```

5. `src/store/chatStore.ts`
   - `replay()` 调用 `startTask(taskId, 'replay', undefined, time)`。
   - `startTask(type === 'replay')` 会请求：
     - `/api/v1/chat/snapshots`
     - `/api/v1/providers?prefer=true`
     - `/api/v1/chat/steps/playback/{taskId}?delay_time=0`
   - `type === 'replay'` 时还会等待 `ssePromise`，这让历史加载和 SSE 结束状态强耦合。

## 根因

### P1：历史查看复用了 replay 实现

历史查看应该加载已完成的最终态，但当前实现把历史 task 当作 replay task 启动。结果是：

- 查看历史会产生 playback 请求。
- 查看历史会重新跑一遍前端任务状态机。
- 查看历史依赖 SSE/playback 正常完成。
- playback 任一环节卡住时，历史详情页一直显示 loading/skeleton。

### P2：历史页面存在重复历史请求风险

`src/pages/Projects/Project.tsx` 中存在：

```ts
useEffect(() => {
  if (!chatStore || !projectStore) return;
  fetchHistoryTasks(setHistoryTasks);
}, [chatStore, projectStore]);
```

这个 `_historyTasks` 状态当前未参与渲染，且 `GroupedHistoryView` 已经会请求 grouped history。若 `useChatStoreAdapter()` 返回对象引用随 store 更新变化，这个 effect 可能反复触发旧接口 `/api/v1/chat/histories`，造成“历史会话一直请求接口”的观感。

### P3：进入首页后模型配置检查过于频繁

`src/components/ChatBox/index.tsx` 的 `checkModelConfig()` 会请求 `/api/v1/providers?prefer=true`。它在 mount、`modelType` 变化、路径回到 `/` 时执行。历史项目加载完成后跳转到 `/`，再叠加 replay 状态更新，会放大 provider 请求数量。

## 建议修复

1. 拆分“查看历史最终态”和“显式回放”。
   - `loadProjectFromHistory` 不应调用 `chatStore.replay()`。
   - 不应触发 `startTask(type='replay')`。
   - 不应请求 `/api/v1/chat/steps/playback/*`。
   - 不应调用 `handleConfirmTask(..., 'replay')`。

2. 为历史查看增加独立的静态加载方法。
   - 优先使用后端已持久化的 history/snapshot/final-state 数据。
   - 一次性把 messages、taskRunning、taskAssigning、files、status、tokens 等最终态写入 store。
   - 项目类型建议使用 `ProjectType.NORMAL` 或新增 `ProjectType.HISTORY`，避免把普通查看历史标成 `REPLAY`。

3. 保留显式回放入口。
   - `replayProject()`、`replayActiveTask()` 或前端“回放”按钮可以继续走现有 playback/SSE 链路。
   - “点击历史项目查看”不能复用该链路。

4. 删除 `src/pages/Projects/Project.tsx` 中未使用的 `fetchHistoryTasks(setHistoryTasks)` effect。
   - 历史列表统一由 `GroupedHistoryView` 调 `fetchGroupedHistoryTasks`。
   - 可避免旧接口重复请求和不必要渲染。

5. 给 `GroupedHistoryView.loadProjects()` 加取消/卸载保护。
   - 避免组件卸载后继续 `setLoading(false)`。
   - 这不是主因，但能减少边界状态下的 loading 异常。

6. 对 `checkModelConfig()` 做缓存或节流。
   - 在模型配置已加载且未变化时，不要每次路径回到 `/` 都请求 provider。
   - 这能减少历史详情页进入后的额外接口噪音。

## 验收标准

1. 打开历史页面：
   - 只出现一次 `GET /api/v1/chat/histories/grouped?include_tasks=true`。
   - 不出现周期性 `/api/v1/chat/histories` 重复请求。

2. 点击已完成历史项目：
   - 不请求 `/api/v1/chat/steps/playback/*`。
   - 不因历史加载请求 `/api/v1/chat/snapshots`。
   - 不启动新的 task 或 SSE。
   - 页面直接渲染已完成消息、任务拆分、输出文件和状态。

3. 显式点击回放功能：
   - 仍然可以请求 `/api/v1/chat/steps/playback/*`。
   - 回放动画和任务状态机行为保持可用。

4. 网络面板稳定：
   - 静态查看历史时没有持续接口请求。
   - `providers?prefer=true` 不因历史状态更新持续触发。

## 建议优先级

优先修 `projectStore.loadProjectFromHistory -> chatStore.replay` 这条主链路。它直接解释了当前截图中的持续 skeleton/loading，也解释了日志中的 playback、snapshot 和 provider 请求。随后再清理 `Project.tsx` 的重复历史请求 effect，以及 `ChatBox` 的模型配置重复检查。

## 修复记录

修复时间：2026-06-17

已完成：

- `projectStore.loadProjectFromHistory` 不再创建 `ProjectType.REPLAY` 项目，也不再调用 `chatStore.replay(...)`。
- 新增 `chatStore.loadHistoryTask(...)`，使用一次性 `GET /api/v1/chat/steps?task_id=...` 静态还原历史 task 状态，不触发 `/api/v1/chat/steps/playback/*`、`/api/v1/chat/snapshots`、任务启动或自动确认。
- grouped history 入口传递完整 task 元数据，让静态加载可以使用每个 task 自己的 question、summary、tokens、status。
- 删除 `src/pages/Projects/Project.tsx` 中未参与渲染的 legacy `fetchHistoryTasks(setHistoryTasks)` effect，避免打开历史页时额外请求 `/api/v1/chat/histories`。
- `HistorySidebar` 的刷新 effect 改为依赖 `chatStore.updateCount` 标量，避免依赖整个 `chatStore` 引用。

验证：

- `npm run type-check` 通过。
- `npx eslint` 针对本次修改的 TS/TSX 文件通过，无 error/warning。
- 本地 `http://127.0.0.1:7777/history` 返回 200。Headless Web 烟测中出现 Electron IPC 缺失报错，这是 Web 环境缺少桌面端 preload API 导致，不属于本次历史加载链路。
