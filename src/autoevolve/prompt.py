from __future__ import annotations

from dataclasses import dataclass

from autoevolve.constants import MANAGED_WORKTREE_ROOT, ROOT_FILES, format_home_relative_path
from autoevolve.harnesses import Harness, get_harness_spec
from autoevolve.problem import build_problem_metric_section


@dataclass(frozen=True)
class ProblemTemplateOptions:
    constraints: str
    goal: str
    metric: str
    metric_description: str
    validation: str


def _with_todo_fallback(value: str, fallback: str) -> str:
    return value.strip() if value.strip() else fallback


def build_protocol_body() -> str:
    managed_worktree_root = format_home_relative_path(MANAGED_WORKTREE_ROOT)
    return "\n".join(
        [
            "# autoevolve protocol",
            "",
            "## Setup",
            "",
            (
                f"Use setup mode whenever `{ROOT_FILES.problem}` is still "
                "incomplete, stubbed, or unclear."
            ),
            "",
            f"1. Read the repository and the current `{ROOT_FILES.problem}`.",
            (
                f"2. Work with the user to make `{ROOT_FILES.problem}` concrete. "
                "The goal, metric, constraints, and validation should be "
                "specific enough that you can execute against them."
            ),
            (
                f"3. If important details are missing, ask focused questions and "
                f"update `{ROOT_FILES.problem}` as you learn more."
            ),
            ("4. Once the validation is defined, run it to make sure it actually works."),
            (
                "5. If this is a new repo or there is no clear baseline yet, "
                "establish a baseline result before starting optimization."
            ),
            (
                f"6. Do not begin the forever experiment loop until "
                f"`{ROOT_FILES.problem}` is ready and the validation is working."
            ),
            ("7. Confirm with the user that everything is set up and ready to start autoevolve."),
            "",
            "## The Experiment Loop",
            "",
            (
                "Operate as an orchestrator. Your default mode is to run "
                "multiple experiments through subagents on separate git "
                "worktrees and branches managed by the autoevolve CLI. You "
                "are responsible for managing tradeoffs like exploration vs. "
                "exploitation, budget constraints, and resource allocation as "
                "you see fit. On each iteration, consider what has been tried "
                "so far and choose the next direction or set of directions most "
                "likely to make meaningful progress toward the goal, whether "
                "through small local moves, large global moves, or broader "
                "strategic shifts."
            ),
            "",
            "LOOP FOREVER:",
            "",
            (
                "1. Inspect the current experiment state. Start from the "
                "current branch and commit, then use the autoevolve CLI to "
                "inspect prior experiments, compare outcomes, identify "
                "promising branches to build on, and reason about the search "
                "as a whole. Use commands like `autoevolve status`, "
                "`autoevolve list`, `autoevolve best`, `autoevolve pareto`, "
                "`autoevolve compare`, and `autoevolve graph`, and anything "
                "else you have access to, to understand what has been tried "
                "and to reflect on where to branch next."
            ),
            (
                "2. Plan the next experiment or batch of experiments based on "
                "prior results. Diversify the search: try different ideas, "
                "different combinations of previous ideas, and entirely new "
                "directions when the current path looks narrow or stale."
            ),
            (
                "3. Launch the planned experiments. You should delegate each "
                "experiment to a subagent working on its own branch and in its "
                "own worktree. Use `autoevolve start <name> <summary> [--from "
                "<ref>]` to create each experiment in its own managed worktree. "
                "Parallelize aggressively when experiments are independent, but "
                "keep each subagent scoped to one experiment or one clear "
                "checkpoint, not an open-ended background stream. Keep yourself "
                "unblocked: while some agents are running, continue inspecting "
                "results, planning the next batch, preparing additional "
                "branches, or thinking through new ideas to try."
            ),
            (
                f"4. When an experiment reaches a meaningful checkpoint or is "
                f"complete, update `{ROOT_FILES.journal}` and "
                f"`{ROOT_FILES.experiment}`, then use `autoevolve record`. If "
                "a direction needs sustained work, continue it as a sequence "
                "of committed experiments rather than one giant uncommitted "
                "run. If you need to clear stale worktrees, use `autoevolve "
                "clean [<name>]` for housekeeping."
            ),
            "",
            (
                "The idea is that you are a completely autonomous orchestrator "
                "trying things out. If an approach works, keep it. If it "
                "doesn't, keep it as well. Continue advancing branches based "
                "on what appears most promising, forking and merging as needed "
                f"while running parallel experiments toward the goal in "
                f"`{ROOT_FILES.problem}`."
            ),
            "",
            (
                "**NEVER STOP**: Once the experiment loop has begun (after the "
                "initial setup), do NOT pause to ask the human if you should "
                'continue. Do NOT ask "should I keep going?" or "is this a '
                'good stopping point?". The human might be asleep, or gone '
                "from a computer and expects you to continue working "
                "indefinitely until you are manually stopped. You are "
                "autonomous. If you run out of ideas, think harder - re-read "
                "the in-scope files for new angles, try combining previous "
                "near-misses, try more radical architectural changes, or read "
                "research papers and explore the literature (but only if "
                f"permitted by `{ROOT_FILES.problem}`). The loop runs until "
                "the human interrupts you, period."
            ),
            "",
            (
                "**SUBAGENTS**: This autonomous loop only applies to the "
                "top-level orchestrator. Delegated subagents or workers should "
                "execute only their assigned experiment/checkpoint, record "
                "results, and then stop rather than starting or continuing the "
                "forever loop. If you are a subagent, `LOOP FOREVER` and any "
                "instruction to continue autonomously apply only to the "
                "top-level orchestrator, not to you."
            ),
            "",
            "Notes:",
            "",
            (
                f"- One commit = one atomic experiment. Keep "
                f"`{ROOT_FILES.journal}` and `{ROOT_FILES.experiment}` in sync "
                "with the code at that commit; do not leave them stale."
            ),
            (
                "- If you try an idea or obtain some intermediate result, "
                "however small (within reason), consider committing it as an "
                "experiment to document it and preserve traceability."
            ),
            (
                "- Commit both improvements and regressions. The goal is to "
                "preserve the full search history, not just the winners."
            ),
            (
                "- Prefer fan-out over a single linear chain. You do not need "
                "to merge back to `main`. Use `autoevolve start` to branch "
                "from the current commit, any promising experiment commit, or "
                "another intentionally chosen ref, and continue exploring "
                "outward through committed experiments rather than one "
                "long-lived uncommitted session."
            ),
            (
                "- When an experiment borrows ideas from another branch "
                "without direct Git ancestry, record this in "
                f"`{ROOT_FILES.experiment}` under `references`."
            ),
            (
                "- Keep the filesystem tidy. Put new files, intermediate "
                "results, and other artifacts in the repository itself, or "
                "preferably in managed worktrees under "
                f"`{managed_worktree_root}`, and clean them up before "
                "committing the experiment. Do not scatter files across "
                "`/tmp`, `/private`, cache directories, your home directory, "
                "or other ad hoc paths unless the task explicitly requires it. "
                "Managed worktrees should be temporary, and once you are done "
                "with them, clean them up with the autoevolve lifecycle "
                "commands."
            ),
            "",
            "## How autoevolve works",
            "",
            "### Problem Definition",
            "",
            (f"- `{ROOT_FILES.problem}` defines the task, metric, constraints, and validation."),
            (
                "- The `## Metric` section begins with a structured, non-empty "
                "line that defines the primary metric to optimize, followed by "
                "an optional description."
            ),
            (
                f"- During experiment loops, treat `{ROOT_FILES.problem}` as "
                "read-only unless a human explicitly changes the task."
            ),
            "",
            "### Experiment Record",
            "",
            (
                "An experiment is a git commit that contains both root "
                f"`{ROOT_FILES.journal}` and `{ROOT_FILES.experiment}`."
            ),
            "",
            (
                f"- `{ROOT_FILES.journal}` is the narrative record. You can "
                "update it as the experiment unfolds. Treat it as notes for "
                "your future self: hypotheses, changes made, intermediate "
                "observations, failures, reflections, and anything else worth "
                "preserving."
            ),
            "",
            (f"- `{ROOT_FILES.experiment}` is the structured record and should follow this shape:"),
            "",
            "  ```json",
            "  {",
            '    "summary": "one-sentence result summary",',
            '    "metrics": {',
            '      "primary_metric": 0.0,',
            '      "runtime_sec": 0.0',
            "    },",
            '    "references": [',
            "      {",
            '        "commit": "abc1234",',
            '        "why": "borrowed the premium-guard idea from this experiment"',
            "      }",
            "    ]",
            "  }",
            "  ```",
            "",
            (
                "- Prefer numeric values in `metrics` whenever possible so "
                "they can be graphed later."
            ),
            (
                f"- Every experiment should record the primary metric declared "
                f"in `{ROOT_FILES.problem}`."
            ),
            (
                "- Faithfully record the metrics produced by this experiment "
                "commit itself. `metrics` should be a truthful record of what "
                "this experiment achieved when evaluated."
            ),
            (
                "- You are not limited to the primary metric. Store any "
                "additional metrics that help you compare experiments, reason "
                "about tradeoffs, or plan future search."
            ),
            (
                "- Keep metric names consistent across experiments so tools "
                "can compare runs and compute best or Pareto-style frontiers "
                "later. But you can always add new metrics to the `metrics` "
                "object in later experiments if they help you reason about "
                "progress."
            ),
            (
                "- Every meaningful experiment should be recorded, even if it "
                "regresses. Do not rewrite history just to hide weaker results."
            ),
            "",
            "### Lineage",
            "",
            (
                "- Use `references` only for semantic links that are not "
                "already obvious from git ancestry, for example when you "
                "borrow ideas from sibling branches without doing a formal "
                "merge."
            ),
            (
                "- Git ancestry captures code lineage. `references` captures "
                "idea lineage that git alone does not show."
            ),
            (
                "- Branches are operational handles for parallel work, not the "
                "source of truth. The commit is the durable experiment record."
            ),
            (
                "- When you run experiments concurrently, prefer using "
                "separate worktrees so each branch stays isolated and easy to "
                "inspect. In normal operation, assume you should be managing "
                "multiple concurrent experiment branches unless the task is "
                "inherently serial."
            ),
            "",
            "### Tooling",
            "",
            (
                "- The autoevolve CLI gives you structured views over this "
                "git-backed history. Under the hood it reads git history plus "
                f"`{ROOT_FILES.journal}` and `{ROOT_FILES.experiment}` from "
                "experiment commits and compiles them into useful views for "
                "planning the next experiment."
            ),
            (
                "- Use `autoevolve validate` to validate that the current "
                "checkout is a valid experiment."
            ),
            (
                "- Use `autoevolve start <name> <summary> [--from <ref>]` to "
                "create a managed experiment branch and worktree."
            ),
            (
                "- Use `autoevolve record` from inside a managed experiment "
                "worktree to commit the result and remove that worktree."
            ),
            (
                "- Use `autoevolve clean [<name>] [-f]` to remove stale "
                "managed worktrees under "
                f"`{managed_worktree_root}` for the current repository."
            ),
            "- Use `autoevolve list` to inspect prior experiments.",
            (
                "- Use `autoevolve recent`, `autoevolve best --max <metric>`, "
                "or `autoevolve pareto --max <metric> --min <metric>` to find "
                "strong candidates to build on."
            ),
            (
                "- Use `autoevolve compare <a> <b>` or `autoevolve graph <ref>` "
                "when you need to understand how experiments differ or connect."
            ),
            "- If you need the full command surface, use `autoevolve --help`.",
            "",
        ]
    )


def build_harness_prompt(harness: Harness) -> str:
    if not get_harness_spec(harness).uses_skill_frontmatter:
        return build_protocol_body()
    return "\n".join(
        [
            "---",
            "name: autoevolve",
            (
                "description: Use when working in a repository initialized "
                "with autoevolve. Follow setup if PROBLEM.md is incomplete, "
                "then run the autoevolve experiment loop and record "
                "experiments in JOURNAL.md and EXPERIMENT.json."
            ),
            "---",
            "",
            build_protocol_body(),
        ]
    )


def build_problem_template(options: ProblemTemplateOptions) -> str:
    goal = _with_todo_fallback(
        options.goal,
        "TODO: describe the goal you want the agent to solve for.",
    )
    metric = build_problem_metric_section(options.metric, options.metric_description)
    constraints = _with_todo_fallback(
        options.constraints,
        "TODO: list any hard constraints or non-goals.",
    )
    validation = _with_todo_fallback(
        options.validation,
        (
            "TODO: describe how progress should be validated. This can be a "
            "command, multiple commands, a script, or a manual evaluation "
            "procedure."
        ),
    )
    return f"""# Problem

## Goal
{goal}

## Metric
{metric}

## Constraints
{constraints}

## Validation
{validation}
"""
