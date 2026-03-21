from pathlib import Path

from autoevolve.git import find_repo_root
from autoevolve.harnesses import HARNESS_SPECS, Harness, get_harness_spec
from autoevolve.models.experiment import PromptFile
from autoevolve.problem import parse_problem_spec
from autoevolve.prompt import build_harness_skill_prompt, build_problem_template
from autoevolve.repository import (
    EXPERIMENT_FILE,
    JOURNAL_FILE,
    PROBLEM_FILE,
    parse_experiment_document,
)


class Scaffolder:
    def __init__(self, cwd: str | Path = ".") -> None:
        self.root = find_repo_root(cwd)

    def apply_init(self, harness: Harness, continue_hook: bool) -> list[str]:
        spec = get_harness_spec(harness)
        written: list[str] = []
        if not (self.root / PROBLEM_FILE).exists():
            (self.root / PROBLEM_FILE).write_text(
                build_problem_template(), encoding="utf-8"
            )
            written.append(PROBLEM_FILE)
        prompt_path = self.root / spec.prompt_path
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(build_harness_skill_prompt(harness), encoding="utf-8")
        written.append(spec.prompt_path)
        if continue_hook:
            for file_spec in spec.continue_hook_files:
                path = self.root / file_spec.path
                existing = path.read_text(encoding="utf-8") if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(file_spec.build_contents(existing), encoding="utf-8")
                written.append(file_spec.path)
        return written

    def prompt_files(self) -> list[PromptFile]:
        files: list[PromptFile] = []
        for harness, spec in HARNESS_SPECS.items():
            path = self.root / spec.prompt_path
            if path.exists():
                files.append(PromptFile(harness=harness.value, path=path))
        return files

    def update_prompt(self, prompt_file: PromptFile) -> None:
        harness = Harness(prompt_file.harness)
        prompt_file.path.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.path.write_text(
            build_harness_skill_prompt(harness), encoding="utf-8"
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        problem_path = self.root / PROBLEM_FILE
        if not problem_path.exists():
            problems.append(f"Missing {PROBLEM_FILE}. Run autoevolve init first.")
            problem = None
        else:
            try:
                problem = parse_problem_spec(problem_path.read_text(encoding="utf-8"))
            except ValueError as error:
                problems.append(str(error))
                problem = None
        if not self.prompt_files():
            problems.append(
                "Missing prompt file. Expected PROGRAM.md or a supported harness skill file."
            )
        journal_path = self.root / JOURNAL_FILE
        experiment_path = self.root / EXPERIMENT_FILE
        if journal_path.exists() or experiment_path.exists():
            if not journal_path.exists():
                problems.append(f"Missing {JOURNAL_FILE}.")
            if not experiment_path.exists():
                problems.append(f"Missing {EXPERIMENT_FILE}.")
            if experiment_path.exists():
                try:
                    document = parse_experiment_document(
                        experiment_path.read_text(encoding="utf-8")
                    )
                except ValueError as error:
                    problems.append(str(error))
                else:
                    if problem is not None and problem.metric not in document.metrics:
                        problems.append(
                            f'{EXPERIMENT_FILE} must record the primary metric "{problem.metric}" '
                            f"declared in {PROBLEM_FILE} ({problem.raw})."
                        )
        return problems
