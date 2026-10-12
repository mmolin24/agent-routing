# How it evolved

Agent-routing improves through the friction its orchestrators report with
`route.py --feedback`. A long-lived Claude maintainer audits each report: is it a
one-off, or a gap in routing? It reproduces the problem, fixes it in a worktree,
tests, gets an independent review, ships, and logs what changed and why.
Changes to grants, credentials, hard rules or capacity limits become proposals
for the human to decide, and some reports end with guidance only.

These are the changes from that loop that mattered most: each one closed a way
to lose work, miss a stalled task, or disturb a live worker or machine.

## Talking to running workers

- **What happened:** A worker asked a question mid-run. Answering it the way an
  approval dialog is answered sent a key that interrupted the run.
  **What changed:** `--question` and `--reply` are a logged path for questions;
  `--blocked` tells questions from approvals, and `--answer` refuses keys there.

- **What happened:** An orchestrator needed to narrow a running task and could
  only do it by prompting the agent directly, with no record in routing.
  **What changed:** `--steer` became the one logged way to narrow scope or set a
  stop point on a working worker, approved by the human first.

## Not losing work

- **What happened:** A worker's commits sat only in its local worktree,
  invisible to the orchestrator and lost if the machine went down.
  **What changed:** `--status` shows unpushed, stashed and dirty counts per
  worktree. After a human decision, the `draft-pr` grant asks for checkpoint pushes,
  and `UNPUSHED` fires once the oldest unpushed commit ages past an hour.

- **What happened:** A machine restarted under open tasks. Its panes came back
  and looked alive, but every process in them had died.
  **What changed:** Preflight records the host's boot identity at dispatch.
  A different boot marks the task `INTERRUPTED` and fires `HOST_RESET`; routing
  never closes or replays it. Reports moved from `/tmp` to `ROUTE_REPORT_DIR`,
  which survives a restart.

## Noticing what needs the orchestrator

- **What happened:** The event consumer exited and wasn't restarted, so finished
  workers sat unread while their tasks stayed open.
  **What changed:** `--events --follow` is a long-running stream that is started
  once. `--status` and the views warn when nobody is listening, and `STILL_STOPPED`
  fires when a stopped worker's output still hasn't been collected.

- **What happened:** Draining the queue without `--dispatch` recorded tasks as
  started and held their heavy slots, but no worker ever launched.
  **What changed:** `--next` alone is a dry run, and reports "started" only after
  a launch. `NO_WORKER` flags an open task whose worker never appeared.

- **What happened:** A queued task blocked on a stale commit sat at the head of
  the queue and held up work that could have run.
  **What changed:** Draining reports a blocked head and starts the next task that
  fits, while a head that is only waiting for capacity still keeps its place.

## Protecting the machines

- **What happened:** The volume behind the desktop's virtual disks filled up.
  Inside the VM the disk still looked empty, until the container engine crashed.
  **What changed:** `--status` and the views show free space on the real backing
  volume, and worker briefs treat I/O errors and engine crashes as host faults to
  report, not test failures to retry.

- **What happened:** Routing's own Docker probes kept starting session-bus
  daemons that never exited, until the machine ran out of a system resource.
  **What changed:** Every Docker call routing makes disables session-bus
  autolaunch, and a test guards it.

- **What happened:** Agents sharing a directory were shown each other's profiles
  and handoffs, because the newest session file won.
  **What changed:** Profiles, child agents and `--handoff` read each agent's own
  session file by its Herdr session id, falling back to the newest file only when
  that id isn't known.

## Patterns

- **Make the failure visible before automating the fix.** Unpushed work, disk
  space and restarts became visible first; anything that acts on them was a
  separate decision.
- **The human owns authority.** Checkpoint pushes, review scope and cleanup went
  through proposals, never straight into a grant.
- **Detect an interruption; never replay it.** A new boot means the processes are
  gone. The task stays open until someone inspects it.
- **Audit the report before building.** Several reports turned out to be one-offs
  in how a task was written, and got guidance instead of code.
