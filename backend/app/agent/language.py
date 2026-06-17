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

from camel.messages import BaseMessage


def get_output_language(language: str | None) -> str:
    """Return a model-friendly language name from a UI locale/code."""
    if not language:
        return "English"

    normalized = language.strip().lower().replace("_", "-")
    if not normalized or normalized == "system":
        return "English"

    language_map = (
        (
            ("zh-hant", "zh-tw", "zh-hk", "traditional", "繁体", "繁體"),
            "繁體中文",
        ),
        (("zh", "cn", "chinese", "中文", "简体"), "简体中文"),
        (("ja", "japanese", "日本"), "日本語"),
        (("ko", "korean", "한국"), "한국어"),
        (("es", "spanish", "español"), "Español"),
        (("fr", "french", "français"), "Français"),
        (("de", "german", "deutsch"), "Deutsch"),
        (("pt", "portuguese", "português"), "Português"),
        (("ru", "russian", "рус"), "Русский"),
        (("ar", "arabic", "عربي"), "العربية"),
        (("it", "italian", "italiano"), "Italiano"),
        (("en", "english"), "English"),
    )
    for markers, output_language in language_map:
        if any(marker in normalized for marker in markers):
            return output_language

    return language.strip()


def build_output_language_policy(language: str | None) -> str:
    output_language = get_output_language(language)
    return (
        "\n\n<output_language_policy>\n"
        f"The current user's language is {output_language}. "
        "All user-facing replies, task descriptions, plans, summaries, "
        "status messages, subtask deliverables, and final answers MUST be "
        f"written in {output_language}. If tool results, web pages, API data, "
        "or source material are in another language, translate the user-facing "
        f"content into {output_language} before returning it. Keep code, "
        "commands, file paths, URLs, API names, units, and proper nouns in "
        "their original form unless a conventional localized name exists.\n"
        "</output_language_policy>"
    )


def get_task_label(language: str | None, follow_up: bool = False) -> str:
    output_language = get_output_language(language)
    labels = {
        "简体中文": ("任务", "后续任务"),
        "繁體中文": ("任務", "後續任務"),
        "日本語": ("タスク", "後続タスク"),
        "한국어": ("작업", "후속 작업"),
        "Español": ("Tarea", "Tarea de seguimiento"),
        "Français": ("Tache", "Tache de suivi"),
        "Deutsch": ("Aufgabe", "Folgeaufgabe"),
        "Português": ("Tarefa", "Tarefa de acompanhamento"),
        "Русский": ("Задача", "Последующая задача"),
        "العربية": ("مهمة", "مهمة متابعة"),
        "Italiano": ("Attivita", "Attivita di follow-up"),
        "English": ("Task", "Follow-up Task"),
    }
    default_label, follow_up_label = labels.get(
        output_language, labels["English"]
    )
    return follow_up_label if follow_up else default_label


def append_output_language_policy(
    system_message: BaseMessage | str | None, language: str | None
) -> BaseMessage | str | None:
    if system_message is None:
        return None

    policy = build_output_language_policy(language)
    if isinstance(system_message, str):
        return f"{system_message}{policy}"

    if isinstance(system_message, BaseMessage):
        content = f"{system_message.content}{policy}"
        if hasattr(system_message, "model_copy"):
            return system_message.model_copy(update={"content": content})
        if hasattr(system_message, "copy"):
            return system_message.copy(update={"content": content})
        return BaseMessage.make_assistant_message(
            role_name=system_message.role_name,
            content=content,
        )

    return system_message
