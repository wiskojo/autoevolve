import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

CONTINUE_HOOK_MESSAGE = "continue"
SHELL_CONTINUE_HOOK_COMMAND = f"printf '%s\\n' {CONTINUE_HOOK_MESSAGE!r} >&2; exit 2"
CODEX_CONTINUE_HOOK_COMMAND = (
    "cat >/dev/null; printf '%s\\n' "
    f"{json.dumps({'decision': 'block', 'reason': CONTINUE_HOOK_MESSAGE}, separators=(',', ':'))!r}"
)


class Harness(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"
    OTHER = "other"


@dataclass(frozen=True)
class ContinueHookFileSpec:
    path: str
    build_contents: Callable[[str | None], str]


@dataclass(frozen=True)
class HarnessSpec:
    handoff_prompt: str
    prompt_path: str
    uses_skill_frontmatter: bool
    continue_hook_files: tuple[ContinueHookFileSpec, ...] = ()

    @property
    def supports_continue_hook(self) -> bool:
        return bool(self.continue_hook_files)


def _parse_json_object_file(existing_text: str | None) -> dict[str, object]:
    if existing_text is None:
        return {}
    parsed = json.loads(existing_text)
    if not isinstance(parsed, dict):
        raise ValueError("settings file must contain a JSON object.")
    return dict(parsed)


def _append_hook_entry(
    hooks_value: object, event_name: str, hook_entry: Mapping[str, object]
) -> dict[str, object]:
    hooks = dict(hooks_value) if isinstance(hooks_value, dict) else {}
    existing_entries = (
        list(hooks.get(event_name, [])) if isinstance(hooks.get(event_name), list) else []
    )
    if all(entry != hook_entry for entry in existing_entries):
        existing_entries.append(hook_entry)
    hooks[event_name] = existing_entries
    return hooks


def _build_claude_continue_hook_settings(existing_text: str | None) -> str:
    settings = _parse_json_object_file(existing_text)
    hook_entry = {"hooks": [{"type": "command", "command": SHELL_CONTINUE_HOOK_COMMAND}]}
    settings["hooks"] = _append_hook_entry(settings.get("hooks"), "Stop", hook_entry)
    return f"{json.dumps(settings, indent=2)}\n"


def _build_gemini_continue_hook_settings(existing_text: str | None) -> str:
    settings = _parse_json_object_file(existing_text)
    hook_entry = {
        "hooks": [
            {
                "name": "autoevolve-continue",
                "type": "command",
                "command": SHELL_CONTINUE_HOOK_COMMAND,
            }
        ]
    }
    settings["hooks"] = _append_hook_entry(settings.get("hooks"), "AfterAgent", hook_entry)
    return f"{json.dumps(settings, indent=2)}\n"


def _build_codex_hooks(existing_text: str | None) -> str:
    hooks_document = _parse_json_object_file(existing_text)
    hook_entry = {"hooks": [{"type": "command", "command": CODEX_CONTINUE_HOOK_COMMAND}]}
    hooks_document["hooks"] = _append_hook_entry(hooks_document.get("hooks"), "Stop", hook_entry)
    return f"{json.dumps(hooks_document, indent=2)}\n"


def _build_codex_config(existing_text: str | None) -> str:
    if existing_text is None or not existing_text.strip():
        return "[features]\ncodex_hooks = true\n"
    if "codex_hooks" in existing_text:
        updated = existing_text.replace("codex_hooks = false", "codex_hooks = true")
        return f"{updated.strip()}\n"
    if "[features]" in existing_text:
        updated = existing_text.replace("[features]", "[features]\ncodex_hooks = true", 1)
        return f"{updated.strip()}\n"
    return f"{existing_text.strip()}\n\n[features]\ncodex_hooks = true\n"


HARNESS_SPECS = {
    Harness.CLAUDE: HarnessSpec(
        handoff_prompt="/autoevolve",
        prompt_path=".claude/skills/autoevolve/SKILL.md",
        uses_skill_frontmatter=True,
        continue_hook_files=(
            ContinueHookFileSpec(
                path=".claude/settings.json",
                build_contents=_build_claude_continue_hook_settings,
            ),
        ),
    ),
    Harness.CODEX: HarnessSpec(
        handoff_prompt="$autoevolve",
        prompt_path=".codex/skills/autoevolve/SKILL.md",
        uses_skill_frontmatter=True,
        continue_hook_files=(
            ContinueHookFileSpec(
                path=".codex/config.toml",
                build_contents=_build_codex_config,
            ),
            ContinueHookFileSpec(
                path=".codex/hooks.json",
                build_contents=_build_codex_hooks,
            ),
        ),
    ),
    Harness.GEMINI: HarnessSpec(
        handoff_prompt="autoevolve",
        prompt_path=".gemini/skills/autoevolve/SKILL.md",
        uses_skill_frontmatter=True,
        continue_hook_files=(
            ContinueHookFileSpec(
                path=".gemini/settings.json",
                build_contents=_build_gemini_continue_hook_settings,
            ),
        ),
    ),
    Harness.OTHER: HarnessSpec(
        handoff_prompt="Read PROGRAM.md and start working.",
        prompt_path="PROGRAM.md",
        uses_skill_frontmatter=False,
    ),
}


def get_harness_spec(harness: Harness) -> HarnessSpec:
    return HARNESS_SPECS[harness]
