# Planner

Type a goal and the model writes a plan in a clean context, one step per line:
`N. [tool] what to do (after: N)`. Code: `plans.py`, `planning.py`.

- **Expanding.** `/expand [N,M] [times=K]` (or Expand on the web) runs one pass, or K,
  over all steps or the ones named. Each pass rewrites a step's details in more depth:
  3 lines on the first pass, then 6, 12, up to 24. Each step gets its own
  clean-context call that sees the goal, the step and its current details. Steps are
  expanded in parallel on as many model instances as RAM allows.
- **Editing.** A plan is plain text: each step on an unindented line, its details on
  the indented lines below it. Edit it with `/edit` (in `$EDITOR`, or typed) or in the
  web editor (Tab indents), or write one yourself with `/draft`. A step left as it was
  keeps its status and its expansion depth.

  ```
  1. [safe_read_file] Read the CLI
     find where arguments are parsed
  2. [none] Add --verbose (after: 1)
  ```
- **Running.** `/run fresh` gives each step a new, empty context: the plan, the step's
  details and the results of the steps it depends on. `/run continuous` carries one
  context across all steps, refreshed as it fills. `parallel` runs independent steps
  at the same time, as RAM allows (fresh runs). Each step ends with `DONE: …` or
  `FAILED: …`; when a step fails, the steps that depend on it are skipped. Runs use the
  [workspace tools](tools.md) and nothing else: no knowledge base.
- **Ticking steps.** On the web, tick a step to mark it done by hand, or untick it
  (or *untick all*) so the next run does it again.
- **Deleting.** On the web, Delete on a plan's page or the ✕ in the list. Files its
  runs wrote stay.

Files a plan run writes with `risky_write_file` are listed under
[Relics](web.md#relics) in the web interface.

Plans are saved in `.smallharness/plans/` as `<id>.json`, with a readable `<id>.md`
beside it.
