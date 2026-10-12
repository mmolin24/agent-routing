# agent-routing

> **Note:** This is a public reflection of the private version I use every day, not the
> copy I run. It mirrors that version's design, safeguards and tests, with project names,
> paths, issue numbers and machine details replaced by generic examples. It's updated
> from the private version periodically, so it may lag behind it.

Runs a fleet of AI coding agents for several projects on shared machines, in my
case a Windows desktop (Ubuntu on WSL2) and a Mac. A Claude orchestrator plans the
work. This repo decides **where** each task runs, checks that the machine can do it,
keeps parallel agents from colliding, starts them, and tells the orchestrator when
they need it.

**Why it exists:** hosted CI minutes were becoming a real cost. The turning point was
moving the full readiness gate (test suites, Compose stacks, browser smokes) off hosted
runners and onto hardware I already own, the desktop and the Mac. Splitting that
verification and the agents' work across both machines, without them colliding or
overloading either one, is what this router does. Hosted CI stays as an advisory signal.

Machines and projects are configuration. Each host lists its capabilities and limits;
each project has a profile that says how it works (issue keys, worktree helper, test
and gate commands, runtime ports, tracker, PR checks, pipeline). Every project on a
machine shares its heavy slots. I built it to run day-to-day development on my own
products; the examples use generic projects (`web-app`, `mobile`, `infra`) and
placeholder issues (`APP-…`). To set it up for your own projects, follow
[Adding a project](docs/adding-a-project.md).

```
┌──────────────────────────────┐                        ┌──────────────────────────────┐
│ Claude orchestrator (Opus)   │ ─────────────────────▶ │ routing maintainer (Claude)  │
│ plans issues, holds grants   │  route.py --feedback   │ audits each report, fixes it │
└──────────────┬───────────────┘                        │ in a worktree, gets a Codex  │
               │ route.py --issue APP-123               │ review, ships a new version  │
               │   --grant draft-pr --dispatch          └──────────────┬───────────────┘
┌──────────────▼───────────────┐                                       │
│ router (Python)              │ ◀─────────────────────────────────────┘
│ preflight (POSIX sh)         │  capabilities · live load · heavy slots
└──────┬────────────────┬──────┘  FIFO queue · port slots · collisions
  Herdr│                │ Herdr over SSH (Tailscale)
┌──────▼────────┐  ┌────▼──────────┐
│ desktop (WSL2)│  │ Mac           │   one git worktree per task,
│ Codex / Claude│  │ Codex / Claude│   pinned to an exact commit
│ workers       │  │ workers: iOS, │
│               │  │ Android, infra│
└──────┬────────┘  └────┬──────────┘
       └────────────┬───┘
┌───────────────────▼──────────┐
│ agent-collect (Go)           │  both machines' state in one SSH round trip
│ events / watch / dashboard   │  BLOCKED · STOPPED · HOST_RESET · CI · queues
└──────────────────────────────┘
```

## What it does

- **Places work** on the machine that can do it and is least loaded (CPU, memory pressure, Docker).
- **Throttles heavy phases** (test suites, builds, Docker) with per-machine slots, not the agents themselves.
- **Isolates agents:** one git worktree per task at a pinned commit, plus its own ports for Compose stacks.
- **Starts agents** (OpenAI Codex or Claude Code) through [Herdr](https://herdr.dev), one brief each.
- **Talks to any running worker** on any machine: read, steer, or answer its questions, all logged.
- **Lets workers fan out:** each worker can run its own subagents, nested several levels deep, shown as a tree under its task.
- **Streams what needs attention:** blocked or stopped workers, failing CI, hot machines, restarts.
- **Mixes projects** on the same hardware, each with its own profile.
- **Shows it all** in a terminal view and an HTML map.

Machines reach each other over SSH; in my setup that runs over a private
[Tailscale](https://tailscale.com) network, so SSH is never exposed to the internet.
Any SSH path works, a LAN included.

## Guardrails

Routing decides placement. It never grants authority. Every task carries a human
grant (`draft-pr`, `review`, `gate`, `verify-only`, …) that says what the agent may do,
and the router renders it into the brief. Routing itself never deploys, pushes or
stops agents, never replays an interrupted command, and never mutates a main
checkout. The one exception is `--reap --apply`, which closes leftover idle agents'
tabs once their issue is finished. A tripwire refuses obviously destructive commands
in task files, and no credential is stored in the repo. See `AGENTS.md` §1.

## How it's built

| Part | What |
| --- | --- |
| `scripts/route.py` | The router: validates a task, picks a host, enforces caps, queues, renders the Herdr plan and the brief; `--dispatch` starts it. Python 3.8+, standard library only. |
| `scripts/dispatch.py` | Dispatch, listen and live status through Herdr, locally or over SSH. |
| `scripts/events.py` | The event stream, with hysteresis and carry-forward so a blip on one machine isn't news. |
| `collector/` | `agent-collect` (Go): probes both machines in parallel and emits the same JSON as the Python readers, which stay as a fallback. [Python vs Go timings](collector/README.md#python-and-go). |
| `scripts/preflight.sh` | A read-only check on the target: tools, Herdr, repo identity, commit, ports, commit signing. |
| `skills/agent-routing/` | The orchestrator's skill: route, start, listen, close. |
| `scripts/credentials.sh` | Signs a reviewer into a staging app from credentials stored on the machine (Keychain or `pass`); the password goes to the project's helper on stdin only, never to the agent. |
| `tests/`, `scripts/selftest.sh` | Offline routing and view snapshots, multi-project profiles, event scenarios, the queue, heavy slots, host resets, and Go tests. |

The repo also maintains itself (top right of the diagram). Orchestrators file friction
with `route.py --feedback`, and a long-lived Claude maintainer session audits each
report, fixes it in a worktree, gets an independent Codex review, and ships the new
router, inside guardrails it can't change on its own: grants, credentials, the hard
rules and capacity limits wait for the human. `docs/maintainer.md` is its standing brief.
See [How it evolved](docs/how-it-evolved.md) for what that loop changed.

## The lifecycle

```sh
route.py --issue APP-123 --requires web --grant draft-pr --run-preflight --record   # decide, check the target
route.py --issue APP-123 --requires web --grant draft-pr --dispatch                 # start it, get its link
route.py --events --follow          # one line per thing that needs the orchestrator
route.py --listen app123-impl       # read the worker, on whichever machine it runs
route.py --steer app123-impl "stop after the migration; report before touching the UI"
route.py --close app123-impl --outcome done --next --dispatch   # free its slot, start the next queued task
```

There is no daemon. The orchestrator consumes the events and drains the queue.

## Try it (dry run)

```sh
python3 tests/example_config.py   # the example config routes a task per project, offline
sh scripts/selftest.sh             # every check, including offline routing and view snapshots
cp config/hosts.example.json config/hosts.local.json   # then follow docs/adding-a-project.md
```

Needs Python 3.10+ (standard library only), POSIX `sh`, and Go for the optional
collector and its tests. Real dispatch also needs [Herdr](https://herdr.dev) on each
machine and a project checkout. Without one, the self-test skips the smoke runs that
resolve commits.

Exit codes: `0` routed, `4` queued, `3` blocked, `2` invalid. `docs/adding-a-project.md`
walks through machines, project profiles and a first dispatch. `AGENTS.md` is the
operating guide for one real two-machine deployment: rules, host selection, capacity,
lifecycle, and the Herdr command reference. `docs/task-contract.md` defines a task and
the config, including the `projects` profiles. [How it evolved](docs/how-it-evolved.md)
traces the changes that came from the maintainer's feedback loop.

