import json

from inline_snapshot import snapshot

from tests.e2e.conftest import RepoFixture


def test_init_other(repo: RepoFixture) -> None:
    result = repo.run("init", "--harness", "other", "--yes")
    assert repo.normalize(result.stdout) == snapshot(
        """\
Setup
Repository    <PATH_1>
Harness       other
Problem       write PROBLEM.md

Files
write PROBLEM.md
write PROGRAM.md

autoevolve initialized
Written       PROBLEM.md
              PROGRAM.md
Next          Read PROGRAM.md and start working.
"""
    )
    assert (repo.root / "PROBLEM.md").exists()
    assert (repo.root / "PROGRAM.md").exists()


def test_help(repo: RepoFixture) -> None:
    result = repo.run("--help")
    assert result.stdout == snapshot(
        """\
Usage: autoevolve [OPTIONS] COMMAND [ARGS]...

  Git-backed experiment loops for coding agents.

Options:
  --help  Show this message and exit.

Human:
  init      Set up PROBLEM.md and agent instructions.
  validate  Check that the repo is ready for autoevolve.
  update    Update detected prompt files to the latest version.

Lifecycle:
  start     Create a managed experiment branch and worktree.
  record    Validate, commit, and remove the current managed worktree.
  clean     Remove stale managed worktrees for this repository.

Inspect:
  status    Show the current experiment status.
  log       Show experiment logs.
  show      Show experiment details.
  compare   Compare two experiments.
  lineage   Show experiment lineage around one ref.

Analytics:
  recent    List the most recent recorded experiments.
  best      List the top experiments for one metric.
  pareto    List the Pareto frontier for selected metrics.

Examples:
  autoevolve start tune-thresholds "Try a tighter threshold sweep" --from 07f1844
  autoevolve record
  autoevolve log
  autoevolve recent --limit 5
  autoevolve best --max benchmark_score --limit 5

Run "autoevolve <command> --help" for command-specific details.
"""
    )


def test_validate(repo: RepoFixture) -> None:
    repo.init_other()
    result = repo.run("validate")
    assert result.stdout == snapshot("OK: repository is ready for autoevolve.\n")


def test_top_level_errors(repo: RepoFixture) -> None:
    not_repo = repo.run("status", cwd=repo.home, expect_failure=True)
    assert not_repo.stderr == snapshot("Not inside a git repository.\n")

    missing = repo.run("validate", expect_failure=True)
    assert missing.stderr == snapshot(
        """\
Missing PROBLEM.md. Run autoevolve init first.
Missing prompt file. Expected PROGRAM.md or a supported harness skill file.
"""
    )


def test_validate_invalid_experiment(repo: RepoFixture) -> None:
    repo.run("init", "--harness", "other", "--yes")
    repo.write_problem()
    (repo.root / "EXPERIMENT.json").write_text("{\n", encoding="utf-8")
    (repo.root / "JOURNAL.md").write_text("# x\n", encoding="utf-8")

    result = repo.run("validate", expect_failure=True)
    assert result.stderr == snapshot(
        "Expecting property name enclosed in double quotes: line 2 column 1 (char 2)\n"
    )


def test_update(repo: RepoFixture) -> None:
    repo.run("init", "--harness", "other", "--yes")
    repo.run("init", "--harness", "codex", "--yes")
    (repo.root / "PROGRAM.md").write_text("stale program\n", encoding="utf-8")
    (repo.root / ".codex" / "skills" / "autoevolve" / "SKILL.md").write_text(
        "stale codex\n",
        encoding="utf-8",
    )
    result = repo.run("update", input_text="n\n")
    assert repo.normalize(result.stdout) == snapshot(
        """\
detected prompts:
  - .codex/skills/autoevolve/SKILL.md (codex)
  - PROGRAM.md (other)
Overwrite PROGRAM.md? [y/N]:
updated:
  - .codex/skills/autoevolve/SKILL.md
skipped:
  - PROGRAM.md
"""
    )


def test_init_gemini_continue_hook(repo: RepoFixture) -> None:
    repo.run("init", "--harness", "gemini", "--continue-hook", "--yes")
    settings = json.loads((repo.root / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert settings == {
        "hooks": {
            "AfterAgent": [
                {
                    "hooks": [
                        {
                            "name": "autoevolve-continue",
                            "type": "command",
                            "command": "printf '%s\\n' '{\"decision\":\"deny\",\"reason\":\"continue\"}'",
                        }
                    ]
                }
            ]
        }
    }
