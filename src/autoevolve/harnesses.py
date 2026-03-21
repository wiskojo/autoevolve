import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

CONTINUE_HOOK_MESSAGE = "continue"


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
    display_name: str
    handoff_prompt: str
    prompt_path: str
    uses_skill_frontmatter: bool
    continue_hook_files: tuple[ContinueHookFileSpec, ...] = ()

    @property
    def supports_continue_hook(self) -> bool:
        return bool(self.continue_hook_files)


def _load_json_object(existing_text: str | None) -> dict[str, object]:
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


def _build_hook_file(
    existing_text: str | None, *, event_name: str, command: str, name: str | None = None
) -> str:
    settings = _load_json_object(existing_text)
    hook = {"type": "command", "command": command}
    if name is not None:
        hook["name"] = name
    settings["hooks"] = _append_hook_entry(settings.get("hooks"), event_name, {"hooks": [hook]})
    return f"{json.dumps(settings, indent=2)}\n"


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
        display_name="Claude Code",
        handoff_prompt="/autoevolve",
        prompt_path=".claude/skills/autoevolve/SKILL.md",
        uses_skill_frontmatter=True,
        continue_hook_files=(
            ContinueHookFileSpec(
                path=".claude/settings.json",
                build_contents=lambda existing_text: _build_hook_file(
                    existing_text,
                    event_name="Stop",
                    command=(
                        "printf '%s\\n' "
                        f"{json.dumps({'decision': 'block', 'reason': CONTINUE_HOOK_MESSAGE}, separators=(',', ':'))!r}"
                    ),
                ),
            ),
        ),
    ),
    Harness.CODEX: HarnessSpec(
        display_name="Codex",
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
                build_contents=lambda existing_text: _build_hook_file(
                    existing_text,
                    event_name="Stop",
                    command=(
                        "cat >/dev/null; printf '%s\\n' "
                        f"{json.dumps({'decision': 'block', 'reason': CONTINUE_HOOK_MESSAGE}, separators=(',', ':'))!r}"
                    ),
                ),
            ),
        ),
    ),
    Harness.GEMINI: HarnessSpec(
        display_name="Gemini",
        handoff_prompt="autoevolve",
        prompt_path=".gemini/skills/autoevolve/SKILL.md",
        uses_skill_frontmatter=True,
        continue_hook_files=(
            ContinueHookFileSpec(
                path=".gemini/settings.json",
                build_contents=lambda existing_text: _build_hook_file(
                    existing_text,
                    event_name="AfterAgent",
                    command=(
                        "printf '%s\\n' "
                        f"{json.dumps({'decision': 'deny', 'reason': CONTINUE_HOOK_MESSAGE}, separators=(',', ':'))!r}"
                    ),
                    name="autoevolve-continue",
                ),
            ),
        ),
    ),
    Harness.OTHER: HarnessSpec(
        display_name="Other",
        handoff_prompt="Read PROGRAM.md, then start working.",
        prompt_path="PROGRAM.md",
        uses_skill_frontmatter=False,
    ),
}


def get_harness_spec(harness: Harness) -> HarnessSpec:
    return HARNESS_SPECS[harness]
