# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========

import asyncio
import datetime
import json
import logging
import platform
from pathlib import Path
from typing import Any

from camel.models import ModelProcessingError
from camel.tasks import Task
from camel.toolkits import ToolkitMessageIntegration
from camel.types import ModelPlatformType
from fastapi import Request
from inflection import titleize
from pydash import chain

from app.agent.agent_model import agent_model
from app.agent.factory import (
    browser_agent,
    developer_agent,
    document_agent,
    mcp_agent,
    multi_modal_agent,
    question_confirm_agent,
    task_summary_agent,
)
from app.agent.factory.remote_sub_agent import (
    attach_remote_sub_agent_if_enabled,
    remote_sub_agent_enabled,
)
from app.agent.language import (
    build_output_language_policy,
    get_output_language,
    get_task_label,
)
from app.agent.listen_chat_agent import ListenChatAgent
from app.agent.prompt import build_remote_sub_agent_planning_notice
from app.agent.toolkit.human_toolkit import HumanToolkit
from app.agent.toolkit.note_taking_toolkit import NoteTakingToolkit
from app.agent.toolkit.search_toolkit import SearchToolkit
from app.agent.toolkit.skill_toolkit import SkillToolkit
from app.agent.toolkit.terminal_toolkit import TerminalToolkit
from app.agent.tools import get_mcp_tools, get_toolkits
from app.component.environment import env
from app.model.chat import Chat, NewAgent, Status, TaskContent, sse_json
from app.service.task import (
    Action,
    ActionActivateToolkitData,
    ActionDecomposeProgressData,
    ActionDecomposeTextData,
    ActionDeactivateToolkitData,
    ActionImproveData,
    ActionInstallMcpData,
    ActionNewAgent,
    Agents,
    TaskLock,
    delete_task_lock,
    set_current_task_id,
)
from app.utils.event_loop_utils import set_main_event_loop
from app.utils.file_utils import get_working_directory, list_files
from app.utils.server.sync_step import sync_step
from app.utils.telemetry.workforce_metrics import WorkforceMetricsCallback
from app.utils.workforce import Workforce

logger = logging.getLogger("chat_service")

SEARCH_ONLY_KEYWORDS = (
    "天气",
    "新闻",
    "搜索",
    "搜一下",
    "查一下",
    "查询",
    "官网",
    "最新",
    "今天",
    "现在",
    "当前",
    "价格",
    "汇率",
    "政策",
    "航班",
    "比赛",
    "赛程",
    "search",
    "lookup",
    "weather",
    "news",
    "latest",
    "today",
    "current",
    "price",
    "official site",
)

BROWSER_REQUIRED_KEYWORDS = (
    "打开",
    "点击",
    "登录",
    "登陆",
    "下载",
    "截图",
    "滚动",
    "填写",
    "提交",
    "表单",
    "爬取",
    "抓取正文",
    "open",
    "click",
    "login",
    "download",
    "screenshot",
    "scroll",
    "form",
    "submit",
)


def should_use_search_only(question: str, attaches: list[str]) -> bool:
    if attaches:
        return False
    normalized = question.strip().lower()
    if not normalized:
        return False
    if any(keyword in normalized for keyword in BROWSER_REQUIRED_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in SEARCH_ONLY_KEYWORDS)


def search_config_available() -> bool:
    has_user_google = bool(env("GOOGLE_API_KEY") and env("SEARCH_ENGINE_ID"))
    has_cloud_search = bool(env("cloud_api_key") and env("SERVER_URL"))
    return has_user_google or has_cloud_search


def build_search_toolkit_event(
    action: type[ActionActivateToolkitData] | type[ActionDeactivateToolkitData],
    task_id: str,
    message: str,
):
    return action(
        data={
            "agent_name": "search_agent",
            "process_task_id": task_id,
            "toolkit_name": SearchToolkit.toolkit_name(),
            "method_name": "search_google",
            "message": message,
        }
    )


def compact_search_results(results: Any, max_results: int = 5) -> list[dict]:
    if isinstance(results, dict):
        candidates = (
            results.get("items")
            or results.get("results")
            or results.get("organic_results")
            or []
        )
    elif isinstance(results, list):
        candidates = results
    else:
        candidates = []

    compacted = []
    for item in candidates[:max_results]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "title": item.get("title") or item.get("name") or "",
                "link": item.get("link")
                or item.get("url")
                or item.get("href")
                or "",
                "snippet": item.get("snippet")
                or item.get("description")
                or item.get("summary")
                or "",
            }
        )
    return compacted


async def build_search_only_answer(
    question_agent: ListenChatAgent,
    question: str,
    options: Chat,
    task_lock: TaskLock,
) -> str | None:
    if not search_config_available():
        logger.info(
            "Search-only route skipped: no search configuration",
            extra={"project_id": options.project_id, "task_id": options.task_id},
        )
        return None

    search_toolkit = SearchToolkit(
        options.project_id, agent_name="search_agent"
    )
    raw_results = await asyncio.wait_for(
        asyncio.to_thread(
            search_toolkit.search_google_raw,
            question,
            "web",
            1,
            1,
        ),
        timeout=8,
    )
    search_results = compact_search_results(raw_results)
    if not search_results:
        logger.info(
            "Search-only route returned no results",
            extra={"project_id": options.project_id, "task_id": options.task_id},
        )
        return None

    language_policy = build_output_language_policy(options.language)
    prompt = f"""{language_policy}

用户问题：
{question}

联网搜索结果（JSON）：
{json.dumps(search_results, ensure_ascii=False)}

请基于搜索结果直接回答用户。要求：
1. 回答必须使用{get_output_language(options.language)}。
2. 对天气、新闻、价格、政策等时效信息，说明这是基于当前联网搜索结果。
3. 保留关键数值、单位、日期和地点。
4. 最后用简短“来源”列出用到的链接。
5. 如果搜索结果不足以可靠回答，说明信息不足，不要编造。
"""
    resp = question_agent.step(prompt)
    if resp and resp.msgs:
        answer = resp.msgs[0].content
    else:
        answer = ""
    if not answer:
        return None

    task_lock.add_conversation("assistant", answer)
    task_lock.last_task_result = answer
    task_lock.status = Status.done
    return answer


def format_task_context(
    task_data: dict, seen_files: set | None = None, skip_files: bool = False
) -> str:
    """Format structured task data into a readable context string.

    Args:
        task_data: Dictionary containing task content, result,
            and working directory
        seen_files: Optional set to track already-listed files
            and avoid duplicates (deprecated, use skip_files
            instead)
        skip_files: If True, skip the file listing entirely
    """
    context_parts = []

    if task_data.get("task_content"):
        context_parts.append(f"Previous Task: {task_data['task_content']}")

    if task_data.get("task_result"):
        context_parts.append(
            f"Previous Task Result: {task_data['task_result']}"
        )

    # Skip file listing if requested
    if not skip_files:
        working_directory = task_data.get("working_directory")
        if working_directory:
            try:
                generated_files = list_files(
                    working_directory,
                    base=working_directory,
                    skip_dirs={"node_modules", "__pycache__", "venv"},
                    skip_extensions=(".pyc", ".tmp"),
                    skip_prefix=".",
                )
                if seen_files is not None:
                    generated_files = [
                        p for p in generated_files if p not in seen_files
                    ]
                    seen_files.update(generated_files)
                if generated_files:
                    context_parts.append("Generated Files from Previous Task:")
                    for file_path in sorted(generated_files):
                        context_parts.append(f"  - {file_path}")
            except Exception as e:
                logger.warning(f"Failed to collect generated files: {e}")

    return "\n".join(context_parts)


def collect_previous_task_context(
    working_directory: str,
    previous_task_content: str,
    previous_task_result: str,
    previous_summary: str = "",
) -> str:
    """
    Collect context from previous task including content, result,
    summary, and generated files.

    Args:
        working_directory: The working directory to scan for generated files
        previous_task_content: The content of the previous task
        previous_task_result: The result/output of the previous task
        previous_summary: The summary of the previous task

    Returns:
        Formatted context string to prepend to new task
    """

    context_parts = []

    # Add previous task information
    context_parts.append("=== CONTEXT FROM PREVIOUS TASK ===\n")

    # Add previous task content
    if previous_task_content:
        context_parts.append(f"Previous Task:\n{previous_task_content}\n")

    # Add previous task summary
    if previous_summary:
        context_parts.append(f"Previous Task Summary:\n{previous_summary}\n")

    # Add previous task result
    if previous_task_result:
        context_parts.append(
            f"Previous Task Result:\n{previous_task_result}\n"
        )

    # Collect generated files from working directory (safe listing)
    try:
        generated_files = list_files(
            working_directory,
            base=working_directory,
            skip_dirs={"node_modules", "__pycache__", "venv"},
            skip_extensions=(".pyc", ".tmp"),
            skip_prefix=".",
        )
        if generated_files:
            context_parts.append("Generated Files from Previous Task:")
            for file_path in sorted(generated_files):
                context_parts.append(f"  - {file_path}")
            context_parts.append("")
    except Exception as e:
        logger.warning(f"Failed to collect generated files: {e}")

    context_parts.append("=== END OF PREVIOUS TASK CONTEXT ===\n")

    return "\n".join(context_parts)


def check_conversation_history_length(
    task_lock: TaskLock, max_length: int = 200000
) -> tuple[bool, int]:
    """
    Check if conversation history exceeds maximum length

    Returns:
        tuple: (is_exceeded, total_length)
    """
    if (
        not hasattr(task_lock, "conversation_history")
        or not task_lock.conversation_history
    ):
        return False, 0

    total_length = 0
    for entry in task_lock.conversation_history:
        total_length += len(entry.get("content", ""))

    is_exceeded = total_length > max_length

    if is_exceeded:
        logger.warning(
            f"Conversation history length {total_length} "
            f"exceeds maximum {max_length}"
        )

    return is_exceeded, total_length


def build_conversation_context(
    task_lock: TaskLock,
    header: str = "=== CONVERSATION HISTORY ===",
    include_generated_files: bool = True,
) -> str:
    """Build conversation context from task_lock history
    with files listed only once at the end.

    Args:
        task_lock: TaskLock containing conversation history
        header: Header text for the context section

    Returns:
        Formatted context string with task history
        and files listed once at the end
    """
    context = ""
    working_directories = set()  # Collect all unique working directories

    if task_lock.conversation_history:
        context = f"{header}\n"

        for entry in task_lock.conversation_history:
            if entry["role"] == "task_result":
                if isinstance(entry["content"], dict):
                    formatted_context = format_task_context(
                        entry["content"], skip_files=True
                    )
                    context += formatted_context + "\n\n"
                    if entry["content"].get("working_directory"):
                        working_directories.add(
                            entry["content"]["working_directory"]
                        )
                else:
                    context += entry["content"] + "\n"
            elif entry["role"] == "assistant":
                context += f"Assistant: {entry['content']}\n\n"

        if include_generated_files and working_directories:
            all_generated_files: set[str] = set()
            for working_directory in working_directories:
                try:
                    files_list = list_files(
                        working_directory,
                        base=working_directory,
                        skip_dirs={"node_modules", "__pycache__", "venv"},
                        skip_extensions=(".pyc", ".tmp"),
                        skip_prefix=".",
                    )
                    all_generated_files.update(files_list)
                except Exception as e:
                    logger.warning(
                        "Failed to collect generated "
                        f"files from {working_directory}: {e}"
                    )

            if all_generated_files:
                context += "Generated Files from Previous Tasks:\n"
                for file_path in sorted(all_generated_files):
                    context += f"  - {file_path}\n"
                context += "\n"

        context += "\n"

    return context


def build_context_for_workforce(
    task_lock: TaskLock,
    options: Chat,
    task_content: str | None = None,
    current_attaches: list[str] | None = None,
) -> str:
    """Build context information for workforce.
    Instructs coordinator to actively load skills using list_skills/load_skill tools.
    """
    has_current_attaches = bool(current_attaches)
    context = build_conversation_context(
        task_lock,
        header="=== CONVERSATION HISTORY ===",
        include_generated_files=not has_current_attaches,
    )
    if not has_current_attaches:
        return context

    attachment_context = "=== CURRENT ATTACHMENTS ===\n"
    attachment_context += (
        "The user's current request includes these attachments. "
        "They are the authoritative files for the current task. "
        "Do not replace them with files mentioned in conversation history "
        "or generated by previous tasks unless the user explicitly asks.\n"
    )
    for file_path in current_attaches or []:
        attachment_context += f"  - {file_path}\n"
    attachment_context += "\n"
    return attachment_context + context


@sync_step
async def step_solve(options: Chat, request: Request, task_lock: TaskLock):
    """Main task execution loop. Called when POST /chat endpoint
    is hit to start a new chat session.

    Processes task queue, manages workforce lifecycle, and streams
    responses back to the client via SSE.

    Args:
        options (Chat): Chat configuration containing task details and
            model settings.
        request (Request): FastAPI request object for client connection
            management.
        task_lock (TaskLock): Shared task state and queue for the project.

    Yields:
        SSE formatted responses for task progress, errors, and results
    """
    start_event_loop = True

    # Initialize task_lock attributes
    if not hasattr(task_lock, "conversation_history"):
        task_lock.conversation_history = []
    if not hasattr(task_lock, "last_task_result"):
        task_lock.last_task_result = ""
    if not hasattr(task_lock, "question_agent"):
        task_lock.question_agent = None
    if not hasattr(task_lock, "summary_generated"):
        task_lock.summary_generated = False

    # Create or reuse persistent question_agent
    if task_lock.question_agent is None:
        task_lock.question_agent = question_confirm_agent(options)
    else:
        hist_len = len(task_lock.conversation_history)
        logger.debug(
            f"Reusing existing question_agent with {hist_len} history entries"
        )

    question_agent = task_lock.question_agent

    # Other variables
    camel_task = None
    workforce = None
    mcp = None
    last_completed_task_result = ""  # Track the last completed task result
    summary_task_content = ""  # Track task summary
    loop_iteration = 0
    event_loop = asyncio.get_running_loop()
    sub_tasks: list[Task] = []

    logger.info("=" * 80)
    logger.info(
        "🚀 [LIFECYCLE] step_solve STARTED",
        extra={"project_id": options.project_id, "task_id": options.task_id},
    )
    logger.info("=" * 80)
    logger.debug(
        "Step solve options",
        extra={
            "task_id": options.task_id,
            "model_platform": options.model_platform,
        },
    )

    while True:
        loop_iteration += 1
        logger.debug(
            f"[LIFECYCLE] step_solve loop iteration #{loop_iteration}",
            extra={
                "project_id": options.project_id,
                "task_id": options.task_id,
            },
        )

        if await request.is_disconnected():
            logger.warning("=" * 80)
            logger.warning(
                "[LIFECYCLE] CLIENT DISCONNECTED "
                f"for project {options.project_id}"
            )
            logger.warning("=" * 80)
            if workforce is not None:
                logger.info(
                    "[LIFECYCLE] Stopping workforce "
                    "due to client disconnect, "
                    "workforce._running="
                    f"{workforce._running}"
                )
                if workforce._running:
                    workforce.stop()
                workforce.stop_gracefully()
                logger.info(
                    "[LIFECYCLE] Workforce stopped after client disconnect"
                )
            else:
                logger.info("[LIFECYCLE] Workforce is None, no need to stop")
            task_lock.status = Status.done
            try:
                await delete_task_lock(task_lock.id)
                logger.info(
                    "[LIFECYCLE] Task lock deleted after client disconnect"
                )
            except Exception as e:
                logger.error(f"Error deleting task lock on disconnect: {e}")
            logger.info(
                "[LIFECYCLE] Breaking out of "
                "step_solve loop due to "
                "client disconnect"
            )
            break
        try:
            item = await task_lock.get_queue()
        except Exception as e:
            logger.error(
                "Error getting item from queue",
                extra={
                    "project_id": options.project_id,
                    "task_id": options.task_id,
                    "error": str(e),
                },
                exc_info=True,
            )
            # Continue waiting instead of breaking on queue error
            continue

        try:
            if item.action == Action.improve or start_event_loop:
                logger.info("=" * 80)
                logger.info(
                    "[NEW-QUESTION] Action.improve "
                    "received or start_event_loop",
                    extra={
                        "project_id": options.project_id,
                        "start_event_loop": start_event_loop,
                    },
                )
                wf_state = (
                    "None"
                    if workforce is None
                    else f"exists(id={id(workforce)})"
                )
                logger.info(
                    "[NEW-QUESTION] Current workforce"
                    f" state: workforce={wf_state}"
                )
                ct_state = (
                    "None"
                    if camel_task is None
                    else f"exists(id={camel_task.id})"
                )
                logger.info(
                    "[NEW-QUESTION] Current "
                    "camel_task state: "
                    f"camel_task={ct_state}"
                )
                logger.info("=" * 80)
                # from viztracer import VizTracer

                # tracer = VizTracer()
                # tracer.start()
                if start_event_loop is True:
                    question = options.question
                    attaches_to_use = options.attaches
                    logger.info(
                        "[NEW-QUESTION] Initial question"
                        " from options.question: "
                        f"'{question[:100]}...'"
                    )
                    start_event_loop = False
                else:
                    assert isinstance(item, ActionImproveData)
                    question = item.data.question
                    attaches_to_use = item.data.attaches or []
                    logger.info(
                        "[NEW-QUESTION] Follow-up "
                        "question from "
                        "ActionImproveData: "
                        f"'{question[:100]}...'"
                    )
                if attaches_to_use:
                    logger.info(
                        "[NEW-QUESTION] Current turn attachments: "
                        f"{attaches_to_use}"
                    )

                is_exceeded, total_length = check_conversation_history_length(
                    task_lock
                )
                if is_exceeded:
                    logger.error(
                        "Conversation history too long",
                        extra={
                            "project_id": options.project_id,
                            "current_length": total_length,
                            "max_length": 100000,
                        },
                    )
                    ctx_msg = (
                        "The conversation history "
                        "is too long. Please create"
                        " a new project to continue."
                    )
                    yield sse_json(
                        "context_too_long",
                        {
                            "message": ctx_msg,
                            "current_length": total_length,
                            "max_length": 100000,
                        },
                    )
                    continue

                # Determine task complexity: attachments
                # mean workforce, otherwise let agent decide
                is_complex_task: bool
                if len(attaches_to_use) > 0:
                    is_complex_task = True
                    logger.info(
                        "[NEW-QUESTION] Has attachments"
                        ", treating as complex task"
                    )
                else:
                    is_complex_task = await question_confirm(
                        question_agent, question, task_lock
                    )
                    logger.info(
                        "[NEW-QUESTION] question_confirm"
                        " result: is_complex="
                        f"{is_complex_task}"
                    )

                if not is_complex_task:
                    logger.info(
                        "[NEW-QUESTION] Simple question"
                        ", providing direct answer "
                        "without workforce"
                    )
                    conv_ctx = build_conversation_context(
                        task_lock, header="=== 历史对话 ==="
                    )
                    language_policy = build_output_language_policy(
                        options.language
                    )
                    simple_answer_prompt = (
                        f"{conv_ctx}"
                        f"{language_policy}\n\n"
                        f"用户问题：{question}\n\n"
                        "请直接、准确、有帮助地回答这个简单问题。"
                        f"回复必须使用{get_output_language(options.language)}。"
                    )

                    try:
                        simple_resp = question_agent.step(simple_answer_prompt)
                        if simple_resp and simple_resp.msgs:
                            answer_content = simple_resp.msgs[0].content
                        else:
                            answer_content = (
                                "我理解你的问题，但现在暂时无法生成回复。"
                            )

                        task_lock.add_conversation("assistant", answer_content)

                        yield sse_json(
                            "wait_confirm",
                            {"content": answer_content, "question": question},
                        )
                    except Exception as e:
                        logger.error(f"Error generating simple answer: {e}")
                        yield sse_json(
                            "wait_confirm",
                            {
                                "content": "I encountered an error"
                                " while processing "
                                "your question.",
                                "question": question,
                            },
                        )

                    # Clean up empty folder if it was created for this task
                    if (
                        hasattr(task_lock, "new_folder_path")
                        and task_lock.new_folder_path
                    ):
                        try:
                            folder_path = Path(task_lock.new_folder_path)
                            if folder_path.exists() and folder_path.is_dir():
                                # Check if folder is empty
                                if not any(folder_path.iterdir()):
                                    folder_path.rmdir()
                                    logger.info(
                                        "Cleaned up empty"
                                        " folder: "
                                        f"{folder_path}"
                                    )
                                    # Also clean up parent
                                    # project folder if empty
                                    project_folder = folder_path.parent
                                    if project_folder.exists() and not any(
                                        project_folder.iterdir()
                                    ):
                                        project_folder.rmdir()
                                        logger.info(
                                            "Cleaned up "
                                            "empty project"
                                            " folder: "
                                            f"{project_folder}"
                                        )
                                else:
                                    logger.info(
                                        "Folder not empty"
                                        ", keeping: "
                                        f"{folder_path}"
                                    )
                            # Reset the folder path
                            task_lock.new_folder_path = None
                        except Exception as e:
                            logger.error(f"Error cleaning up folder: {e}")
                else:
                    logger.info(
                        "[NEW-QUESTION] Complex task, "
                        "creating workforce and "
                        "decomposing"
                    )
                    # Update the sync_step with new task_id
                    if hasattr(item, "new_task_id") and item.new_task_id:
                        set_current_task_id(
                            options.project_id, item.new_task_id
                        )
                        task_lock.summary_generated = False

                    yield sse_json("confirmed", {"question": question})
                    task_lock.status = Status.confirmed

                    if should_use_search_only(question, attaches_to_use):
                        activate_event = build_search_toolkit_event(
                            ActionActivateToolkitData,
                            options.task_id,
                            f"with query '{question}', web type, 1 result page",
                        )
                        yield sse_json(
                            "activate_toolkit", activate_event.data
                        )
                        try:
                            search_answer = await build_search_only_answer(
                                question_agent,
                                question,
                                options,
                                task_lock,
                            )
                        except Exception as e:
                            logger.warning(
                                "Search-only route failed, "
                                "falling back to workforce",
                                extra={
                                    "project_id": options.project_id,
                                    "task_id": options.task_id,
                                    "error": str(e),
                                },
                            )
                            search_answer = None

                        deactivate_message = (
                            "search-only completed"
                            if search_answer
                            else "search-only unavailable, falling back"
                        )
                        deactivate_event = build_search_toolkit_event(
                            ActionDeactivateToolkitData,
                            options.task_id,
                            deactivate_message,
                        )
                        yield sse_json(
                            "deactivate_toolkit", deactivate_event.data
                        )

                        if search_answer:
                            yield sse_json("end", search_answer)
                            continue

                    context_for_coordinator = build_context_for_workforce(
                        task_lock, options, current_attaches=attaches_to_use
                    )
                    context_for_coordinator = (
                        build_output_language_policy(options.language)
                        + "\n\n"
                        + context_for_coordinator
                    )

                    # Check if workforce exists - reuse
                    # it; otherwise create new one
                    if workforce is not None:
                        logger.debug(
                            "[NEW-QUESTION] Reusing "
                            "existing workforce "
                            f"(id={id(workforce)})"
                        )
                    else:
                        logger.info(
                            "[NEW-QUESTION] Creating NEW workforce instance"
                        )
                        (workforce, mcp) = await construct_workforce(options)
                        for new_agent in options.new_agents:
                            workforce.add_single_agent_worker(
                                format_agent_description(new_agent),
                                await new_agent_model(new_agent, options),
                            )
                    # Create camel_task for the question
                    clean_task_content = question + options.summary_prompt
                    camel_task = Task(
                        content=clean_task_content, id=options.task_id
                    )
                    if len(attaches_to_use) > 0:
                        camel_task.additional_info = {
                            Path(file_path).name: file_path
                            for file_path in attaches_to_use
                        }

                    # Stream decomposition in background
                    stream_state = {
                        "subtasks": [],
                        "seen_ids": set(),
                        "last_content": "",
                    }
                    state_holder: dict[str, Any] = {
                        "sub_tasks": [],
                        "summary_task": "",
                    }

                    def on_stream_batch(
                        new_tasks: list[Task], is_final: bool = False
                    ):
                        fresh_tasks = [
                            t
                            for t in new_tasks
                            if t.id not in stream_state["seen_ids"]
                        ]
                        for t in fresh_tasks:
                            stream_state["seen_ids"].add(t.id)
                        stream_state["subtasks"].extend(fresh_tasks)

                    def on_stream_text(chunk):
                        try:
                            accumulated_content = (
                                chunk.msg.content
                                if hasattr(chunk, "msg") and chunk.msg
                                else str(chunk)
                            )
                            last_content = stream_state["last_content"]

                            # Calculate delta: new content
                            # not in the previous chunk
                            if accumulated_content.startswith(last_content):
                                delta_content = accumulated_content[
                                    len(last_content) :
                                ]
                            else:
                                delta_content = accumulated_content

                            stream_state["last_content"] = accumulated_content

                            if delta_content:
                                asyncio.run_coroutine_threadsafe(
                                    task_lock.put_queue(
                                        ActionDecomposeTextData(
                                            data={
                                                "project_id": options.project_id,
                                                "task_id": options.task_id,
                                                "content": delta_content,
                                            }
                                        )
                                    ),
                                    event_loop,
                                )
                        except Exception as e:
                            logger.warning(
                                f"Failed to stream decomposition text: {e}"
                            )

                    async def run_decomposition():
                        nonlocal summary_task_content
                        try:
                            sub_tasks = await asyncio.to_thread(
                                workforce.eigent_make_sub_tasks,
                                camel_task,
                                context_for_coordinator,
                                on_stream_batch,
                                on_stream_text,
                            )

                            if stream_state["subtasks"]:
                                sub_tasks = stream_state["subtasks"]
                            state_holder["sub_tasks"] = sub_tasks
                            logger.info(
                                "Task decomposed into "
                                f"{len(sub_tasks)} subtasks"
                            )
                            try:
                                task_lock.decompose_sub_tasks = sub_tasks
                            except Exception:
                                pass

                            summary_task_content = build_fallback_task_summary(
                                camel_task, options.language
                            )
                            task_lock.summary_generated = False

                            state_holder["summary_task"] = summary_task_content
                            try:
                                task_lock.summary_task_content = (
                                    summary_task_content
                                )
                            except Exception:
                                pass

                            payload = {
                                "project_id": options.project_id,
                                "task_id": options.task_id,
                                "sub_tasks": tree_sub_tasks(
                                    camel_task.subtasks
                                ),
                                "delta_sub_tasks": tree_sub_tasks(sub_tasks),
                                "is_final": True,
                                "summary_task": summary_task_content,
                            }
                            await task_lock.put_queue(
                                ActionDecomposeProgressData(data=payload)
                            )

                            async def generate_summary_in_background():
                                nonlocal summary_task_content
                                summary_task_agent = task_summary_agent(
                                    options
                                )
                                try:
                                    generated_summary = (
                                        await asyncio.wait_for(
                                            summary_task(
                                                summary_task_agent,
                                                camel_task,
                                                options.language,
                                            ),
                                            timeout=10,
                                        )
                                    )
                                    summary_task_content = generated_summary
                                    state_holder["summary_task"] = (
                                        generated_summary
                                    )
                                    task_lock.summary_task_content = (
                                        generated_summary
                                    )
                                    task_lock.summary_generated = True
                                    await task_lock.put_queue(
                                        ActionDecomposeProgressData(
                                            data={
                                                "project_id": options.project_id,
                                                "task_id": options.task_id,
                                                "summary_task": generated_summary,
                                                "summary_only": True,
                                            }
                                        )
                                    )
                                    logger.info(
                                        "summary_task generated in background",
                                        extra={
                                            "project_id": options.project_id,
                                            "task_id": options.task_id,
                                        },
                                    )
                                except TimeoutError:
                                    task_lock.summary_generated = True
                                    logger.warning(
                                        "summary_task background timeout",
                                        extra={
                                            "project_id": options.project_id,
                                            "task_id": options.task_id,
                                        },
                                    )
                                except Exception:
                                    task_lock.summary_generated = True
                                    logger.exception(
                                        "summary_task background failed",
                                        extra={
                                            "project_id": options.project_id,
                                            "task_id": options.task_id,
                                        },
                                    )

                            summary_bg_task = asyncio.create_task(
                                generate_summary_in_background()
                            )
                            task_lock.add_background_task(summary_bg_task)
                        except Exception as e:
                            logger.error(
                                f"Error in background decomposition: {e}",
                                exc_info=True,
                            )

                    bg_task = asyncio.create_task(run_decomposition())
                    task_lock.add_background_task(bg_task)

            elif item.action == Action.update_task:
                assert camel_task is not None
                update_tasks = {item.id: item for item in item.data.task}
                # Use stored decomposition results if available
                if not sub_tasks:
                    sub_tasks = getattr(task_lock, "decompose_sub_tasks", [])
                sub_tasks = update_sub_tasks(sub_tasks, update_tasks)
                # Also update camel_task.subtasks
                # to remove deleted tasks
                # (used by to_sub_tasks)
                update_sub_tasks(camel_task.subtasks, update_tasks)
                # Add new tasks (with empty id)
                # to both camel_task and sub_tasks
                new_tasks = add_sub_tasks(camel_task, item.data.task)
                # Also add new tasks to sub_tasks so
                # workforce.eigent_start uses correct list
                sub_tasks.extend(new_tasks)
                # Save updated sub_tasks back to
                # task_lock so Action.start uses
                # the correct list
                task_lock.decompose_sub_tasks = sub_tasks
                summary_task_content_local = getattr(
                    task_lock, "summary_task_content", summary_task_content
                )
                yield to_sub_tasks(camel_task, summary_task_content_local)
            elif item.action == Action.add_task:
                # Check if this might be a misrouted second question
                if camel_task is None and workforce is None:
                    logger.error(
                        "Cannot add task: both "
                        "camel_task and workforce "
                        "are None for project "
                        f"{options.project_id}"
                    )
                    yield sse_json(
                        "error",
                        {
                            "message": "Cannot add task: task not "
                            "initialized. Please start"
                            " a task first."
                        },
                    )
                    continue

                assert camel_task is not None
                if workforce is None:
                    logger.error(
                        "Cannot add task: workforce"
                        " not initialized for "
                        "project "
                        f"{options.project_id}"
                    )
                    yield sse_json(
                        "error",
                        {
                            "message": "Workforce not initialized."
                            " Please start the task "
                            "first."
                        },
                    )
                    continue

                # Add task to the workforce queue
                workforce.add_task(
                    item.content, item.task_id, item.additional_info
                )

                returnData = {
                    "project_id": item.project_id,
                    "task_id": item.task_id or (len(camel_task.subtasks) + 1),
                }
                yield sse_json("add_task", returnData)
            elif item.action == Action.remove_task:
                if workforce is None:
                    logger.error(
                        "Cannot remove task: "
                        "workforce not initialized "
                        "for project "
                        f"{options.project_id}"
                    )
                    yield sse_json(
                        "error",
                        {
                            "message": "Workforce not initialized."
                            " Please start the task "
                            "first."
                        },
                    )
                    continue

                workforce.remove_task(item.task_id)
                returnData = {
                    "project_id": item.project_id,
                    "task_id": item.task_id,
                }
                yield sse_json("remove_task", returnData)
            elif item.action == Action.skip_task:
                logger.info("=" * 80)
                logger.info(
                    "🛑 [LIFECYCLE] SKIP_TASK action "
                    "received (User clicked "
                    "Stop button)",
                    extra={
                        "project_id": options.project_id,
                        "item_project_id": item.project_id,
                    },
                )
                logger.info("=" * 80)

                # Prevent duplicate skip processing
                if task_lock.status == Status.done:
                    logger.warning(
                        "[LIFECYCLE] SKIP_TASK "
                        "received but task already "
                        "marked as done. Ignoring."
                    )
                    continue

                wf_match = (
                    workforce is not None
                    and item.project_id == options.project_id
                )
                if wf_match:
                    logger.info(
                        "[LIFECYCLE] Workforce exists"
                        f" (id={id(workforce)}), "
                        "state="
                        f"{workforce._state.name}, "
                        f"_running={workforce._running}"
                    )

                    # Stop workforce completely
                    logger.info("[LIFECYCLE] 🛑 Stopping workforce")
                    if workforce._running:
                        # Import correct BaseWorkforce from camel
                        from camel.societies.workforce.workforce import (
                            Workforce as BaseWorkforce,
                        )

                        BaseWorkforce.stop(workforce)
                        logger.info(
                            "[LIFECYCLE] "
                            "BaseWorkforce.stop() "
                            "completed, state="
                            f"{workforce._state.name}, "
                            f"_running={workforce._running}"
                        )

                    workforce.stop_gracefully()
                    logger.info("[LIFECYCLE] ✅ Workforce stopped gracefully")

                    # Clear workforce to avoid state issues
                    # Next question will create fresh workforce
                    workforce = None
                    logger.info(
                        "[LIFECYCLE] Workforce set "
                        "to None, will be recreated"
                        " on next question"
                    )
                else:
                    logger.warning(
                        "[LIFECYCLE] Cannot skip: "
                        "workforce is None or "
                        "project_id mismatch"
                    )

                # Mark task as done and preserve context (like Action.end does)
                task_lock.status = Status.done
                end_message = (
                    "<summary>Task stopped</summary>Task stopped by user"
                )
                task_lock.last_task_result = end_message

                # Add to conversation history (like normal end does)
                if camel_task is not None:
                    task_content: str = camel_task.content
                    if "=== CURRENT TASK ===" in task_content:
                        task_content = task_content.split(
                            "=== CURRENT TASK ==="
                        )[-1].strip()
                else:
                    task_content: str = f"Task {options.task_id}"

                task_lock.add_conversation(
                    "task_result",
                    {
                        "task_content": task_content,
                        "task_result": end_message,
                        "working_directory": get_working_directory(
                            options, task_lock
                        ),
                    },
                )

                # Clear camel_task as well
                # (workforce is cleared, so
                # camel_task should be too)
                camel_task = None
                logger.info(
                    "[LIFECYCLE] Task marked as "
                    "done, workforce and "
                    "camel_task cleared, "
                    "ready for multi-turn"
                )

                # Send end event to frontend with
                # string format (matching normal
                # end event format)
                yield sse_json("end", end_message)
                logger.info("[LIFECYCLE] Sent 'end' SSE event to frontend")

                # Continue loop to accept new
                # questions (don't break, don't
                # delete task_lock)
            elif item.action == Action.start:
                # Check conversation history length before starting task
                is_exceeded, total_length = check_conversation_history_length(
                    task_lock
                )
                if is_exceeded:
                    logger.error(
                        "Cannot start task: "
                        "conversation history too "
                        f"long ({total_length} chars)"
                        " for project "
                        f"{options.project_id}"
                    )
                    ctx_msg = (
                        "The conversation history "
                        "is too long. Please create"
                        " a new project to continue."
                    )
                    yield sse_json(
                        "context_too_long",
                        {
                            "message": ctx_msg,
                            "current_length": total_length,
                            "max_length": 100000,
                        },
                    )
                    continue

                if workforce is not None:
                    if workforce._state.name == "PAUSED":
                        # Resume paused workforce -
                        # subtasks should already
                        # be loaded
                        workforce.resume()
                        continue
                else:
                    continue

                task_lock.status = Status.processing
                if not sub_tasks:
                    sub_tasks = getattr(task_lock, "decompose_sub_tasks", [])
                task = asyncio.create_task(workforce.eigent_start(sub_tasks))
                task_lock.add_background_task(task)
            elif item.action == Action.task_state:
                # Track completed task results for the end event
                task_id = item.data.get("task_id", "unknown")
                task_state = item.data.get("state", "unknown")
                task_result = item.data.get("result", "")

                if task_state == "DONE" and task_result:
                    last_completed_task_result = task_result

                yield sse_json("task_state", item.data)
            elif item.action == Action.new_task_state:
                logger.info("=" * 80)
                logger.info(
                    "[LIFECYCLE] NEW_TASK_STATE action received (Multi-turn)",
                    extra={"project_id": options.project_id},
                )
                logger.info("=" * 80)

                # Log new task state details
                new_task_id = item.data.get("task_id", "unknown")
                new_task_state = item.data.get("state", "unknown")
                new_task_result = item.data.get("result", "")
                logger.info(
                    "[LIFECYCLE] New task details"
                    f": task_id={new_task_id}, "
                    f"state={new_task_state}"
                )

                if camel_task is None:
                    logger.error(
                        "NEW_TASK_STATE action "
                        "received but camel_task "
                        "is None for project "
                        f"{options.project_id}, "
                        f"task {new_task_id}"
                    )
                    yield sse_json(
                        "error",
                        {
                            "message": "Cannot process new task "
                            "state: current task not "
                            "initialized."
                        },
                    )
                    continue

                old_task_content: str = camel_task.content
                get_result = get_task_result_with_optional_summary
                old_task_result: str = await get_result(camel_task, options)

                old_task_content_clean: str = old_task_content
                if "=== CURRENT TASK ===" in old_task_content_clean:
                    old_task_content_clean = old_task_content_clean.split(
                        "=== CURRENT TASK ==="
                    )[-1].strip()

                task_lock.add_conversation(
                    "task_result",
                    {
                        "task_content": old_task_content_clean,
                        "task_result": old_task_result,
                        "working_directory": get_working_directory(
                            options, task_lock
                        ),
                    },
                )

                new_task_content = item.data.get("content", "")

                if new_task_content:
                    import time

                    task_id = item.data.get(
                        "task_id", f"{int(time.time() * 1000)}-multi"
                    )
                    new_camel_task = Task(content=new_task_content, id=task_id)
                    if (
                        hasattr(camel_task, "additional_info")
                        and camel_task.additional_info
                    ):
                        new_camel_task.additional_info = (
                            camel_task.additional_info
                        )
                    camel_task = new_camel_task

                # Now trigger end of previous task using stored result
                yield sse_json("end", old_task_result)

                # Always yield new_task_state first - this is not optional
                yield sse_json("new_task_state", item.data)
                # Trigger Queue Removal
                yield sse_json(
                    "remove_task", {"task_id": item.data.get("task_id")}
                )

                # Then handle multi-turn processing
                if workforce is not None and new_task_content:
                    logger.info(
                        "[LIFECYCLE] Multi-turn: "
                        "workforce exists "
                        f"(id={id(workforce)}), "
                        "pausing for question "
                        "confirmation"
                    )
                    task_lock.status = Status.confirming
                    workforce.pause()
                    logger.info(
                        "[LIFECYCLE] Multi-turn: "
                        "workforce paused, state="
                        f"{workforce._state.name}"
                    )

                    try:
                        logger.info(
                            "[LIFECYCLE] Multi-turn: "
                            "calling question_confirm "
                            "for new task"
                        )
                        is_multi_turn_complex = await question_confirm(
                            question_agent, new_task_content, task_lock
                        )
                        logger.info(
                            "[LIFECYCLE] Multi-turn: "
                            "question_confirm result:"
                            " is_complex="
                            f"{is_multi_turn_complex}"
                        )

                        if not is_multi_turn_complex:
                            logger.info(
                                "[LIFECYCLE] Multi-turn: "
                                "task is simple, providing"
                                " direct answer without "
                                "workforce"
                            )
                            conv_ctx = build_conversation_context(
                                task_lock,
                                header="=== 历史对话 ===",
                            )
                            language_policy = build_output_language_policy(
                                options.language
                            )
                            simple_answer_prompt = (
                                f"{conv_ctx}"
                                f"{language_policy}\n\n"
                                "用户问题："
                                f"{new_task_content}"
                                "\n\n请直接、准确、有帮助地回答这个简单问题。"
                                "回复必须使用"
                                f"{get_output_language(options.language)}。"
                            )

                            try:
                                simple_resp = question_agent.step(
                                    simple_answer_prompt
                                )
                                if simple_resp and simple_resp.msgs:
                                    answer_content = simple_resp.msgs[
                                        0
                                    ].content
                                else:
                                    answer_content = (
                                        "我理解你的问题，但现在暂时无法生成回复。"
                                    )

                                task_lock.add_conversation(
                                    "assistant", answer_content
                                )

                                # Send response to user
                                # (don't send confirmed
                                # if simple response)
                                yield sse_json(
                                    "wait_confirm",
                                    {
                                        "content": answer_content,
                                        "question": new_task_content,
                                    },
                                )
                            except Exception as e:
                                logger.error(
                                    "Error generating simple "
                                    f"answer in multi-turn: {e}"
                                )
                                yield sse_json(
                                    "wait_confirm",
                                    {
                                        "content": "I encountered an error "
                                        "while processing your "
                                        "question.",
                                        "question": new_task_content,
                                    },
                                )

                            logger.info(
                                "[LIFECYCLE] Multi-turn: "
                                "simple answer provided, "
                                "resuming workforce"
                            )
                            workforce.resume()
                            logger.info(
                                "[LIFECYCLE] Multi-turn: "
                                "workforce resumed, "
                                "continuing to next "
                                "iteration"
                            )
                            # Continue the main while loop,
                            # waiting for next action
                            continue

                        # Update the sync_step with new
                        # task_id before sending new
                        # task sse events
                        logger.info(
                            "[LIFECYCLE] Multi-turn: "
                            "task is complex, setting "
                            f"new task_id={task_id}"
                        )
                        set_current_task_id(options.project_id, task_id)

                        yield sse_json(
                            "confirmed", {"question": new_task_content}
                        )
                        task_lock.status = Status.confirmed

                        logger.info(
                            "[LIFECYCLE] Multi-turn: "
                            "building context for "
                            "workforce"
                        )
                        context_for_multi_turn = build_context_for_workforce(
                            task_lock, options
                        )
                        context_for_multi_turn = (
                            build_output_language_policy(options.language)
                            + "\n\n"
                            + context_for_multi_turn
                        )

                        stream_state = {
                            "subtasks": [],
                            "seen_ids": set(),
                            "last_content": "",
                        }

                        def on_stream_batch(
                            new_tasks: list[Task], is_final: bool = False
                        ):
                            fresh_tasks = [
                                t
                                for t in new_tasks
                                if t.id not in stream_state["seen_ids"]
                            ]
                            for t in fresh_tasks:
                                stream_state["seen_ids"].add(t.id)
                            stream_state["subtasks"].extend(fresh_tasks)

                        def on_stream_text(chunk):
                            try:
                                has_msg = hasattr(chunk, "msg") and chunk.msg
                                accumulated_content = (
                                    chunk.msg.content
                                    if has_msg
                                    else str(chunk)
                                )
                                last_content = stream_state["last_content"]

                                if accumulated_content.startswith(
                                    last_content
                                ):
                                    delta_content = accumulated_content[
                                        len(last_content) :
                                    ]
                                else:
                                    delta_content = accumulated_content

                                stream_state["last_content"] = (
                                    accumulated_content
                                )

                                if delta_content:
                                    asyncio.run_coroutine_threadsafe(
                                        task_lock.put_queue(
                                            ActionDecomposeTextData(
                                                data={
                                                    "project_id": options.project_id,
                                                    "task_id": options.task_id,
                                                    "content": delta_content,
                                                }
                                            )
                                        ),
                                        event_loop,
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to stream decomposition text: {e}"
                                )

                        wf = workforce
                        new_sub_tasks = await wf.handle_decompose_append_task(
                            camel_task,
                            reset=False,
                            coordinator_context=context_for_multi_turn,
                            on_stream_batch=on_stream_batch,
                            on_stream_text=on_stream_text,
                        )
                        if stream_state["subtasks"]:
                            new_sub_tasks = stream_state["subtasks"]
                        n = len(new_sub_tasks)
                        logger.info(
                            "[LIFECYCLE] Multi-turn: "
                            "task decomposed into "
                            f"{n} subtasks"
                        )

                        new_summary_content = build_fallback_task_summary(
                            camel_task,
                            options.language,
                            follow_up=True,
                        )

                        async def generate_multi_turn_summary_background():
                            try:
                                multi_turn_summary_agent = task_summary_agent(
                                    options
                                )
                                generated_summary = await asyncio.wait_for(
                                    summary_task(
                                        multi_turn_summary_agent,
                                        camel_task,
                                        options.language,
                                    ),
                                    timeout=10,
                                )
                                task_lock.summary_task_content = (
                                    generated_summary
                                )
                                await task_lock.put_queue(
                                    ActionDecomposeProgressData(
                                        data={
                                            "project_id": options.project_id,
                                            "task_id": task_id,
                                            "summary_task": generated_summary,
                                            "summary_only": True,
                                        }
                                    )
                                )
                                logger.info(
                                    "Generated LLM summary for multi-turn task",
                                    extra={"project_id": options.project_id},
                                )
                            except TimeoutError:
                                logger.warning(
                                    "Multi-turn summary_task timeout",
                                    extra={
                                        "project_id": options.project_id,
                                        "task_id": task_id,
                                    },
                                )
                            except Exception as e:
                                logger.error(
                                    "Error generating multi-turn "
                                    f"task summary: {e}"
                                )

                        multi_turn_summary_bg_task = asyncio.create_task(
                            generate_multi_turn_summary_background()
                        )
                        task_lock.add_background_task(
                            multi_turn_summary_bg_task
                        )

                        # Emit final subtasks once when
                        # decomposition is complete
                        final_payload = {
                            "project_id": options.project_id,
                            "task_id": options.task_id,
                            "sub_tasks": tree_sub_tasks(camel_task.subtasks),
                            "delta_sub_tasks": tree_sub_tasks(new_sub_tasks),
                            "is_final": True,
                            "summary_task": new_summary_content,
                        }
                        await task_lock.put_queue(
                            ActionDecomposeProgressData(data=final_payload)
                        )

                        # Update the context with new task data
                        sub_tasks = new_sub_tasks
                        summary_task_content = new_summary_content

                    except Exception as e:
                        import traceback

                        logger.error(
                            f"[TRACE] Traceback: {traceback.format_exc()}"
                        )
                        # Continue with existing context if decomposition fails
                        yield sse_json(
                            "error",
                            {"message": f"Failed to process task: {str(e)}"},
                        )
                else:
                    if workforce is None:
                        logger.warning(
                            "[TRACE] Workforce is None "
                            "- this might be the issue"
                        )
                    if not new_task_content:
                        logger.warning("[TRACE] No new task content provided")
            elif item.action == Action.create_agent:
                yield sse_json("create_agent", item.data)
            elif item.action == Action.activate_agent:
                yield sse_json("activate_agent", item.data)
            elif item.action == Action.deactivate_agent:
                yield sse_json("deactivate_agent", dict(item.data))
            elif item.action == Action.assign_task:
                yield sse_json("assign_task", item.data)
            elif item.action == Action.activate_toolkit:
                yield sse_json("activate_toolkit", item.data)
            elif item.action == Action.deactivate_toolkit:
                yield sse_json("deactivate_toolkit", item.data)
            elif item.action == Action.write_file:
                yield sse_json(
                    "write_file",
                    {
                        "file_path": item.data,
                        "process_task_id": item.process_task_id,
                    },
                )
            elif item.action == Action.ask:
                yield sse_json("ask", item.data)
            elif item.action == Action.notice:
                yield sse_json(
                    "notice",
                    {
                        "notice": item.data,
                        "process_task_id": item.process_task_id,
                    },
                )
            elif item.action == Action.search_mcp:
                yield sse_json("search_mcp", item.data)
            elif item.action == Action.install_mcp:
                if mcp is None:
                    logger.error(
                        "Cannot install MCP: mcp "
                        "agent not initialized for "
                        "project "
                        f"{options.project_id}"
                    )
                    yield sse_json(
                        "error",
                        {
                            "message": "MCP agent not initialized."
                            " Please start a complex "
                            "task first."
                        },
                    )
                    continue
                task = asyncio.create_task(install_mcp(mcp, item))
                task_lock.add_background_task(task)
            elif item.action == Action.terminal:
                yield sse_json(
                    "terminal",
                    {
                        "output": item.data,
                        "process_task_id": item.process_task_id,
                    },
                )
            elif item.action == Action.pause:
                if workforce is not None:
                    workforce.pause()
                    logger.info(
                        f"Workforce paused for project {options.project_id}"
                    )
                else:
                    logger.warning(
                        "Cannot pause: workforce is "
                        "None for project "
                        f"{options.project_id}"
                    )
            elif item.action == Action.resume:
                if workforce is not None:
                    workforce.resume()
                    logger.info(
                        f"Workforce resumed for project {options.project_id}"
                    )
                else:
                    logger.warning(
                        "Cannot resume: workforce "
                        "is None for project "
                        f"{options.project_id}"
                    )
            elif item.action == Action.decompose_text:
                yield sse_json("decompose_text", item.data)
            elif item.action == Action.decompose_progress:
                if item.data.get("summary_only"):
                    yield sse_json("summary_task", item.data)
                else:
                    yield sse_json("to_sub_tasks", item.data)
            elif item.action == Action.new_agent:
                if workforce is not None:
                    workforce.pause()
                    workforce.add_single_agent_worker(
                        format_agent_description(item),
                        await new_agent_model(item, options),
                    )
                    workforce.resume()
            elif item.action == Action.timeout:
                logger.info("=" * 80)
                logger.info(
                    "[LIFECYCLE] TIMEOUT action "
                    "received for project "
                    f"{options.project_id}, "
                    f"task {options.task_id}"
                )
                logger.info(f"[LIFECYCLE] Timeout data: {item.data}")
                logger.info("=" * 80)

                # Send timeout error to frontend
                timeout_message = item.data.get(
                    "message", "Task execution timeout"
                )
                in_flight = item.data.get("in_flight_tasks", 0)
                pending = item.data.get("pending_tasks", 0)
                timeout_seconds = item.data.get("timeout_seconds", 0)

                yield sse_json(
                    "error",
                    {
                        "message": timeout_message,
                        "type": "timeout",
                        "details": {
                            "in_flight_tasks": in_flight,
                            "pending_tasks": pending,
                            "timeout_seconds": timeout_seconds,
                        },
                    },
                )

            elif item.action == Action.end:
                logger.info("=" * 80)
                logger.info(
                    "[LIFECYCLE] END action "
                    "received for project "
                    f"{options.project_id}, "
                    f"task {options.task_id}"
                )
                logger.info(
                    "[LIFECYCLE] camel_task "
                    f"exists: {camel_task is not None}"
                    ", current status: "
                    f"{task_lock.status}, workforce"
                    f" exists: {workforce is not None}"
                )
                if workforce is not None:
                    logger.info(
                        "[LIFECYCLE] Workforce state"
                        " at END: _state="
                        f"{workforce._state.name}"
                        ", _running="
                        f"{workforce._running}"
                    )
                logger.info("=" * 80)

                # Prevent duplicate end processing
                if task_lock.status == Status.done:
                    logger.warning(
                        "[LIFECYCLE] END action "
                        "received but task already "
                        "marked as done. Ignoring "
                        "duplicate END action."
                    )
                    continue

                if camel_task is None:
                    logger.warning(
                        "END action received but "
                        "camel_task is None for "
                        "project "
                        f"{options.project_id}, "
                        f"task {options.task_id}. "
                        "This may indicate multiple "
                        "END actions or improper "
                        "task lifecycle management."
                    )
                    # Use item data as final result
                    # if camel_task is None
                    final_result: str = (
                        str(item.data) if item.data else "Task completed"
                    )
                else:
                    get_result = get_task_result_with_optional_summary
                    final_result: str = await get_result(camel_task, options)

                task_lock.status = Status.done

                task_lock.last_task_result = final_result

                # Handle task content - use fallback if camel_task is None
                if camel_task is not None:
                    task_content: str = camel_task.content
                    if "=== CURRENT TASK ===" in task_content:
                        task_content = task_content.split(
                            "=== CURRENT TASK ==="
                        )[-1].strip()
                else:
                    task_content: str = f"Task {options.task_id}"

                task_lock.add_conversation(
                    "task_result",
                    {
                        "task_content": task_content,
                        "task_result": final_result,
                        "working_directory": get_working_directory(
                            options, task_lock
                        ),
                    },
                )

                yield sse_json("end", final_result)

                if workforce is not None:
                    logger.info(
                        "[LIFECYCLE] Calling "
                        "workforce.stop_gracefully()"
                        " for project "
                        f"{options.project_id}, "
                        f"workforce id={id(workforce)}"
                    )
                    workforce.stop_gracefully()
                    logger.info(
                        "[LIFECYCLE] Workforce "
                        "stopped gracefully for "
                        "project "
                        f"{options.project_id}"
                    )
                    workforce = None
                    logger.info("[LIFECYCLE] Workforce set to None")
                else:
                    logger.warning(
                        "[LIFECYCLE] Workforce "
                        "already None at end "
                        "action for project "
                        f"{options.project_id}"
                    )

                camel_task = None
                logger.info("[LIFECYCLE] camel_task set to None")

                if question_agent is not None:
                    question_agent.reset()
                    logger.info(
                        "[LIFECYCLE] question_agent"
                        " reset for project "
                        f"{options.project_id}"
                    )
            elif item.action == Action.supplement:
                # Check if this might be a misrouted second question
                if camel_task is None:
                    logger.warning(
                        "SUPPLEMENT action received "
                        "but camel_task is None for "
                        f"project {options.project_id}"
                    )
                    yield sse_json(
                        "error",
                        {
                            "message": "Cannot supplement task: "
                            "task not initialized. "
                            "Please start a task "
                            "first."
                        },
                    )
                    continue
                else:
                    task_lock.status = Status.processing
                    camel_task.add_subtask(
                        Task(
                            content=item.data.question,
                            id=f"{camel_task.id}.{len(camel_task.subtasks)}",
                        )
                    )
                    if workforce is not None:
                        task = asyncio.create_task(
                            workforce.eigent_start(camel_task.subtasks)
                        )
                        task_lock.add_background_task(task)
            elif item.action == Action.budget_not_enough:
                if workforce is not None:
                    workforce.pause()
                yield sse_json(
                    Action.budget_not_enough, {"message": "budget not enouth"}
                )
            elif item.action == Action.stop:
                logger.info("=" * 80)
                logger.info(
                    "[LIFECYCLE] STOP action received"
                    " for project "
                    f"{options.project_id}"
                )
                logger.info("=" * 80)
                if workforce is not None:
                    logger.info(
                        "[LIFECYCLE] Workforce exists "
                        f"(id={id(workforce)}), "
                        f"_running={workforce._running}"
                        ", _state="
                        f"{workforce._state.name}"
                    )
                    if workforce._running:
                        logger.info(
                            "[LIFECYCLE] Calling "
                            "workforce.stop() because"
                            " _running=True"
                        )
                        workforce.stop()
                        logger.info("[LIFECYCLE] workforce.stop() completed")
                    logger.info(
                        "[LIFECYCLE] Calling workforce.stop_gracefully()"
                    )
                    workforce.stop_gracefully()
                    logger.info(
                        "[LIFECYCLE] Workforce stopped"
                        " for project "
                        f"{options.project_id}"
                    )
                else:
                    logger.warning(
                        "[LIFECYCLE] Workforce is None"
                        " at stop action for project"
                        f" {options.project_id}"
                    )
                logger.info("[LIFECYCLE] Deleting task lock")
                await delete_task_lock(task_lock.id)
                logger.info(
                    "[LIFECYCLE] Task lock deleted, breaking out of loop"
                )
                break
            else:
                logger.warning(f"Unknown action: {item.action}")
        except ModelProcessingError as e:
            if "Budget has been exceeded" in str(e):
                logger.warning(
                    "Budget exceeded for task "
                    f"{options.task_id}, action: "
                    f"{item.action}"
                )
                # workforce decompose task don't use
                # ListenAgent, this need return sse
                if "workforce" in locals() and workforce is not None:
                    workforce.pause()
                yield sse_json(
                    Action.budget_not_enough, {"message": "budget not enouth"}
                )
            else:
                logger.error(
                    "ModelProcessingError for task "
                    f"{options.task_id}, action "
                    f"{item.action}: {e}",
                    exc_info=True,
                )
                yield sse_json("error", {"message": str(e)})
                if (
                    "workforce" in locals()
                    and workforce is not None
                    and workforce._running
                ):
                    workforce.stop()
        except Exception as e:
            logger.error(
                "Unhandled exception for task "
                f"{options.task_id}, action "
                f"{item.action}: {e}",
                exc_info=True,
            )
            yield sse_json("error", {"message": str(e)})
            # Continue processing other items instead of breaking


async def install_mcp(
    mcp: ListenChatAgent,
    install_mcp: ActionInstallMcpData,
):
    mcp_keys = list(install_mcp.data.get("mcpServers", {}).keys())
    logger.info(f"Installing MCP tools: {mcp_keys}")
    try:
        mcp.add_tools(await get_mcp_tools(install_mcp.data))
        logger.info("MCP tools installed successfully")
    except Exception as e:
        logger.error(f"Error installing MCP tools: {e}", exc_info=True)
        raise


def to_sub_tasks(task: Task, summary_task_content: str):
    logger.info("[TO-SUB-TASKS] 📋 Creating to_sub_tasks SSE event")
    logger.info(
        f"[TO-SUB-TASKS] task.id={task.id}"
        f", summary={summary_task_content[:50]}"
        f"..., subtasks_count="
        f"{len(task.subtasks)}"
    )
    result = sse_json(
        "to_sub_tasks",
        {
            "summary_task": summary_task_content,
            "sub_tasks": tree_sub_tasks(task.subtasks),
        },
    )
    logger.info("[TO-SUB-TASKS] ✅ to_sub_tasks SSE event created")
    return result


def build_fallback_task_summary(
    task: Task, language: str | None = None, follow_up: bool = False
) -> str:
    content_preview = task.content if hasattr(task, "content") else ""
    if content_preview is None:
        content_preview = ""
    content_preview = str(content_preview).strip()
    if len(content_preview) > 80:
        content_preview = content_preview[:80] + "..."
    task_label = get_task_label(language, follow_up=follow_up)
    return f"{task_label}|{content_preview}"


def tree_sub_tasks(sub_tasks: list[Task], depth: int = 0):
    if depth > 5:
        return []

    result = (
        chain(sub_tasks)
        .filter(lambda x: x.content != "")
        .map(
            lambda x: {
                "id": x.id,
                "content": x.content,
                "state": x.state,
                "subtasks": tree_sub_tasks(x.subtasks, depth + 1),
            }
        )
        .value()
    )

    return result


def update_sub_tasks(
    sub_tasks: list[Task], update_tasks: dict[str, TaskContent], depth: int = 0
):
    if depth > 5:  # limit the depth of the recursion
        return []

    i = 0
    while i < len(sub_tasks):
        item = sub_tasks[i]
        if item.id in update_tasks:
            item.content = update_tasks[item.id].content
            update_sub_tasks(item.subtasks, update_tasks, depth + 1)
            i += 1
        else:
            sub_tasks.pop(i)
    return sub_tasks


def add_sub_tasks(
    camel_task: Task, update_tasks: list[TaskContent]
) -> list[Task]:
    """Add new tasks (with empty id) to camel_task
    and return the list of added tasks."""
    added_tasks = []
    for item in update_tasks:
        if item.id == "":
            new_task = Task(
                content=item.content,
                id=f"{camel_task.id}.{len(camel_task.subtasks) + 1}",
            )
            camel_task.add_subtask(new_task)
            added_tasks.append(new_task)
    return added_tasks


async def question_confirm(
    agent: ListenChatAgent, prompt: str, task_lock: TaskLock | None = None
) -> bool:
    """Return True for complex tasks, False for simple questions."""

    context_prompt = ""
    if task_lock:
        context_prompt = build_conversation_context(
            task_lock, header="=== 历史对话 ==="
        )

    full_prompt = f"""{context_prompt}用户问题：{prompt}

请判断这个用户问题是复杂任务还是简单问题。

**复杂任务**（回答 "yes"）：需要使用工具、执行代码、操作文件、\
多步骤规划，或创建/修改内容。
- 示例："创建文件"、"搜索 X"、"实现功能 Y"、"写代码"、"分析数据"

**简单问题**（回答 "no"）：可以直接根据知识或历史对话回答，\
不需要执行额外动作。
- 示例：问候、事实查询、澄清问题、状态确认

只能回答 "yes" 或 "no"，不要提供解释。

这是复杂任务吗？(yes/no):"""

    try:
        resp = agent.step(full_prompt)

        if not resp or not resp.msgs or len(resp.msgs) == 0:
            logger.warning(
                "No response from agent, defaulting to complex task"
            )
            return True

        content = resp.msgs[0].content
        if not content:
            logger.warning(
                "Empty content from agent, defaulting to complex task"
            )
            return True

        normalized = content.strip().lower()
        is_complex = "yes" in normalized

        result_str = "complex task" if is_complex else "simple question"
        logger.info(
            f"Question confirm result: {result_str}",
            extra={"response": content, "is_complex": is_complex},
        )

        return is_complex

    except Exception as e:
        logger.error(f"Error in question_confirm: {e}")
        raise


async def summary_task(
    agent: ListenChatAgent, task: Task, language: str | None = None
) -> str:
    output_language = get_output_language(language)
    language_policy = build_output_language_policy(language)
    prompt = f"""{language_policy}

用户的任务如下：
---
{task.to_string()}
---
你的要求：
1. 为这个任务生成一个简短、描述清晰的任务名，必须使用{output_language}。
2. 用{output_language}概括任务的主要内容和目标。
3. 返回任务名和摘要，中间用竖线（|）分隔。

示例格式："任务名称|这是任务摘要。"
不要包含任何其他文本或格式。
"""
    logger.debug("Generating task summary", extra={"task_id": task.id})
    try:
        res = agent.step(prompt)
        summary = res.msgs[0].content
        logger.info("Task summary generated", extra={"summary": summary})
        return summary
    except Exception as e:
        logger.error(
            "Error generating task summary",
            extra={"error": str(e)},
            exc_info=True,
        )
        raise


async def summary_subtasks_result(
    agent: ListenChatAgent, task: Task, language: str | None = None
) -> str:
    """
    Summarize the aggregated results from all subtasks into a concise summary.

    Args:
        agent: The summary agent to use
        task: The main task containing subtasks and their aggregated results

    Returns:
        A concise summary of all subtask results
    """
    subtasks_info = ""
    for i, subtask in enumerate(task.subtasks, 1):
        subtasks_info += f"\n**子任务 {i}**\n"
        subtasks_info += f"描述：{subtask.content}\n"
        subtasks_info += f"结果：{subtask.result or '无结果'}\n"
        subtasks_info += "---\n"

    output_language = get_output_language(language)
    language_policy = build_output_language_policy(language)
    prompt = f"""{language_policy}

你是专业的任务总结助手。请用{output_language}总结以下子任务结果。

主任务：{task.content}

子任务（包含描述和结果）：
---
{subtasks_info}
---

要求：
1. 简洁总结已经完成的内容
2. 突出每个子任务的关键发现或产出
3. 提及创建的重要文件或执行的重要操作
4. 使用项目符号或小节提升可读性
5. 不要重复任务名称，直接进入结果总结
6. 保持专业、自然、易读
7. 如果子任务结果包含英文工具/API 输出，先翻译成{output_language}再总结

总结：
"""

    res = agent.step(prompt)
    summary = res.msgs[0].content

    logger.info(
        "Generated subtasks summary for "
        f"task {task.id} with "
        f"{len(task.subtasks)} subtasks"
    )

    return summary


async def get_task_result_with_optional_summary(
    task: Task, options: Chat
) -> str:
    """
    Get the task result, with LLM summary if there are multiple subtasks.

    Args:
        task: The task to get result from
        options: Chat options for creating summary agent

    Returns:
        The task result (summarized if multiple subtasks, raw otherwise)
    """
    result = str(task.result or "")

    if task.subtasks and len(task.subtasks) > 1:
        logger.info(
            f"Task {task.id} has "
            f"{len(task.subtasks)} subtasks, "
            "generating summary"
        )
        try:
            summary_agent = task_summary_agent(options)
            summarized_result = await summary_subtasks_result(
                summary_agent, task, options.language
            )
            result = summarized_result
            logger.info(f"Successfully generated summary for task {task.id}")
        except Exception as e:
            logger.error(f"Failed to generate summary for task {task.id}: {e}")
    elif task.subtasks and len(task.subtasks) == 1:
        logger.info(f"Task {task.id} has only 1 subtask, skipping LLM summary")
        if result and "--- Subtask" in result and "Result ---" in result:
            parts = result.split("Result ---", 1)
            if len(parts) > 1:
                result = parts[1].strip()

    return result


async def construct_workforce(
    options: Chat,
) -> tuple[Workforce, ListenChatAgent]:
    """Construct a workforce with all required agents.

    This function creates all agents in PARALLEL to minimize startup time.
    Sync functions are run in thread pool, async functions
    are awaited concurrently.
    """
    logger.debug(
        "construct_workforce started",
        extra={"project_id": options.project_id, "task_id": options.task_id},
    )

    # Store main event loop reference for thread-safe async task scheduling
    # This allows agent_model() to schedule tasks
    # when called from worker threads
    set_main_event_loop(asyncio.get_running_loop())

    working_directory = get_working_directory(options)
    remote_sub_agent_planning_notice = (
        build_remote_sub_agent_planning_notice()
        if remote_sub_agent_enabled(options, working_directory)
        else ""
    )

    # ========================================================================
    # Define agent creation functions
    # ========================================================================

    def _create_coordinator_and_task_agents() -> list[ListenChatAgent]:
        """Create coordinator and task agents (sync, runs in thread pool)."""
        return [
            agent_model(
                key,
                prompt,
                options,
                [],
            )
            for key, prompt in {
                Agents.coordinator_agent: f"""
You are a helpful coordinator.
- You are now working in system {platform.system()} with architecture
{platform.machine()} at working directory \
`{working_directory}`. All local file operations \
must occur here, but you can access files from any \
place in the file system. For all file system \
operations, you MUST use absolute paths to ensure \
precision and avoid ambiguity.
The current date is {datetime.date.today()}. \
For any date-related tasks, you MUST use this as \
the current date.
{remote_sub_agent_planning_notice}
            """,
                Agents.task_agent: f"""
You are a helpful task planner.
- You are now working in system {platform.system()} with architecture
{platform.machine()} at working directory \
`{working_directory}`. All local file operations \
must occur here, but you can access files from any \
place in the file system. For all file system \
operations, you MUST use absolute paths to ensure \
precision and avoid ambiguity.
The current date is {datetime.date.today()}. \
For any date-related tasks, you MUST use this as \
the current date.
{remote_sub_agent_planning_notice}
        """,
            }.items()
        ]

    def _create_new_worker_agent() -> ListenChatAgent:
        """Create new worker agent (sync, runs in thread pool)."""
        message_integration = ToolkitMessageIntegration(
            message_handler=HumanToolkit(
                options.project_id, Agents.new_worker_agent
            ).send_message_to_user
        )
        note_toolkit = NoteTakingToolkit(
            options.project_id,
            agent_name=Agents.new_worker_agent,
            working_directory=working_directory,
        )
        note_toolkit = message_integration.register_toolkits(note_toolkit)
        skill_toolkit = SkillToolkit(
            options.project_id,
            Agents.new_worker_agent,
            working_directory=working_directory,
            user_id=options.skill_config_user_id(),
        )
        tools = [
            *HumanToolkit.get_can_use_tools(
                options.project_id, Agents.new_worker_agent
            ),
            *note_toolkit.get_tools(),
            *skill_toolkit.get_tools(),
        ]
        tool_names = [
            HumanToolkit.toolkit_name(),
            NoteTakingToolkit.toolkit_name(),
            SkillToolkit.toolkit_name(),
        ]
        system_message = f"""
        You are a helpful assistant.
- You are now working in system {platform.system()} with architecture
{platform.machine()} at working directory \
`{working_directory}`. All local file operations \
must occur here, but you can access files from any \
place in the file system. For all file system \
operations, you MUST use absolute paths to ensure \
precision and avoid ambiguity.
The current date is {datetime.date.today()}. \
For any date-related tasks, you MUST use this as \
the current date.
        """
        system_message = attach_remote_sub_agent_if_enabled(
            options=options,
            agent_name=Agents.new_worker_agent,
            working_directory=working_directory,
            tools=tools,
            tool_names=tool_names,
            system_message=system_message,
            local_tool_description="local note, terminal, or skill tools",
            message_integration=message_integration,
        )
        return agent_model(
            Agents.new_worker_agent,
            system_message,
            options,
            tools,
            tool_names=tool_names,
        )

    # ========================================================================
    # Execute all agent creations in PARALLEL
    # ========================================================================

    try:
        # asyncio.gather runs all coroutines concurrently
        # asyncio.to_thread runs sync functions in
        # thread pool without blocking event loop
        results = await asyncio.gather(
            asyncio.to_thread(_create_coordinator_and_task_agents),
            asyncio.to_thread(_create_new_worker_agent),
            asyncio.to_thread(browser_agent, options),
            developer_agent(options),
            document_agent(options),
            asyncio.to_thread(multi_modal_agent, options),
            mcp_agent(options),
        )
    except Exception as e:
        logger.error(
            f"Failed to create agents in parallel: {e}", exc_info=True
        )
        raise
    finally:
        # Always clear event loop reference after
        # parallel agent creation completes.
        # This prevents stale references and
        # potential cross-request interference
        set_main_event_loop(None)

    # Unpack results
    (
        coord_task_agents,
        new_worker_agent,
        searcher,
        developer,
        documenter,
        multi_modaler,
        mcp,
    ) = results

    coordinator_agent, task_agent = coord_task_agents

    # ========================================================================
    # Create Workforce instance and add workers (must be sequential)
    # ========================================================================

    try:
        model_platform_enum = ModelPlatformType(options.model_platform.lower())
    except (ValueError, AttributeError):
        model_platform_enum = None

    # Create workforce metrics callback for workforce analytics
    workforce_metrics = WorkforceMetricsCallback(
        project_id=options.project_id, task_id=options.task_id
    )

    workforce = Workforce(
        options.project_id,
        "A workforce",
        graceful_shutdown_timeout=3,
        share_memory=False,
        coordinator_agent=coordinator_agent,
        task_agent=task_agent,
        new_worker_agent=new_worker_agent,
        use_structured_output_handler=False
        if model_platform_enum == ModelPlatformType.OPENAI
        else True,
    )

    # Register workforce metrics callback
    workforce._callbacks.append(workforce_metrics)
    workforce.add_single_agent_worker(
        "Developer Agent: A master-level coding assistant with a powerful "
        "terminal. It can write and execute code, manage files, automate "
        "desktop tasks, and deploy web applications to solve complex "
        "technical challenges.",
        developer,
    )
    workforce.add_single_agent_worker(
        "Browser Agent: Can search the web, extract webpage content, "
        "simulate browser actions, and provide relevant information to "
        "solve the given task.",
        searcher,
    )
    workforce.add_single_agent_worker(
        "Document Agent: A document processing assistant skilled in creating "
        "and modifying a wide range of file formats. It can generate "
        "text-based files/reports (Markdown, JSON, YAML, HTML), "
        "office documents (Word, PDF), presentations (PowerPoint), and "
        "data files (Excel, CSV).",
        documenter,
    )
    workforce.add_single_agent_worker(
        "Multi-Modal Agent: A specialist in media processing. It can "
        "analyze images and audio, transcribe speech, download videos, and "
        "generate new images from text prompts.",
        multi_modaler,
    )

    return workforce, mcp


def format_agent_description(agent_data: NewAgent | ActionNewAgent) -> str:
    r"""Format a comprehensive agent description including name, tools, and
    description.
    """
    description_parts = [f"{agent_data.name}:"]

    # Add description if available
    if hasattr(agent_data, "description") and agent_data.description:
        description_parts.append(agent_data.description.strip())
    else:
        description_parts.append("A specialized agent")

    # Add tools information
    tool_names = []
    if hasattr(agent_data, "tools") and agent_data.tools:
        for tool in agent_data.tools:
            tool_names.append(titleize(tool))

    if hasattr(agent_data, "mcp_tools") and agent_data.mcp_tools:
        for mcp_server in agent_data.mcp_tools.get("mcpServers", {}).keys():
            tool_names.append(titleize(mcp_server))

    if tool_names:
        description_parts.append(
            f"with access to {', '.join(tool_names)} tools : <{tool_names}>"
        )

    return " ".join(description_parts)


async def new_agent_model(data: NewAgent | ActionNewAgent, options: Chat):
    logger.info(
        "Creating new agent",
        extra={
            "agent_name": data.name,
            "project_id": options.project_id,
            "task_id": options.task_id,
        },
    )
    logger.debug(
        "New agent data", extra={"agent_data": data.model_dump_json()}
    )
    working_directory = get_working_directory(options)
    tool_names = []
    requested_tools = [
        item for item in data.tools if item != "remote_sub_agent_toolkit"
    ]
    tools = [
        *await get_toolkits(requested_tools, data.name, options.project_id)
    ]
    for item in requested_tools:
        tool_names.append(titleize(item))
    # Always include terminal_toolkit with proper working directory
    terminal_toolkit = TerminalToolkit(
        options.project_id,
        agent_name=data.name,
        working_directory=working_directory,
        safe_mode=True,
        clone_current_env=True,
    )
    tools.extend(terminal_toolkit.get_tools())
    tool_names.append(titleize("terminal_toolkit"))
    if data.mcp_tools is not None:
        tools = [*tools, *await get_mcp_tools(data.mcp_tools)]
        for item in data.mcp_tools["mcpServers"].keys():
            tool_names.append(titleize(item))
    for item in tools:
        logger.debug(f"Agent {data.name} tool: {item.func.__name__}")
    logger.info(
        f"Agent {data.name} created with {len(tools)} tools: {tool_names}"
    )
    # Enhanced system message with platform information
    enhanced_description = f"""{data.description}
- You are now working in system {platform.system()} with architecture
{platform.machine()} at working directory \
`{working_directory}`. All local file operations \
must occur here, but you can access files from any \
place in the file system. For all file system \
operations, you MUST use absolute paths to ensure \
precision and avoid ambiguity.
The current date is {datetime.date.today()}. \
For any date-related tasks, you MUST use this as \
the current date.
"""
    message_integration = ToolkitMessageIntegration(
        message_handler=HumanToolkit(
            options.project_id, data.name
        ).send_message_to_user
    )
    enhanced_description = attach_remote_sub_agent_if_enabled(
        options=options,
        agent_name=data.name,
        working_directory=working_directory,
        tools=tools,
        tool_names=tool_names,
        system_message=enhanced_description,
        local_tool_description="local custom-agent tools or terminal actions",
        message_integration=message_integration,
    )

    # Pass per-agent custom model config if available
    custom_model_config = getattr(data, "custom_model_config", None)
    return agent_model(
        data.name,
        enhanced_description,
        options,
        tools,
        tool_names=tool_names,
        custom_model_config=custom_model_config,
    )
