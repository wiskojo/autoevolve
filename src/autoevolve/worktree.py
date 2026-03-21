import json
import shutil
from pathlib import Path

from git.exc import GitCommandError

from autoevolve.git import find_repo_root, list_linked_worktrees, open_repo
from autoevolve.models.experiment import ExperimentWorktree
from autoevolve.models.worktree import (
    CleanedWorktrees,
    RecordedExperiment,
    StartedExperiment,
)
from autoevolve.repository import (
    EXPERIMENT_FILE,
    JOURNAL_FILE,
    WORKTREE_ROOT,
    ExperimentRepository,
    parse_experiment_document,
)

_REF_PREFIX = "autoevolve/"


class ExperimentWorktreeManager:
    def __init__(self, cwd: str | Path = ".") -> None:
        self.root = find_repo_root(cwd)
        self.repo = open_repo(self.root)
        self.cwd = Path(cwd).resolve()

    def start(self, name: str, summary: str, from_ref: str | None) -> StartedExperiment:
        name = self._validate_name(name)
        ref_name = f"{_REF_PREFIX}{name}"
        try:
            self.repo.git.check_ref_format(f"refs/heads/{ref_name}")
        except GitCommandError as error:
            raise ValueError(
                f'"{ref_name}" is not a valid managed experiment branch name.'
            ) from error
        if any(head.name == ref_name for head in self.repo.heads):
            raise RuntimeError(f'Branch "{ref_name}" already exists.')

        path = (WORKTREE_ROOT / name).resolve()
        if path.exists():
            raise RuntimeError(f"Worktree path already exists: {path}")

        current_branch = self.repo.git.branch("--show-current").strip()
        base_ref = from_ref or current_branch or "HEAD"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.repo.git.worktree("add", "-b", ref_name, str(path), self.repo.commit(base_ref).hexsha)
        (path / JOURNAL_FILE).write_text(self._journal_stub_text(name), encoding="utf-8")
        (path / EXPERIMENT_FILE).write_text(
            json.dumps({"summary": summary, "metrics": {}, "references": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        return StartedExperiment(branch=ref_name, base_ref=base_ref, path=path)

    def record(self) -> RecordedExperiment:
        current_worktree = next(
            worktree
            for worktree in list_linked_worktrees(self.repo, current_path=self.cwd)
            if worktree.is_current
        )
        branch_name = current_worktree.branch
        if not branch_name:
            raise RuntimeError("record requires an attached branch.")
        if not branch_name.startswith(_REF_PREFIX):
            raise RuntimeError(
                f"record only works on managed autoevolve experiment branches ({_REF_PREFIX}<name>)."
            )

        root = current_worktree.path.resolve()
        managed_root = WORKTREE_ROOT.resolve()
        if root != managed_root and managed_root not in root.parents:
            raise RuntimeError(
                f"record must be run from a managed autoevolve worktree under {managed_root}."
            )

        worktree_repo = open_repo(root)
        git_dir = Path(worktree_repo.git.rev_parse("--git-dir").strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        common_git_dir = Path(worktree_repo.git.rev_parse("--git-common-dir").strip())
        if not common_git_dir.is_absolute():
            common_git_dir = (root / common_git_dir).resolve()
        if git_dir == common_git_dir:
            raise RuntimeError("record refuses to remove the primary worktree.")

        journal_path = root / JOURNAL_FILE
        experiment_path = root / EXPERIMENT_FILE
        if not journal_path.exists() or not experiment_path.exists():
            raise RuntimeError(f"record requires both {JOURNAL_FILE} and {EXPERIMENT_FILE}.")

        experiment_name = branch_name.removeprefix(_REF_PREFIX)
        journal_text = journal_path.read_text(encoding="utf-8").strip()
        if journal_text == self._journal_stub_text(experiment_name).strip():
            raise RuntimeError(f"Replace the {JOURNAL_FILE} stub before committing.")

        document = parse_experiment_document(experiment_path.read_text(encoding="utf-8"))
        if not worktree_repo.is_dirty(untracked_files=True):
            raise RuntimeError("No changes to commit.")

        message = next((line.strip() for line in document.summary.splitlines() if line.strip()), "")
        if not message:
            raise RuntimeError(f"{EXPERIMENT_FILE} summary must not be empty.")

        worktree_repo.git.add("-A")
        worktree_repo.git.commit("-m", message)
        sha = worktree_repo.head.commit.hexsha
        self.repo.git.worktree("remove", str(root))
        return RecordedExperiment(branch=branch_name, sha=sha, path=root)

    def clean(self, name: str | None, force: bool) -> CleanedWorktrees:
        worktrees = ExperimentRepository(self.root).active_worktrees()
        managed = [
            worktree for worktree in worktrees if worktree.is_managed and not worktree.is_primary
        ]
        experiment_name = ""
        if name is not None:
            experiment_name = self._normalize_name(name)
            target_path = (WORKTREE_ROOT / experiment_name).resolve()
            target = next(
                (worktree for worktree in worktrees if worktree.path == target_path), None
            )
            if target is None or target.is_primary or not target.is_managed:
                raise RuntimeError(
                    f'No managed experiment worktree named "{experiment_name}" found for this repository.'
                )
            managed = [target]

        if not managed:
            return CleanedWorktrees(experiment_name=experiment_name, removed=())

        blocked = [worktree for worktree in managed if worktree.is_missing or worktree.dirty]
        if blocked and not force:
            reason = (
                "Refusing to remove a dirty or missing linked worktree without --force:"
                if len(blocked) == 1
                else "Refusing to remove dirty or missing linked worktrees without --force:"
            )
            details = "\n".join(
                f"  {self._describe_worktree_for_removal(worktree)}" for worktree in blocked
            )
            raise RuntimeError(f"{reason}\n{details}")

        removed = []
        for worktree in managed:
            if worktree.is_missing:
                shutil.rmtree(worktree.path, ignore_errors=True)
                self.repo.git.worktree("prune", "--expire", "now")
            else:
                args = ["worktree", "remove"]
                if force or worktree.dirty:
                    args.append("--force")
                args.append(str(worktree.path))
                self.repo.git.execute(["git", *args])
            if worktree.branch and any(head.name == worktree.branch for head in self.repo.heads):
                self.repo.git.branch("-D", worktree.branch)
            removed.append(worktree)

        return CleanedWorktrees(experiment_name=experiment_name, removed=tuple(removed))

    @staticmethod
    def _normalize_name(name: str) -> str:
        value = name.strip()
        if value.startswith(_REF_PREFIX):
            value = value.removeprefix(_REF_PREFIX)
        return value

    @staticmethod
    def _validate_name(name: str) -> str:
        value = ExperimentWorktreeManager._normalize_name(name)
        if not value:
            raise ValueError("Experiment name must not be empty.")
        if WORKTREE_ROOT.resolve() not in (WORKTREE_ROOT / value).resolve().parents:
            raise ValueError(f'"{name}" is not a valid experiment name.')
        return value

    @staticmethod
    def _journal_stub_text(name: str) -> str:
        return f"# {name}\n\nTODO: fill this in once you're done with your experiment.\n"

    @staticmethod
    def _describe_worktree_for_removal(worktree: ExperimentWorktree) -> str:
        state = "missing" if worktree.is_missing else "dirty" if worktree.dirty else "clean"
        return f"{worktree.path} ({worktree.branch or '(detached HEAD)'}, {state}, {worktree.head[:7]})"
