from pathlib import Path

WORKTREE_ROOT = Path.home() / ".autoevolve" / "worktrees"


def display_path(path: str | Path) -> str:
    expanded = Path(path).expanduser()
    home = Path.home()
    try:
        relative = expanded.relative_to(home)
    except ValueError:
        return str(expanded)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"
