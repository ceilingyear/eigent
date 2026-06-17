# 任务处理全流程图

日期：2026-06-17

## 全流程

```mermaid
flowchart TD
    A[用户输入任务] --> B[前端 startTask]
    B --> C{是否需要等待本地后端就绪}
    C -- 是 --> C1[waitForBackendReady<br/>最多等待 60s]
    C -- 否 --> D[创建/复用前端任务卡片<br/>设置 isPending]
    C1 --> D

    D --> E[组装 Chat 请求参数<br/>模型/工具/附件/语言/项目与任务 ID]
    E --> F[POST /chat<br/>建立 SSE 连接]

    F --> G[chat_controller.start_chat]
    G --> H[设置 CAMEL 日志目录与环境变量]
    H --> I[创建或复用 TaskLock<br/>set_current_task_id]
    I --> J[向 TaskLock.queue 入队 ActionImproveData]
    J --> K[返回 StreamingResponse<br/>step_solve 持续消费队列]

    K --> L[step_solve 收到 Action.improve]
    L --> M[写入用户消息到会话历史]
    M --> N{是否有附件}
    N -- 有附件 --> P[直接判定为复杂任务]
    N -- 无附件 --> O[question_confirm 调用 LLM 判断复杂度]
    O --> Q{复杂任务?}
    Q -- 否 --> R[question_agent 直接回答]
    R --> S[SSE wait_confirm<br/>前端展示简单回复]
    S --> Z1[本轮结束或等待用户继续输入]

    Q -- 是 --> P
    P --> T[SSE confirmed<br/>前端进入任务创建/拆解状态]
    T --> U[build_context_for_workforce<br/>合并历史、附件、工作目录、语言策略]
    U --> V{已有 workforce?}
    V -- 有 --> W[复用 workforce]
    V -- 无 --> X[construct_workforce<br/>并行创建 coordinator/task/new_worker/browser/developer/document/multi_modal/mcp agents]
    X --> W

    W --> Y[创建 CAMEL Task]
    Y --> AA[后台 run_decomposition]
    AA --> AB[workforce.eigent_make_sub_tasks]
    AB --> AC[handle_decompose_append_task]
    AC --> AD[_decompose_task<br/>task_agent 调用 LLM 生成子任务]
    AD --> AE[SSE decompose_text<br/>前端流式展示拆解文本]
    AD --> AF[得到 subtasks]
    AF --> AG[summary_task<br/>生成任务名与摘要]
    AG --> AH[SSE to_sub_tasks<br/>携带 sub_tasks、summary_task、is_final]

    AH --> AI[前端展示可编辑子任务]
    AI --> AJ{用户手动确认?}
    AJ -- 是 --> AK[handleConfirmTask]
    AJ -- 否 --> AL[30s 自动确认定时器]
    AL --> AK

    AK --> AM[PUT /task/{project_id}<br/>保存编辑后的子任务]
    AM --> AN[POST /task/{project_id}/start]
    AN --> AO[后端 TaskLock.queue 入队 start 信号]
    AO --> AP[step_solve 收到 Action.start]
    AP --> AQ[workforce.eigent_start(sub_tasks)]

    AQ --> AR[Workforce 调度 pending_tasks]
    AR --> AS[Coordinator 选择合适 worker]
    AS --> AT[Browser/Developer/Document/Multi-modal/MCP/New worker 执行工具或模型步骤]
    AT --> AU[SSE agent/tool/task_state<br/>前端实时更新步骤、工具、token、状态]
    AU --> AV{子任务全部完成?}
    AV -- 否 --> AR
    AV -- 是 --> AW[聚合 task.result]

    AW --> AX{是否多子任务}
    AX -- 是 --> AY[summary_subtasks_result<br/>汇总多个子任务结果]
    AX -- 否 --> AZ[直接取单子任务结果]
    AY --> BA[SSE end]
    AZ --> BA
    BA --> BB[前端写入最终消息<br/>isPending=false]
    BB --> BC[同步 chat history / snapshots / steps]
    BC --> BD[任务完成]

    AU --> BE{执行中用户追加任务?}
    BE -- 是 --> BF[SSE new_task_state]
    BF --> BG[暂停/复用 workforce]
    BG --> BH[对新任务重新 question_confirm]
    BH --> BI{新任务复杂?}
    BI -- 否 --> BJ[直接回答 wait_confirm<br/>恢复 workforce]
    BI -- 是 --> BK[handle_decompose_append_task<br/>追加拆解新子任务]
    BK --> AH
```

## 关键事件对应

| 阶段 | 后端事件 / 接口 | 前端处理 |
| --- | --- | --- |
| 建立任务 | `POST /chat` | `fetchEventSource` 建立 SSE |
| 任务确认 | `confirmed` | 记录 TTFT 起点，切换任务状态 |
| 拆解流式文本 | `decompose_text` | 追加显示拆解过程 |
| 子任务生成完成 | `to_sub_tasks` | 展示可编辑子任务，启动 30s 自动确认 |
| 用户确认执行 | `PUT /task/{project_id}` + `POST /task/{project_id}/start` | 保存子任务并启动执行 |
| 执行过程 | `agent_state` / `tool_state` / `task_state` | 实时展示 agent、工具调用、子任务状态 |
| 多轮追加任务 | `new_task_state` | 新建/切换任务卡片，重新拆解或直接回答 |
| 最终结果 | `end` | 写入最终消息，结束 pending 状态 |

## 主要耗时点

1. `waitForBackendReady`：前端最多等待 60 秒。
2. `question_confirm`：无附件复杂度判断会调用一次 LLM。
3. `construct_workforce`：首次复杂任务会创建完整 agent/toolkit 集合。
4. `_decompose_task`：任务拆解 LLM 调用，生成子任务。
5. `summary_task`：拆解完成后额外生成任务名和摘要。
6. 前端自动确认：用户不手动点击时，会等待 30 秒后自动执行。
7. 执行阶段：取决于子任务数量、工具调用次数、网页/文件/模型耗时。
