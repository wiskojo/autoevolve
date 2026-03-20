# autoevolve

`autoevolve` lets you use your existing coding agent in a simple git-backed experiment loop.

Run it inside an existing project, let it set up the files your coding agent needs, and then let the agent iterate through experiments as git commits.

## Install

```bash
pip install autoevolve
```

## Quickstart

Initialize `autoevolve` in an existing git repo:

```bash
autoevolve init
```

`autoevolve init` walks you through the setup for your coding harness and problem:

- a `SKILL.md` or `PROGRAM.md`: the instructions your coding agent reads to use autoevolve
- `PROBLEM.md`: the goal, metric, constraints, and validation setup for your problem

From there, your agent works in the repo as usual. Experiment commits will include:

- `EXPERIMENT.json`: the structured record of the experiment, including summary, metrics, and any references to other experiments
- `JOURNAL.md`: the narrative record of the experiment, could include the hypothesis, changes made, validation used, outcome, and reflections
