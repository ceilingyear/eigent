# 创建任务慢问题排查报告

日期：2026-06-17

## 结论摘要

当前“创建任务慢”主要不是 `TaskLock` 创建或 `/task/{id}/start` 入队慢，而是创建任务卡片前的后端编排链路过重：

1. `/chat` 入口只做环境设置、创建日志目录、创建或复用 `TaskLock`、入队 `ActionImproveData`，这部分代码路径很轻。
2. `step_solve` 收到 `Action.improve` 后，会先做复杂度判断。无附件时会调用一次 LLM；有附件时直接判定为复杂任务。
3. 一旦判定为复杂任务，会立即构建完整 workforce，包含 coordinator、task、new_worker、browser、developer、document、multi_modal、mcp 等多类 agent 和大量 toolkit。
4. 构建 workforce 后，task agent 再用较长 prompt 做任务拆解。日志样本显示单次拆解 prompt 约 3.3k 到 3.9k tokens，耗时 4.37s 到 12.18s。
5. 拆解完成后，还会额外创建 summary agent 并调用一次 LLM 生成任务标题/摘要，通常再增加约 1.8s 到 2.2s，最差会等到 10s 超时。
6. 前端收到 `to_sub_tasks` 后还有 30 秒自动确认定时器；如果用户没有手动点开始，会表现为“任务创建出来了但迟迟不运行”。
7. Docker 容器日志显示，本地任务执行过程中会向 `server` API 高频同步 `/api/v1/chat/steps`。这不是本地 agent 创建慢的直接证据，但会带来额外异步 HTTP/DB 写入压力，并解释了为什么 Docker API 日志里出现大量 chat step 请求。
8. 对“天气、新闻、官网信息、搜索一下”等联网检索需求，当前容易进入 Browser Agent 路径，导致浏览器/CDP/toolkit 初始化成本偏高。应增加轻量联网搜索路径，优先使用 Search Toolkit 或 Search Agent，只有需要登录、点击、翻页、下载、表单交互、动态页面读取时才启动 Browser Agent。

## 日志现状

仓库 `logs/` 目录只有前端启动日志：

- `logs/local-frontend.out.log`：Vite/Electron 启动、重启、构建耗时。
- `logs/local-frontend.err.log`：npm 配置警告。

后端业务日志当前没有落到文件，`backend/main.py` 只配置了控制台 `logging.basicConfig`。模型请求日志由 `/chat` 设置 `CAMEL_MODEL_LOG_ENABLED=true` 和 `CAMEL_LOG_DIR` 后写入用户目录：

- `C:\Users\25350\.eigent\admin\project_1781676770548-2035\task_1781676810186-5182\camel_logs`
- `C:\Users\25350\.eigent\admin\project_1781619442375-6229\task_1781619462440-5394\camel_logs`

这意味着当前只能精确量化 LLM 调用耗时，无法直接从文件日志量化 controller、queue、agent factory、toolkit 初始化各自耗时。

## Docker 日志补充

本机 Docker 当前运行的相关容器：

| 容器 | 镜像 | 状态 | 作用判断 |
| --- | --- | --- | --- |
| `eigent_api` | `server-api` | Up 16 hours, healthy | `server/` 目录的 FastAPI API，监听 `3001->5678` |
| `eigent_celery_worker` | `server-celery_worker` | Up 16 hours, healthy | 触发器/异步任务 worker |
| `eigent_celery_beat` | `server-celery_beat` | Up 16 hours | 定时触发器调度 |
| `eigent_postgres` | `postgres:15` | Up 27 hours, healthy | server 数据库 |
| `eigent-redis` | `redis:7-alpine` | Up 27 hours, healthy | server Redis / Celery broker |

对应 compose 文件在：

- `server/docker-compose.yml`
- `server/docker-compose.dev.yml`

### Docker 日志能看到什么

`eigent_api` 日志主要是 `server/` API 的访问日志，例如：

- `GET /api/v1/chat/histories/grouped?include_tasks=true`
- `POST /api/v1/chat/history`
- `POST /api/v1/chat/steps`
- `PUT /api/v1/chat/history/{id}`
- `POST /api/v1/chat/snapshots`

这些请求来自本地 agent 侧的同步逻辑。`backend/app/utils/server/sync_step.py` 在 `SERVER_URL` 配置存在且未禁用时，会把 SSE step 异步 POST 到 server 的 `/v1/chat/steps`。如果 `SERVER_URL` 指向 Docker API，就会在 `eigent_api` 日志里看到这些请求。

### Docker 日志不能看到什么

Docker 中的 `eigent_api` 不是当前 `backend/app/agent/listen_chat_agent.py` 所在的本地 agent 后端进程。它没有输出以下关键链路的详细日志：

- `question_confirm` 耗时
- `construct_workforce` 总耗时
- 每个 agent/toolkit 初始化耗时
- `workforce.eigent_make_sub_tasks` 内部分段耗时
- `summary_task` 是否阻塞 `to_sub_tasks`

因此 Docker 日志不能替代本地 backend 业务日志，只能补充“云端/服务端 step 同步流量”的证据。

### Docker 日志量化结果

近 24 小时 `eigent_api` 中 `/api/v1/chat/steps` 请求按分钟统计：

| Docker 日志分钟 | `/chat/steps` 请求数 |
| --- | ---: |
| 2026-06-16 14:17 | 8 |
| 2026-06-16 14:18 | 25 |
| 2026-06-16 14:20 | 12 |
| 2026-06-16 14:22 | 31 |
| 2026-06-16 14:23 | 11 |
| 2026-06-17 06:16 | 36 |
| 2026-06-17 06:17 | 13 |
| 2026-06-17 06:18 | 12 |

说明：

- Docker 日志时间看起来是 UTC；例如 `2026-06-17 06:16` 对应本地 Asia/Shanghai 的 `2026-06-17 14:16`，正好匹配 CAMEL 日志中的珠海天气任务时间段。
- `2026-06-17 06:16` 这一分钟有 36 次 `/chat/steps` 请求，说明任务执行期间 step 同步请求较密集。
- 同一分钟还出现了 `PUT /api/v1/chat/history/9`，`06:18:25` 附近出现多个 `POST /api/v1/chat/snapshots`。
- API 访问日志都是 200，未看到明显 5xx、timeout 或异常栈；Celery worker/beat 主要是每分钟触发器轮询，耗时通常 0.002s 到 0.011s，不是创建任务慢点。

### 对创建任务性能的影响判断

`sync_step.py` 使用 `asyncio.create_task(_send(...))` 异步发送 step，理论上不会阻塞 SSE 主生成器返回下一条消息。但它仍有三个风险：

1. 每个非 `decompose_text` step 都会创建一个 `httpx.AsyncClient(timeout=5.0)` 并发 POST，任务执行阶段请求会明显增多。
2. 如果 Docker API、网络或数据库变慢，后台 `_send` task 会堆积，可能增加事件循环压力和日志噪声。
3. 当前只有 Docker access log，没有记录单次 `/chat/steps` 的 server 处理耗时，无法判断 DB 写入是否成为局部瓶颈。

结论：Docker 日志没有推翻原结论。创建任务卡片前的主要慢点仍是本地后端的 LLM 拆解和 summary 阻塞；Docker 日志新增的风险点是 step 同步洪峰，建议纳入 P1/P2 优化。

## 运行流程拆解

### 1. 前端启动任务

位置：`src/store/chatStore.ts`

流程：

1. `startTask` 先调用 `waitForBackendReady(60000, 500)`。如果后端已经启动，这一步很快；如果后端冷启动或异常，会最多等待 60 秒。
2. 创建新的 chat store 实例和 task id。
3. 通过 `fetchEventSource` 发起 `/chat` SSE 请求。
4. 收到 `confirmed` 后记录 TTFT。
5. 收到 `decompose_text` 后流式更新拆解文本。
6. 收到 `to_sub_tasks` 后展示任务拆解结果，并设置 30 秒自动确认。
7. 自动确认或用户手动确认后，前端依次调用 `PUT /task/{project_id}` 和 `POST /task/{project_id}/start`。

关键点：

- `waitForBackendReady` 只影响后端未就绪场景。
- `to_sub_tasks` 之前的等待主要在后端 LLM 拆解和 summary。
- `to_sub_tasks` 之后的 30 秒自动确认会影响“开始执行”的体感速度。

### 2. `/chat` 后端入口

位置：`backend/app/controller/chat_controller.py`

流程：

1. `get_or_create_task_lock(data.project_id)` 创建或复用项目级 `TaskLock`。
2. 设置用户环境变量、模型配置、搜索配置。
3. 创建 `CAMEL_LOG_DIR`。
4. `set_current_task_id(data.project_id, data.task_id)`。
5. 将 `ActionImproveData` 放入 `task_lock.queue`。
6. 返回 `StreamingResponse(step_solve(...))`。

判断：

- 这部分没有明显重 CPU 或网络调用。
- `load_dotenv`、`mkdir`、环境变量设置有成本，但不是主要慢点。

### 3. `step_solve` 处理新问题

位置：`backend/app/service/chat_service.py`

流程：

1. 初始化或复用 `question_agent`。
2. 从 queue 取 `ActionImproveData`。
3. 检查 conversation history 长度。
4. 判定复杂度：
   - 有附件：直接 `is_complex_task = True`。
   - 无附件：调用 `question_confirm(question_agent, question, task_lock)`，触发一次 LLM。
5. 简单任务：直接用 `question_agent.step` 回答，不创建 workforce。
6. 复杂任务：
   - 发送 `confirmed` SSE。
   - 构造 coordinator context。
   - 如果没有 workforce，则调用 `construct_workforce(options)`。
   - 创建 `camel_task`。
   - 后台执行 `workforce.eigent_make_sub_tasks(...)`。
   - 拆解完成后再调用 `summary_task(...)`。
   - 最后发送 `to_sub_tasks`。

关键慢点：

- `question_confirm` 是额外 LLM 调用。
- 有附件直接复杂，绕过简单路径。
- `construct_workforce` 在任务卡片出现前创建所有 agent。
- `eigent_make_sub_tasks` 依赖 task agent 的大 prompt LLM 请求。
- `summary_task` 是第二次 LLM 请求，且在发送最终 `to_sub_tasks` 前同步等待。

### 4. workforce 构建

位置：`backend/app/service/chat_service.py`

`construct_workforce` 已经使用 `asyncio.gather` 和 `asyncio.to_thread` 并行创建 agent，但仍会等待最慢的 agent 初始化完成。当前会一次性创建：

- coordinator agent
- task agent
- new worker agent
- browser agent
- developer agent
- document agent
- multi-modal agent
- mcp agent

这些 agent 又会初始化不同 toolkit，例如 browser/CDP、terminal、screenshot、skill、document、excel、Google Drive MCP、MCP search 等。即使用户只是查天气或转换一个文件，也会先构建完整 workforce。

### 5. 任务拆解

位置：`backend/app/utils/workforce.py`

流程：

1. `eigent_make_sub_tasks` 校验 task content。
2. `reset` workforce，设置 channel 和 state。
3. 调用 `handle_decompose_append_task`。
4. `_decompose_task` 拼接 CAMEL `TASK_DECOMPOSE_PROMPT`，其中包含长规则、示例和所有 worker 能力信息。
5. task agent 调用模型输出 `<tasks>`。
6. 后端流式发送 `decompose_text`，最终发送 `to_sub_tasks`。

## 真实日志样本

### 样本 A：查询珠海天气

目录：`C:\Users\25350\.eigent\admin\project_1781676770548-2035\task_1781676810186-5182\camel_logs`

| 时间 | 阶段推断 | 耗时 | Prompt tokens | Total tokens | Cache hit/miss |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-06-17 14:13:33.001 | 复杂度判断或简单问答 | 2.08s | 283 | 333 | 0 / 283 |
| 2026-06-17 14:16:11.657 | 任务拆解 | 4.37s | 3340 | 3619 | 0 / 3340 |
| 2026-06-17 14:16:17.493 | 任务摘要 | 2.19s | 305 | 450 | 0 / 305 |
| 2026-06-17 14:16:43.075 起 | 执行阶段 agent 调用 | 2s 到 8s/次 | 1.7k 到 48.6k | 2.2k 到 48.8k | 部分命中 |

说明：

- “帮我查一下今天珠海天气”被拆成了一个搜索天气的子任务。
- 创建任务卡片前至少包含拆解 4.37s + 摘要 2.19s，不含 workforce 初始化耗时。
- 拆解 prompt 3340 tokens，且 prompt cache hit 为 0。

### 样本 B：将 Excel 转成 Markdown

目录：`C:\Users\25350\.eigent\admin\project_1781619442375-6229\task_1781619462440-5394\camel_logs`

| 时间 | 阶段推断 | 耗时 | Prompt tokens | Total tokens | Cache hit/miss |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-06-16 22:17:53.044 | 任务拆解 | 6.13s | 3374 | 3859 | 0 / 3374 |
| 2026-06-16 22:18:01.803 | 任务摘要 | 1.86s | 347 | 434 | 0 / 347 |
| 2026-06-16 22:18:56.000 | 执行阶段大上下文调用 | 102.40s | 179195 | 197189 | 14080 / 165115 |
| 2026-06-16 22:22:06.311 | 多轮新任务拆解 | 12.18s | 3928 | 5050 | 0 / 3928 |

说明：

- 第一次创建任务卡片前，拆解和摘要合计约 7.99s，不含 workforce 初始化。
- 后续执行阶段出现 179k 到 197k tokens 的请求，说明上下文可能被文件内容或历史结果放大。
- 多轮任务的拆解 prompt 会带历史上下文，耗时升到 12.18s。

## 根因分析

### 根因 1：任务卡片出现前做了过多同步前置工作

复杂任务路径下，后端在发送 `to_sub_tasks` 前必须完成：

1. 复杂度判断或附件直接复杂判定。
2. 完整 workforce 初始化。
3. task agent 拆解。
4. summary agent 生成任务标题/摘要。

其中第 3、4 步有真实日志证明至少会消耗 6s 到 12s；第 2 步缺少分段日志，但从代码看包含大量 agent/toolkit 初始化，是明确风险点。

### 根因 2：完整 workforce 被 eager 初始化

创建任务卡片只需要“可用 worker 能力描述”和 task agent 拆解结果，但当前会初始化所有 worker agent 和 toolkit。对于单一类型任务，这些 agent 中大部分不会在拆解阶段真正执行。

### 根因 3：拆解 prompt 过大

当前拆解 prompt 包含：

- 长任务拆解规则。
- 多个示例。
- 完整 worker 列表。
- 每个 worker 的 toolkit 和工具函数列表。
- 历史对话和附件信息。

日志中即使简单任务也有 3340 tokens 的拆解 prompt。多轮任务会进一步膨胀到 3928 tokens 甚至更高。

### 根因 4：摘要生成阻塞最终 `to_sub_tasks`

`run_decomposition` 在拆解后调用 `summary_task_agent`，并 `await asyncio.wait_for(..., timeout=10)`。也就是说最终任务卡片会被第二次 LLM 调用拖住。

### 根因 5：附件直接复杂，缺少轻量路由

`len(attaches_to_use) > 0` 直接进入复杂任务路径。带附件的简单转换、摘要、读取类任务没有机会走轻量路径；即使只需要 Document Agent，也会先构建完整 workforce。

### 根因 6：前端自动确认延迟影响开始执行体感

前端在 `to_sub_tasks` 后设置 30 秒自动确认。若用户没有手动开始，执行会延迟 30 秒。这个不是“创建任务卡片慢”的后端根因，但会造成“创建任务后很久才开始”的体验问题。

### 根因 7：缺少端到端耗时埋点

当前后端业务日志没有落盘，且关键函数缺少统一 span id 和耗时字段。排查只能依赖 CAMEL 模型日志反推，无法回答：

- `construct_workforce` 各 agent 初始化分别耗时多少。
- Google Drive MCP / MCP tools / CDP 初始化是否慢。
- queue 入队到 step_solve 取出耗时多少。
- 前端 `confirmed` 到首个 `decompose_text` 的 TTFT 是否稳定。

### 根因 8：step 同步请求高频发送到 Docker API

Docker `eigent_api` access log 显示，本地执行任务时会在短时间内向 `/api/v1/chat/steps` 发送多次请求。例如 `2026-06-17 06:16` 一分钟内有 36 次。

这不是创建任务卡片前最主要的慢点，因为 `_send` 是异步后台 task；但如果远端 server 或本地 Docker API 变慢，可能造成后台请求堆积、事件循环压力、DB 写入压力和日志噪声。当前 `sync_step.py` 已默认不同步 `decompose_text`，但非 `decompose_text` 事件仍是一条 step 一个 HTTP 请求。

### 根因 9：联网检索与浏览器操作没有分层

项目里已有 `SearchToolkit.search_google`，可以通过用户配置的 Google Search API 或 cloud search 执行轻量检索。但当前 workforce 初始化会创建 Browser Agent，Browser Agent 又包含 Hybrid Browser/CDP、搜索、终端、笔记等工具。对于“查天气”“查新闻”“搜索官网信息”这类只需要搜索结果和摘要的任务，启动 Browser Agent 的成本明显高于直接搜索 API。

更合理的分层是：

- Search：只需要搜索结果、摘要、链接、最新公开信息时使用，不打开浏览器。
- Browser：需要网页交互、登录态、点击、滚动、动态内容、下载文件、截图验证时使用。
- Developer/Document：需要代码、文件、文档处理时再进入对应 worker。

如果不做这层路由，联网类简单任务会持续走重路径，影响创建任务和执行启动速度。

## 优化方案

### P0：先补齐耗时观测

目标：能直接从日志看到每个阶段耗时。

建议新增统一计时日志，字段包括 `project_id`、`task_id`、`stage`、`duration_ms`、`tokens`、`cache_hit_tokens`、`cache_miss_tokens`。

建议埋点阶段：

- `chat.post.total`
- `task_lock.create_or_get`
- `queue.put.initial_improve`
- `step_solve.wait_queue`
- `question_confirm.llm`
- `construct_workforce.total`
- `construct_workforce.agent.coordinator_task`
- `construct_workforce.agent.new_worker`
- `construct_workforce.agent.browser`
- `construct_workforce.agent.developer`
- `construct_workforce.agent.document`
- `construct_workforce.agent.multi_modal`
- `construct_workforce.agent.mcp`
- `decompose.total`
- `decompose.first_token`
- `summary_task.llm`
- `to_sub_tasks.emit`
- `task.start.queue`
- `workforce.start.total`

同时建议把 backend 标准日志落到文件，例如：

- `~/.eigent/{email}/project_{project_id}/task_{task_id}/backend.log`
- 或 `backend/runtime/logs/backend.log`

### P1：让任务卡片先出来

短期收益最大。

1. 拆解完成后立即发送 `to_sub_tasks`，不要等待 `summary_task`。
2. `summary_task` 改成后台异步更新：先用 `camel_task.content[:80]` 或第一个子任务生成临时标题，LLM 摘要完成后再发一个 `summary_task_updated` 或复用现有事件更新。
3. 对单子任务场景，直接用子任务内容生成标题，不再调用 summary LLM。

预期收益：创建任务卡片减少约 1.8s 到 2.2s，最差避免 10s summary timeout。

### P1：惰性构建 worker agent

当前 `construct_workforce` 在拆解前创建完整 worker。建议拆成两层：

1. `WorkerCatalog`：静态能力描述，不初始化 toolkit，不创建 model。
2. `RuntimeWorker`：用户确认任务并真正要执行时，按子任务类型或被选中 worker 懒加载。

拆解阶段只需要：

- task agent
- coordinator agent 或 task agent
- 轻量 worker catalog

执行阶段才创建：

- 被分配到的 worker agent
- 该 worker 需要的 toolkit

预期收益：减少首屏任务创建等待，尤其是 Browser/Document/MCP/Google Drive/CDP 初始化成本。

### P1：压缩任务拆解 prompt

优化方向：

1. 将长规则和示例改为短系统提示，减少重复示例。
2. worker 能力只传高层描述，不传所有工具函数名。
3. 有附件时只传附件摘要、类型、路径，不把历史结果全文塞入拆解 prompt。
4. 多轮任务只传最近必要上下文，历史任务结果改成摘要。
5. 将静态提示前缀稳定化，提高 prompt cache 命中率。

预期收益：降低 3k 到 4k token 的基础拆解成本，减少多轮上下文膨胀。

### P1：增加轻量任务路由

在进入完整 workforce 前增加一个低成本路由：

- 明确简单问答：直接 `question_agent` 回答。
- 单附件转换/摘要：直接创建一个 Document Agent 子任务，或只初始化 Document Agent。
- 网页/天气/搜索类单步：优先走轻量联网搜索工具或 Search Agent，不启动 Browser Agent。
- 代码修改类：只初始化 Developer Agent。

路由可以先用规则加小模型：

- 附件扩展名 `.docx/.xlsx/.pdf/.csv` + “总结/转换/提取” → document-only。
- “天气/搜索/查一下/官网/新闻/今天/最新/价格/政策/航班/比赛结果” → search-only。
- “打开网页/点击/登录/下载/截图/滚动/填写表单/比对页面” → browser-only。
- “改代码/修复/实现/运行测试” → developer-only。

预期收益：避免“所有任务都先启动全量 workforce”。

### P1：新增轻量联网搜索能力，优先于 Browser Agent

目标：当判断出用户需求需要联网搜索时，优先使用联网搜索工具或轻量 Search Agent，而不是启动 Browser Agent 打开浏览器查询。

建议实现：

1. 新增 `Search Agent` 或 `search_only` 执行路径，只挂载 `SearchToolkit.search_google` 和必要的摘要模型，不挂载 Hybrid Browser/CDP。
2. 在 `question_confirm` 之后、`construct_workforce` 之前增加 `route_task_intent`：
   - `no_tool`：直接回答。
   - `search_only`：调用联网搜索工具，返回摘要和来源。
   - `document_only`：只初始化 Document Agent。
   - `developer_only`：只初始化 Developer Agent。
   - `browser_required`：需要真实网页交互时才启动 Browser Agent。
   - `full_workforce`：复杂、多工具、多步骤任务回退原路径。
3. Search Agent 输出必须包含：
   - 用户语言的直接答案。
   - 关键来源链接。
   - 搜索时间或数据时间，避免天气/新闻/价格类信息时效不清。
4. 对搜索结果不足、来源冲突、需要登录或动态页面的情况，Search Agent 再升级到 Browser Agent。
5. 优先复用现有 `backend/app/agent/toolkit/search_toolkit.py`，避免新引入浏览器会话成本。

建议判定规则：

| 用户意图 | 优先路径 | 是否需要 Browser Agent |
| --- | --- | --- |
| 天气、新闻、百科、官网信息、价格、政策、比赛结果 | `search_only` | 否 |
| 搜索多个来源并总结 | `search_only` | 默认否 |
| 打开某网页并点击/滚动/截图 | `browser_required` | 是 |
| 需要登录态、Cookie、表单提交 | `browser_required` | 是 |
| 需要下载网页文件后处理 | 先 `search_only` 找链接，再按需 Browser/Document | 视情况 |

预期收益：查询天气、新闻、官网资料等任务不再初始化 Browser/CDP 和完整 workforce，创建任务等待可减少到一次轻量搜索和一次摘要模型调用，或直接在简单回复路径返回结果。

### P2：调整前端自动确认策略

当前自动确认固定 30 秒。建议：

1. 单子任务且用户未编辑时，3 秒到 5 秒自动确认。
2. 低风险任务可立即自动确认，并提供撤回或暂停。
3. UI 展示倒计时，避免用户误以为卡住。
4. 对多子任务或高风险任务保留 30 秒。

### P2：减少同步 API 和事件循环开销

`task_controller.py` 中同步 FastAPI handler 使用 `asyncio.run(task_lock.put_queue(...))`。建议改为 async endpoint 并直接 `await task_lock.put_queue(...)`。

这不是主要慢点，但可以减少每次请求创建事件循环的额外开销，也更符合 async 服务模型。

### P2：优化 step 同步到 server 的策略

结合 Docker 日志，建议调整 `backend/app/utils/server/sync_step.py`：

1. 对所有 step 类型做批量发送，不只批量 `decompose_text`。
2. 复用单例 `httpx.AsyncClient`，避免每条 step 新建 client。
3. 增加并发上限和内存队列上限，避免 server 变慢时无限堆积。
4. 增加丢弃或降级策略：本地 UI SSE 优先，云端 step 同步失败不能影响主任务。
5. Docker `server` 侧为 `/api/v1/chat/steps` 加请求耗时日志和批量写入接口。

建议新增环境开关：

- `DISABLE_STEP_SYNC=true`：本地排查性能时关闭同步。
- `SYNC_STEP_BATCH_SIZE=20`
- `SYNC_STEP_FLUSH_INTERVAL_SECONDS=1`
- `SYNC_STEP_MAX_IN_FLIGHT=4`

### P2：模型分工

为不同阶段配置不同模型：

- 复杂度判断：小模型或规则，目标 < 500ms。
- 拆解：快模型，短输出。
- 执行：保持当前高能力模型。
- summary：本地模板或小模型后台异步。

## 建议实施顺序

1. 加耗时埋点和 backend 文件日志。
2. `summary_task` 从阻塞链路移出，先发 `to_sub_tasks`。
3. 把 30 秒自动确认改成按任务风险分级。
4. 新增轻量联网搜索路由：搜索类任务优先走 Search Toolkit/Search Agent，不启动 Browser Agent。
5. 压缩拆解 prompt 和 worker 描述。
6. 优化 `sync_step`：批量、复用 client、并发限流，排查时允许关闭。
7. 引入 document-only、developer-only、browser-required 等轻量路由，跳过完整 workforce。
8. 重构 workforce 为 catalog + lazy runtime worker。

## 验收指标

建议用以下指标衡量优化：

- `POST /chat` 到 `confirmed`：P95 < 500ms。
- `confirmed` 到首个 `decompose_text`：P95 < 1500ms。
- `confirmed` 到 `to_sub_tasks`：P95 < 4000ms。
- 单子任务场景 `to_sub_tasks` 到自动开始：P95 < 5000ms。
- 搜索类任务不启动 Browser Agent：P95 > 90%。
- 搜索类任务 `POST /chat` 到首个用户可见结果：P95 < 3000ms。
- 拆解 prompt tokens：常规任务 < 1500，多轮任务 < 2500。
- `construct_workforce.total`：拆解前路径为 0 或 < 1000ms。
- `/api/v1/chat/steps` 同步请求：常规任务 < 10 次/分钟，批量接口 P95 < 200ms。

## 风险与注意事项

- lazy worker 会改变 agent 创建时机，需要确认 `create_agent` SSE 与前端展示逻辑兼容。
- 路由规则不能改变用户任务语义；规则不确定时应回退到原复杂路径。
- search-only 不能替代需要网页交互的任务；遇到登录、点击、动态内容、下载、截图等需求应升级到 Browser Agent。
- 搜索类答案需要带来源链接和检索时间，避免最新信息不可追溯。
- summary 异步化后，前端需要支持标题后置更新或接受临时标题。
- prompt 压缩需要回归测试，避免拆解质量下降。
