# Agent Routing Guide

This is the shared instruction entry point for any orchestrator (Claude, Codex or
a human) that sends work to the right compute: the **desktop** (Windows, Ubuntu
on WSL2) or the **Mac**, dispatched through Herdr.

**Scope: where work runs, not how it's done.** Each project's own `AGENTS.md`,
skills and docs own the workflow: worktrees, verification gates, commit and PR
shape, the Linear workpad, model selection and reporting. This repo never
restates them. It decides the host, checks that the host can do the work, keeps
parallel workers from colliding on one machine, and renders the Herdr commands.

This repo is not a service: it has no daemon, database, scheduler or queue.

- Contract: `docs/task-contract.md`
- Adding a project: [machines, profiles and first dispatch](docs/adding-a-project.md).
- Host config: `config/hosts.example.json`. Copy it to the git-ignored `config/hosts.local.json`.
- Skill for orchestrators: `skills/agent-routing/SKILL.md`. It's the short path: route, start, listen, finish. Install it on each machine by symlinking it into `~/.claude/skills/`.
- Data for the views: `collector/` (Go, `agent-collect`). `route.py`'s views use it when it's installed and fall back to the Python readers (set `AGENT_COLLECT=0` to force Python). Both produce identical JSON; a refresh costs one SSH round trip either way (about 0.3 s).
- Router: `scripts/route.py`. A dry run by default; `--record` writes the git-ignored `runs/`; `--dispatch` starts the task and returns its **link**. Dispatch, listen and status live in `scripts/dispatch.py`. Read-only preflight: `scripts/preflight.sh`.

## 1. Hard rules

1. **A routing decision does not authorize execution.** Only a human grant recorded in the task's `authorization` does. `--dispatch` starts work only when the orchestrator asks, and only within that grant. The router itself never deploys, applies infrastructure, pushes, opens PRs, stops agents or changes security settings. The one exception is `route.py --reap --apply`, which the orchestrator runs (§4): it closes the tabs of leftover idle agents in issue workspaces once the issue is finished or has had no open task for 24 h. What a *worker* may do (e.g. example-app's draft-PR flow) comes from that grant plus the project's own rules.
2. **Honor explicit host requests. Never silently switch hosts.** If the requested host can't do the work, stop and ask.
3. **Pin exact commits.** Matching branch names can still point to different commits.
4. **Treat Mac checkouts as stale, and never mutate any main checkout.** On either host, never `pull`, `reset`, `clean`, `checkout`, `switch`, `restore` or `stash` them automatically. Preserve untracked files. `git fetch` is allowed on both hosts, because it only updates remote-tracking refs. A fetch refspec that writes a local branch is not allowed. Work happens in a worktree at `project_commit`.
5. **Route from one place.** The open-task list and queue live on the routing home (`routing_home`, the desktop). Commands that change them refuse to run anywhere else, so heavy caps and the queue order stay correct with any number of orchestrators.
6. **Don't let parallel workers collide on one host.** One worktree per worker, plus a per-task port slot for any runtime that publishes ports. Respect each host's `max_heavy` (§3).
7. **One outstanding task per worker.** `idle`/`done` only means the agent is ready for input. It doesn't mean the task succeeded. Read the output; verify per the project's own parent checks.
8. **Use explicit Herdr targets.** Rediscover IDs on the host where the command runs. Selecting something in the UI doesn't redirect commands in existing panes.
9. **Never automatically replay an interrupted mutating command.** Inspect state, record it, and ask.
10. **Keep credentials outside this repo.** Don't remove key passphrases, enable `ForwardAgent`, disable host-key checking, or record agent socket paths. They are temporary, not configuration.
11. **The project's rules win** for anything inside that project, including whether delegation is allowed at all (see the project's `AGENTS.md`).

## 2. Host selection

### By work type

| Work | Host | `requires` | Notes |
| --- | --- | --- | --- |
| Backend, worker, migrations, integration tests | either (load-shared) | `docker` | Add `"runtime": "compose"` when the work needs the project's Compose stack. |
| Web frontend, browser smoke, docs | either (load-shared) | `web` | Both machines run Node, pnpm and Playwright. Use `linux` only for work that truly needs Linux; it pins the desktop. |
| iOS simulator | mac | `macos`, `xcode`, `ios-simulator` | example-app's iOS harness refuses non-darwin. |
| Android emulator | mac | `android-emulator` | Android Studio, SDK and AVDs exist only on the Mac. |
| Cross-platform (web plus native) | both | — | **Split it:** a desktop task for web and tests (`--suffix web`), a Mac task for native verification (`--suffix native`, usually the `verify-only` grant). Only one may push: the router blocks two `project-workflow` tasks on the same branch across hosts. To verify the pushed PR natively, fetch on the Mac and route with `--commit origin/feature/APP-N`. |
| Staging review (`--grant staging-review`) | either (load-shared) | `staging-login` (added by the grant) | Runs in its own worktree `.worktrees/<issue>-staging`, detached at `origin/main` (fetch first), so the session helper is current: an issue worktree at an old feature head predates helper fixes. Confirm that the configured test roles are available on the selected host. |
| example-infra OpenTofu checks | mac | `opentofu` | `tofu` is installed only on the Mac (1.11.x). Applies are never routed. |
| Explicitly requested host | as requested | — | Blocked, not moved, if that host lacks a requirement. |

### Rules (applied in order by `choose_host`)

| # | Condition | Result |
| --- | --- | --- |
| 1 | `requires` contains both `linux` and a Mac-only tag | **Blocked.** Split the task. |
| 2 | `requested_host` is set | That host. If it lacks a required capability: **blocked**. |
| 3 | `requires` has a Mac-only tag (`macos`, `ios`, `xcode`, `ios-simulator`, `android-emulator`) | **Mac**, if it has every other required tag too. |
| 4 | More than one host has every required tag (`web`, `docker`) | **Load-shared:** hot hosts are skipped; for heavy work, so are hosts with no free heavy slot or a queue already waiting (it queues only when every capable host is full); then the lowest score wins: open tasks ÷ the host's `share` (e.g. desktop 1.0, Mac 0.6, to reserve capacity for interactive use), plus 0.5 for a host at warn level. Ties go to the desktop (`default_linux_host`). |
| 5 | Exactly one host has every required tag (e.g. `opentofu`) | **That host** |
| 6 | Nothing covers it | **Blocked** |

Capability tags per host live in the config (`capabilities`). Re-check them when tools are installed or removed.

| Host | Repositories | Reach from the other host |
| --- | --- | --- |
| desktop | `/home/dev/projects/example-app`, `/home/dev/projects/example-infra` | From the Mac: Herdr profile `Desktop WSL` (SSH alias `dev-wsl`) |
| mac | `/Users/dev/example-app`, `/Users/dev/example-infra` | From the desktop: Herdr profile `Mac` (SSH alias `dev-mac`) |

## 3. Capacity and isolation on one host

- **Heavy phases take turns: `scripts/heavy`.** Workers mostly wait on the model, so routing doesn't cap them. What loads a machine is their heavy *phases*: Docker or Compose, full test suites, typechecks, web builds and browser smokes. Every worker's brief tells it to wrap those commands, e.g. `~/projects/agent-routing/scripts/heavy pnpm ready:check <PR>`. At most `max_heavy` of them run at once per machine (e.g. desktop 3, Mac 2), and the rest wait first come, first served. Gates wrap theirs too, so gates and workers share one count. Slots are file locks under `~/.cache/agent-routing/heavy`, freed when the process exits, even after a crash. `heavy --status`, `--watch` and the map show who holds each slot and who waits. The dispatch tab sets `RT_TASK`, so slots are named by task.
- **One cap per host: `max_heavy`.** A task is heavy when it runs a Compose stack, when `resources.class` is `heavy` (readiness gates, full suites; `--heavy`), or when it requires `docker` unless explicitly marked `light` (`--light`, e.g. focused tests only). Light workers are not capped. Example config: desktop 3, Mac 2; tune these limits for the available hardware.
- **Heavy queue with backpressure.** With `--record`, a heavy task over the cap (or with no free port slot) is queued in `runs/queue/<host>/`, first in first out per host, and the router exits `4`. A new heavy task also queues behind any already waiting on that host. When a host's queue reaches `max_queue` (example: desktop 10, Mac 6), new heavy tasks are refused (exit `3`), so the producer must slow down. There is no daemon: the orchestrator drains the queue with `route.py --next --dispatch` after closing a task; without `--dispatch`, `--next` is a dry run that records and dequeues nothing; recording a task without a worker would reserve a slot without doing work. A head still waiting for a slot keeps everything behind it waiting on that host, but a task queued without an explicit host (load-shared) may start on any capable host that has room, in queue order; `--next --host X` drains X only. A **blocked** head (a stale commit, a missing tool) needs a person, not a slot, so `--next` reports it, leaves it queued and starts the next task that fits. `--dequeue <id>` drops a task. `--queue` shows open tasks, heavy counts and queues per host.
  - **Why:** concurrent heavy phases can exhaust memory and cause timeouts; even focused tests from light workers can add up.
  - **Desktop:** Docker in WSL shares the memory allocated to WSL. An example starting point is `max_heavy` 3 on a 16-thread, 24 GB machine; verify it under load.
  - **Mac:** size Docker memory for the workload and leave capacity for interactive use. Prefer another capable host when memory pressure is high.
  - Re-measure (load average, `free -g`, `memory_pressure`) and adjust the numbers. Raise them only with evidence.
- **Load-aware backpressure.** Routing reads each machine's live load (`scripts/host_load.py`: load per core, memory available, swap, Docker memory against its limit). It grades each one ok, warn or hot against `hot_limits` in the config (defaults: load ≥ 1.0 per core, < 10% memory available, Docker ≥ 90% of its limit; swap alone is only ever a warning, because macOS grows swap on demand). Memory pressure also counts. On the Mac it is the kernel's own level (`kern.memorystatus_vm_pressure_level`); on the desktop it is Linux PSI (`/proc/pressure/memory`: `some` avg60 ≥ 10% means warn, `full` avg60 ≥ 10% means critical). Warn grades the machine warn; critical grades it hot. Placement skips a **hot** machine when another can do the work. A **heavy** task that can only run on a hot machine queues. Light (model-bound) work still starts there with a warning, because its heavy phases wait for a slot (`scripts/heavy`). Heavy slots prevent concurrent checks from overloading a host. `--status`, `--watch` and the map show the load per machine. They also show free space on the volume that really holds the machine's disks: Windows' C: (`/mnt/c`) on WSL, whose `ext4.vhdx` and Docker `docker_data.vhdx` grow into it while `/` inside WSL always looks empty. Backing-volume exhaustion can crash Docker or the host even when the virtual disk shows free space. Disk space is measured but doesn't grade the machine yet; choose thresholds for your hardware before using it for placement.
- **Port slots.** example-app's Compose stack publishes `API_HOST_PORT` (3000), `POSTGRES_HOST_PORT` (5432) and `FRONTEND_HOST_PORT` (8081) whatever the Compose project is called. Its runtime smoke uses `RUNTIME_SMOKE_PORT` (43085) and a fixed image tag. For a `"runtime": "compose"` task, the router assigns the lowest free slot `n` (1..`max_slot`); every port becomes `base + n*step` and the image tag is made per-task. Slot 0, the defaults, belongs to the main checkout and a human.
  - The env is set on the task's own Herdr tab (`tab create --env K=V`, or `workspace create --env` for an issue's first task), so the worker's shell inherits it. Tabs don't share env, so tasks in one issue's workspace keep their own port slots. The worktree's `.env` is usually a symlink to the main checkout's, so it is never edited. Host-side DB commands must use `$POSTGRES_HOST_PORT`, which the handoff note says.
  - Preflight checks that the slot's ports aren't already listening on the target (`--ports-free`).
- **Worktree and branch collisions** are checked across strategies (an example-app issue task occupies `<repo>/.worktrees/<issue>` and `feature/<issue>`).
- **Dependencies.** example-app's `--deps link` symlinks `node_modules` into the main checkout, so an install inside that worktree writes into the main checkout. The router blocks link plus an install command; use `"deps": "install"` for dependency work.

## 4. Lifecycle

1. Describe the task. For a routine issue, build it from flags instead of writing JSON:
   `python3 scripts/route.py --issue APP-1 --requires web --grant draft-pr`
   Add `--compose` for a Compose stack, `--suffix web|native` when one issue has a task per host, `--host` for an explicit request, and `--model/--effort` from the project's routing guide. `--commit` defaults to `origin/main`, resolved read-only on the chosen host as of its last fetch (fetch first). Grants are named authority presets in the config (`draft-pr`, `local-only`, `verify-only`, `gate`, `review`). A `review` focuses on the diff and may run focused checks (wrapped in `scripts/heavy`) in the issue's worktree, never edit or push, and never touch a database. Dispatch it once the impl task is closed (`--close`), with `--host <impl's host>` so it runs in the worktree impl used, and `--commit` at the PR head (fetch there first so `origin/feature/APP-N` resolves). For anything unusual, write the task file (`docs/task-contract.md`).
2. Add `--run-preflight --record` to that command (or `route.py <task.json> --run-preflight --record`). Review the decision, blockers and every rendered command. `--record` writes `runs/tasks/<task_id>/{task.json, decision.json, handoff.md}` and `runs/active/<task_id>.json` (or into `--active-dir`).
3. Dispatch with `--dispatch` (or run the dry-run plan's commands by hand). It works the same from either machine (local, `--machine`, or SSH), in order:
   1. opens the task's own **tab** in **one Herdr workspace per issue**, labelled with the issue (`APP-1`), so Herdr lists issues rather than every task. The tab is labelled with the task's role (`impl`, `gate`, `staging`), and the workspace is created with the issue's first task. A task id without an issue key gets a workspace of its own. Legacy tasks may keep a one-workspace-per-task layout;
   2. creates the worktree with the project's helper and verifies `rc=0` and HEAD == `project_commit`;
   3. starts the worker (with model and effort flags if given);
   4. sends exactly one brief: the handoff note, which points the worker at the project's `AGENTS.md` and skill.

   It writes `runs/tasks/<id>/link.json`, the **link**: host, how to reach it, workspace, pane, agent, worktree, branch, verified HEAD, and the commands to listen. A failed step stops, is recorded in the link, and leaves the workspace open for inspection. Nothing is retried.
4. Listen and collect. The orchestrator keeps one `route.py --events` consumer running: preferably `--events --follow` attached once to a line watcher, which streams a line per thing that needs it and never exits on its own; or the one-shot `--events`, restarted after every exit (skill §3). `--status` and the views warn when no consumer has polled for 15 min while tasks are open. `--listen <id>` reads a worker through its link. `--status` shows every open task live on both machines, with the worker and any **children**: other agents in the task's workspace, such as child Codex or Claude runs. It also flags **untracked** agents that belong to no task. Read the project's own handoff (for example-app: workpad, PR, head SHA), verify the routing facts, and apply the project's parent checks. Work started outside routing (e.g. `codex exec` in a shell) can't be tracked.
   Issue views use each project's profile for its pipeline and tracker flow; an issue completes at its last pipeline step or tracker Done. Projects with `tracker: none` have no tracker line.
5. Close: `route.py --close <task_id> --outcome <result> --next --dispatch` once the result is recorded. Gate and review tasks require `--outcome` (passed, failed, approved, changes_required), so the views never show a guessed result. Record review and gate results that routing can't see with `--mark … --sha <commit>`, so they show as stale once the PR head moves (skill §4). That frees the task's heavy and port slot and starts the oldest queued heavy task that now fits. Re-check with `--run-preflight`, since queued tasks can wait a while. Stop any long-running panes you started. Once an issue is finished (PR merged or closed, the profile's last pipeline step done, or tracker Done, Canceled or Duplicate), or neither it nor the agent has been active for 24 h, `route.py --reap` lists its leftover idle agents and `--reap --apply` closes their tabs. It never runs while a task on the issue is open or queued, it never closes a tab that also holds another pane (a dev server, a shell, a live agent), and worktrees and session files stay. It runs only on the routing home. A `REAP` event says when an issue's agents can go. Open entries older than twice their timeout are flagged as probably finished but never closed.

## 5. Preflight

Run `scripts/preflight.sh` on the target, or let `route.py --run-preflight` do it. It is read-only: git runs with `--no-optional-locks`, fsmonitor off and lazy fetch disabled, and the preflight itself never fetches. If the required commit is missing, it blocks with "fetch first". It reports:

- that the host is reachable (over SSH, exit 255 means unreachable)
- required tools on a **non-interactive** PATH (via `path_prepend`; non-interactive shells may omit tool directories)
- `pnpm` version against the project pin (configure `pnpm_cmd` when the installed version differs from the project pin)
- the Herdr version and that its server is running
- repository identity: `origin` URL, toplevel, branch, HEAD
- dirty state: tracked changes and untracked files, counted separately
- behind/ahead relative to the last fetch, and the time of the last fetch
- commit availability: `--expect-commit` for in-place work or `--need-commit` for worktree work
- ports that must be free (`--ports-free`)
- for tasks that will commit (`draft-pr`): whether commit signing is ready (`--need-signing`). If GPG signing is configured, a locked agent blocks routing with an unlock command. A configured macOS Keychain signing helper is reported as `keychain`.

Anything blocked stops the task. Report the blocker. Don't repair it automatically.

## 6. Herdr command guide (v0.9.3, both hosts)

Syntax comes from `herdr --help`, the per-group help, `herdr --skill`, and the official docs (session-state, connecting-machines). **Live** means executed during setup; **Syntax** means checked against the 0.9.3 help only.

### Targeting

| From → To | Prefix | Status |
| --- | --- | --- |
| Same host, inside a Herdr pane (`HERDR_ENV=1`) | `herdr …` | Live |
| Mac → desktop | `herdr --machine "Desktop WSL" …` (or the profile ID from `herdr machine list --json`) | Live (`agent list`) |
| Desktop → Mac | `herdr --machine Mac …` (profile `<PROFILE_ID>`, discover with `herdr machine list --json`; SSH alias `dev-mac` underneath) | Live (dispatch, listen, status round trip). Probes and preflight still use `ssh dev-mac` directly |

- Selectors are profile IDs or unique, case-sensitive labels. **Profiles are not shared:** the Mac has `Desktop WSL`; the desktop has `Mac`.
- Don't combine `--machine` with `--session` or `--remote`. `--current` and inherited `HERDR_*` IDs never identify panes on another host.
- Over plain SSH there is no caller pane, so always pass explicit pane IDs or agent names. This deviates from `herdr --skill`, which expects control from inside a Herdr pane; it's acceptable only with explicit targets. The long-term fix is a desktop → Mac profile (`herdr machine add dev-mac --label Mac`), which is the human's decision: it prepares and may start the remote server.

### Discovery (read-only)

```sh
herdr status                      # Live
herdr machine list --json         # Live: saved profiles on THIS host
herdr machine status --json       # Live
herdr session list --json         # Live
herdr agent list                  # Live: agents, agent_status, pane_id, cwd
herdr workspace list              # Live
herdr pane list --workspace <W>   # Syntax
herdr agent get <name|pane>       # Syntax
```

### Dispatch (mutating; needs authorization)

```sh
herdr workspace list                                                                # the issue's workspace, by label (APP-N)
herdr tab create --workspace <W> --cwd <repo> --label <role> [--env K=V ...] --no-focus   # → .result.root_pane.pane_id
herdr workspace create --cwd <repo> --label <APP-N> [--env K=V ...] --no-focus     # only for the issue's first task (then tab rename)
herdr pane run <PANE> '<command>; echo "__RT_DONE_<n>__ rc=$?"'
herdr pane wait-output <PANE> --regex '__RT_DONE_<n>__ rc=[0-9]+' --timeout <ms>
herdr agent start <name> --kind codex --pane <PANE> [-- -m <model> -c model_reasoning_effort=<effort>]
b="$(cat /abs/brief.md)" && [ -n "$b" ] && herdr agent prompt <name> "$b" --wait --timeout <ms>
```

- All syntax-checked against 0.9.3.
- **Numbered sentinel.** `wait-output` also searches output already in the pane, so each command gets its own `__RT_DONE_<n>__`. The typed command line shows `rc=$?`, which doesn't match `rc=[0-9]+`.
- **Where the brief is read.** `$(cat …)` runs on the orchestrator for local and `--machine` targets, and on the target for SSH targets. Use an absolute `[A-Za-z0-9._/-]` path, and never send an empty prompt.
- **Model and effort** come from the project's routing guide. The router only renders them as native flags: codex `-m`/`-c model_reasoning_effort=`, claude `--model`/`--effort`. When a task names neither, the config's `worker_defaults` apply (Codex: `gpt-6.1-sol`, `high`), so a worker starts the same way on either machine rather than at that machine's own Codex default.
- **Long-running commands** in command mode (Metro, dev servers) get their own split pane and a `ready_regex`. In agent mode the worker runs them per the project's skill.
- **Talking to agents on the other machine.** An agent is reachable only if it runs inside a Herdr pane on its host. Claude desktop app sessions are invisible to Herdr, so resume them in a pane with `claude --resume <id>`. Give long-lived agents a Herdr name (`[a-z][a-z0-9_-]*`, no spaces, e.g. `orchestrator`) so both machines can address them by name instead of a pane ID.
- **IDs and names.** Read IDs from the JSON responses. Moving a tab between workspaces (by hand, in the UI) renumbers its tab and workspace IDs, so routing follows each task's worker by its **agent name** (unique per machine), and treats the recorded IDs as a fallback only. `agent start` never creates layout. Agent names must match `[a-z][a-z0-9_-]{0,31}` and be unique per server.

### Collect

```sh
herdr agent get <name>                                    # agent_status: idle|working|blocked|done|unknown
herdr agent read <name> --source recent-unwrapped --lines 200   # plain text, not JSON
herdr pane read <PANE> --source recent-unwrapped --lines 200
herdr agent wait <name> --until blocked --timeout <ms>    # only for a state-specific wait
```

- `blocked` means an approval or question dialog is open. Routed workers start with auto-approval (`worker_args`: Codex `--approve-for-me`, Claude `--permission-mode auto`), so a block is a real decision. **It goes to the orchestrator.** The orchestrator approves what the project's workflow needs inside the worktree and local dev caches, refuses anything beyond the grant, and escalates that to the human (skill §3). It listens through one background `route.py --events` stream (skill §3), not blocking `agent wait` calls, and doesn't poll the human.
- `unknown` doesn't prove completion.
- `timeout` or `agent_prompt_stalled` doesn't prove the prompt wasn't delivered. Inspect before you re-send.
- A connection failure doesn't prove a mutation wasn't applied. Inspect remote state before retrying.

### Never, as part of routing

- **Stopping or removing:** `herdr server stop`, `session stop|delete`, and `workspace|tab|pane close` or `worktree remove [--force]` on things you didn't create. The exception is `route.py --reap --apply` (§4), which closes the tabs of leftover idle agents in issue workspaces (finished, or 24 h without an open task) after re-checking each one live.
- **Profiles:** `machine add|remove|enable|disable|reconnect`.
- **Trust and updates:** `--trust-repository` without the user's say-so, and `herdr update`.
- **Attaching the TUI:** bare `herdr`, `agent attach`, `session attach`.
- **Taking the user's focus or agents:** `agent rename`, `pane release-agent`, `agent|workspace focus`.
- **Keystrokes:** `agent|pane send-keys` and `pane send-text` are allowed only to answer a `blocked` dialog, by the orchestrator within the task's grant (skill §3), or after the human approves anything beyond it. `route.py --answer` is the logged path; for a Codex question on a running worker, `--question` (opens it with `shift+left`) and `--reply` (text, then enter), never `esc`. A mid-run instruction to a *working* worker goes through `route.py --steer`, only to narrow scope or set a stop point, within the grant and never to widen it. It is logged; Codex 0.162 folds it into the running turn.

## 7. Disconnect vs reboot

| Event | What survives | Orchestrator action |
| --- | --- | --- |
| Client detach, SSH drop, `--machine` link drop | Server, panes, shells and agents keep running. Herdr reconnects automatically. | Rediscover with `agent list` and `agent read`, then continue. Don't re-send the prompt. |
| Server restart or host reboot | Layout and working directories come back. **Running processes do not.** Claude/Codex panes with a native session reference may auto-resume their conversation. Commands are not re-run. `/tmp` is wiped, so workers write reports to `$ROUTE_REPORT_DIR` (`~/.cache/agent-routing/reports/<task>`). | Routing detects it: preflight records the host's boot id at dispatch; a different one now marks the task INTERRUPTED in `--status` and fires `HOST_RESET` and `INTERRUPTED` events. Close it with `--outcome interrupted`; routing never frees its slot by itself. Inspect the worktree. A resumed agent hasn't finished its task. Re-dispatch only with human approval. |
| Desktop → Mac SSH auth lost (agent locked or socket gone) | Mac work keeps running. | Report blocked. The human unlocks the agent. Never store a socket path. |

## 8. Responsibilities

**Router** (`scripts/route.py`):

- validates the contract and picks a host (§2), or blocks with a reason
- enforces the heavy-task cap (queueing over it), port slots and collisions (§3); runs only the read-only preflight
- renders the Herdr plan and a short handoff note; always returns `execution_authorized: false`
- applies a forbidden-command **tripwire** to explicit `commands[]`. Examples: push, pull, reset, clean, checkout, switch, restore, stash, rebase, branch or worktree deletion, `tofu`/`terraform` apply/destroy/import/state, a fetch refspec writing a local branch, destructive `herdr`, PR/release create, deploy CLIs, `kubectl apply|delete`, `rm -rf`, `compose down -v`. It catches honest mistakes only. **Human review of every rendered command is the real control.** A worker following the project's workflow in agent mode is governed by the grant and the project, not by this regex.
- starts workers only with `--dispatch`: one tab (in its issue's workspace), the project's worktree helper, one agent and one brief per task. Otherwise it never launches workers, sends prompts or touches checkouts. Main checkouts are never modified.

**Orchestrator:** gets the grant; routes and starts work with the skill (`--dispatch`); **actively listens** to each worker until it reaches its stop point, through the background `--events` stream; answers blocked dialogs within the grant and escalates anything beyond it to the human; applies the project's parent checks; closes the task and drains the queue (`--close <id> --next --dispatch`); reports blockers instead of working around them.

**Worker:** works in its own worktree at its host, follows the project's `AGENTS.md` and skill up to the stop point, and reports host, work directory and head SHA alongside the project's own handoff.

