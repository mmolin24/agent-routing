---
name: agent-routing
description: Route an orchestrated task to the right computer (Ubuntu WSL2 desktop or Mac), start it through Herdr, and get back a link to listen in on the worker and its child Codex/Claude runs. Use when an orchestrator is about to hand an issue, readiness gate or verification run to a Codex or Claude worker, wants to know where earlier work is running, or needs to free capacity and start queued heavy work.
---

# Agent routing

The agent-routing repo decides **where** work runs and starts it there. It
doesn't define **how** the work is done: the project's own `AGENTS.md` and
skills do (in example-app: worktree helper, verification, PR shape, workpad, model
routing). Never copy those into a task.

Repo: the agent-routing checkout on this machine (`/home/dev/projects/agent-routing`
on the desktop; wherever it is cloned on the Mac). Read its `AGENTS.md` once
per session; this skill is the short path. Below, `route` means
`python3 <repo>/scripts/route.py`.

## 0. Before routing: both machines reachable

Run `route --status` first. If a machine shows as **unreachable**, nothing gets routed there. Authentication and signing depend on each host's configuration; cached credentials may be lost on restart:

- **SSH authentication.** If the target requires a key that the local SSH agent has not loaded, ask the human to unlock it in their own terminal, e.g. `ssh-add <SSH_KEY_PATH>`. Never ask for the passphrase or load keys yourself.
- **Commit signing.** If the repository requires GPG-signed commits, a locked signing agent can block a `draft-pr` worker. Ask the human to unlock it in their own terminal, e.g. `echo test | gpg --clearsign >/dev/null`. For this configuration, `echo test | gpg --batch --pinentry-mode error --clearsign >/dev/null` checks the cache without prompting. A configured Keychain signing helper may supply the passphrase separately.
- **Optional macOS authentication example.** If SSH keys are stored in the Keychain, the human can load the configured key with `ssh-add --apple-use-keychain <SSH_KEY_PATH>`.
- **Herdr profiles.** The desktop reaches the Mac through the Herdr profile `Mac`; the Mac reaches the desktop through `Desktop WSL`. `herdr machine status --json` shows whether each one is reachable. If a profile needs authentication, the human runs `herdr machine reconnect <label>`.
- **The Mac asleep, or off the network.** Nothing to fix from here; report it and wait.

**Route from one place.** The routing home (config `routing_home`, the desktop) holds the open-task list and the queue. Commands that change them (`--record`, `--dispatch`, `--close`, `--next`, `--dequeue`, `--answer`) only run there; elsewhere they refuse and print the command to run on the home. `--status`, `--watch` and dry runs work from any machine.

## 1. Route and start

The commands below use the synthetic issue `APP-1` and PR #1. Replace placeholders before running them.

```sh
route --issue APP-1 --requires web --grant draft-pr --dispatch
```

- `--requires`, by work type (AGENTS.md §2):
  - backend or integration work: `docker`
  - web or docs: `web`
  - both machines have `web` and `docker`, so the router spreads this work by live load and open tasks; use `linux` only when the work truly needs Linux (it pins the desktop)
  - iOS: `ios-simulator`
  - Android: `android-emulator`
  - example-infra: `opentofu`
  - Cross-platform work is two tasks: `--suffix web` and `--suffix native`.
- `--grant` is required. It's the human's authority, never wider than they gave you:
  - `draft-pr`: the project's workflow up to one draft PR
  - `local-only`: edit and test, no commits
  - `verify-only`: run verification, no edits
- Mark load honestly. `--heavy` is for readiness gates and full suites; `--compose` is for the project's Compose stack, which gets its own ports. Docker work is heavy unless you pass `--light`.
- Optional:
  - `--host desktop|mac` makes an explicit request; it is never moved to the other machine.
  - `--model` and `--effort` come from the project's routing guide.
  - `--commit` defaults to `origin/main` on the target. Fetch there first if it's stale; the router warns you.
    On the Mac, fetch over SSH: `ssh dev-mac 'cd ~/example-app && git fetch origin'`. Non-interactive Git authentication must be configured on the target host. If authentication fails, ask the human to renew it there; do not work around it with other credentials.
- **Pass task instructions** (for example a filled `~/orchestrator` template: worker, gate, fix round) with `--brief-file <file>`. They're appended to the handoff note under "Task instructions". The router still sends exactly one brief per worker.
- **Scope each task so it can finish.** Long runs come from open scope, not slow machines: workers are model-bound.
  - Name the deliverable and the stop with `--stop "<deliverable>; STOP when <condition>"`. Bound any verification matrix (which runs, how many passes).
  - Set `--timeout` to your estimate in minutes. The default 120 is not an estimate, and `OVERTIME` fires against it.
- **Don't run dependent issues in parallel.** If issue B builds on, or edits the same files as, an unmerged issue A, either:
  - hold B until A merges; or
  - stack it on purpose: fetch, then `--commit origin/feature/APP-A`, and say so in the instructions.

  Never tell a worker to merge a sibling's branch while it's still changing under review. Each merge couples the two PRs and causes conflict rounds.
- **Pass the issue title:** `--summary "<Linear title>"`. It's what the task row shows at a glance.
- **Naming is automatic:** `<issue>[-<suffix>]-<role>`, e.g. `app1-impl`, `app1-native-verify`, `app1-gate`. The role comes from the grant (`impl`, `verify`, `gate`, `review`); `--role` overrides it. Herdr names must be unique per machine, so agents a worker starts in new panes are named `<issue>-<role>` too (the handoff note says so). Views strip the issue prefix under its task and show subagents (◇) by their description.
- `--dispatch` implies preflight and recording. Without it you get a dry run.

**Heavy phases take turns.** Light workers are uncapped, but every brief tells them to wrap Docker, full suites, typechecks, web builds and smokes in `scripts/heavy`. Those runs share `max_heavy` slots per machine with gates. When you start an Opus review or another check that runs those things yourself, wrap them too. `--watch` shows `slots 2/3 · 1 waiting: …`.

## 2. Read the result

| Exit | Meaning | Do |
| --- | --- | --- |
| `0` | Started. The output has the **link**: host, workspace, pane, agent name, worktree, branch, verified HEAD. | Keep the task id; listen in (§3). |
| `4` | **Queued**: the host is at its heavy cap, or it's **hot** (live load, memory or Docker over its limits). First in, first out. | Nothing runs in the background. Start it later with `route --next --dispatch`. |
| `3` | **Blocked**: wrong host for the work, collision, preflight failure, full queue (backpressure), or a failed dispatch step. | Read the reason. Don't retry blindly. A failed dispatch leaves the task open with a partial link for inspection. |
| `2` | Invalid request. | Fix the flags or the task file. |

## 3. Listen in, and keep workers moving

**You watch every worker you started until it reaches its stop point, without blocking your own conversation.** Workers start with auto-approval (Codex `--approve-for-me`, Claude `--permission-mode auto`, from the config's `worker_args`). Anything that still needs a decision comes to **you**, not the human.

**Event loop: keep exactly one `route --events` consumer running, always.**

```sh
route --events --follow   # preferred: attach once to a line watcher (Claude Code's Monitor); each line is one event
route --events            # fallback: a background command that exits with a batch; start it again every time
```

- It watches every open task, re-prompted workers, the issue pipeline, CI and both machines, and prints **one line per thing that needs you**.
- **Prefer `--follow` under a line watcher.** It never exits on its own: it rides out failed snapshots (an `ERROR` line, then it backs off and retries) and has no timeout, so nothing depends on you remembering to restart it. Without an active consumer, finished tasks and blocked workers can go unnoticed.
- **Never pipe or truncate it** (`| tail`, `| head`): a reader that drops lines consumes those events for good. It remembers what it last reported (`runs/.events-state.json`), so it reports only changes, and a restart doesn't repeat them.
- **The one-shot fallback** exits after the first batch, or with `TIMEOUT` after 45 quiet minutes as a heartbeat. Whatever the exit, read the output and start it again straight away.
- **Exactly one instance.** Before starting one, check your own background tasks and `pgrep -f 'route.py --events'`, and never start a second. If your watcher re-arms, the previous process must have ended first; `ALREADY` means one is still running. Stop the old one through the watcher itself (Claude Code: `TaskStop` the old monitor), never with `pkill -f 'route.py --events'` inside the watcher's command: the pattern matches that command's own shell and kills it.
- **If nobody is reading,** `--status` prints `WARNING: EVENTS CONSUMER IDLE N MIN` (or `HAS NEVER RUN`), and the watch view shows a red banner, once tasks are open and no consumer has polled for 15 min.
- **After a session restart or a context compaction,** run `route --status` once before starting it: events from the gap may already be consumed.
- **Exit codes:**
  - `0`: events (one-shot only);
  - `3`: `TIMEOUT` (one-shot only);
  - `4`: `ERROR`: the state file can't be written, or (one-shot only) three failed snapshots in a row;
  - `5`: `ALREADY`: another instance holds the lock. Don't start another; the one running reports.
- **On a `4`, or any non-zero exit without events twice in a row,** tell the human and fall back to checking `route --status` until it's fixed.
- **Blips don't raise false alarms.** While a machine is unreachable, or GitHub or Linear doesn't answer, it keeps the last known values for that part and reports `UNREACHABLE`; nothing else fires twice when the data comes back.
- **Never block on `herdr agent wait` or `route --listen --wait-ms`.** A foreground wait keeps the human waiting until it returns.
- Between events you're idle. **That's the point: the human can talk to you at any time.** If events arrive while you're talking with the human, say so in one line and handle them, unless they ask you to hold.

**Per event. Handle `BLOCKED` first: that worker is frozen until you answer.**

| Event | What to do |
| --- | --- |
| `BLOCKED` | Read the dialog with `route --blocked`. **Approve** (`y`) what the project's workflow needs inside the task's worktree and local dev caches: network for package installs, Playwright browsers, test and build runs, edits inside the worktree. Use `p` ("don't ask again") only for read or test commands confined to the worktree. Anything touching git remotes, the network beyond package registries, or paths outside the worktree gets `y` once, if it's in the grant at all. **Refuse** (`esc`) and tell the human anything beyond the grant: commit, push or PR without `draft-pr`; deploys; staging or provider writes; edits outside the worktree; deleting things; credential or security changes. Answer with `route --answer <task_id> <key>` (logged in the link). A `QUESTION:` line is a Codex question on a worker that keeps running: **never `esc`** (it interrupts the run). Open it with `route --question <task_id>`, then answer within the grant with `route --reply <task_id> "<text>"` (logged). |
| `STOPPED` | Read `route --listen <task_id> --handoff` (its last reply and key facts) and the project's handoff. Check it against the issue, then go to §4. Idle isn't success. |
| `GONE` | The worker's pane disappeared: treat it as interrupted. Inspect the worktree and the link, record what you find, and ask the human. Never re-dispatch or re-prompt automatically (AGENTS.md §1.9, §7). |
| `STALE` | The result is for an older PR head, or a fix round is running. Re-review or re-gate the current head once it settles. |
| `FAIL` | A review asked for changes or a gate failed. Send a fix round (below) or decide it's not the PR's fault, and say why. |
| `NEED` / `UNKNOWN` | Run `route --status`. Record the missing step with `--mark`, answer the decision, or ask the human. |
| `OVERTIME` | A worker is still working past its task's `--timeout` (and again at twice it). Read `route --listen <task_id> --handoff`: if it's on scope and close, let it finish; if the scope has grown, ask the human whether to extend or stop it. Set `--timeout` per task when you route; the default (120) isn't an estimate. |
| `UNPUSHED` | A draft-pr worker's oldest unpushed commit is over 60 min old. Its grant asks for a checkpoint push (its real commits, or a `WIP (checkpoint): <issue> [skip ci] [skip deploy]` commit) at least that often, so work never sits only in the worktree. Remind it at its next stop; a checkpoint is never a PR. It counts commits only: dirty files with no unpushed commit don't trigger it, so check `git` in `--status` for long runs. |
| `STALE_BASE` | An open PR (routed or not) is stacked on a branch whose PR merged or closed. Restack it onto `main` (route a fix round at `--commit origin/feature/APP-N`, fetch first) or retarget it, before more work lands on it. Keep restack triggers here, not only in notes. Bases whose PR is older than the 80 most recent PRs aren't seen. |
| `DRAFT_IDLE` | A routed issue's draft PR hasn't changed in 24 h. Finish it, hand it off or ask the human whether to close it; don't leave it in "other open PRs". |
| `HOST_RESET` / `INTERRUPTED` | A machine restarted (its boot id changed): every worker, child and container on it died. Each open task dispatched before the restart gets one `INTERRUPTED` line with its dispatched SHA, and `--status` shows it as INTERRUPTED with its clock stopped. Inspect its worktree (unpushed work, the report in `$ROUTE_REPORT_DIR`), record what you find, then `--close <id> --outcome interrupted`. Re-dispatch only with the human's OK (AGENTS.md §1.9). Routing stops following an interrupted task (no more STOPPED, GONE or NO_WORKER for it), so don't let a resumed pane carry on under it: close it and route again. A WSL distro restart counts too (PID 1's start time is part of the boot id). Restart your `--events --follow` stream if it died with the machine. |
| `GPG_LOCKED` | If GPG signing is configured, the routing home's signing agent may lose its cached passphrase on restart. Signed commits, pushes and merges fail or hang on pinentry, and preflight blocks draft-pr. Ask the human to run, in their own terminal, `echo test \| gpg --clearsign >/dev/null`. A signing timeout can indicate a locked agent; inspect the output. `--status` also warns, and says `DOCKER DOWN` with the engine restart command for WSL. |
| (queue) | `--next --dispatch` starts each host's oldest queued task that fits. A task queued with no explicit `--host` (load-shared) starts on whichever capable host has room first; `--host` keeps it pinned. `--next --host X` drains X only. A gate queued with `--allow-behind` isn't blocked when main moves: preflight warns how far behind it is, and its brief stops on a `git merge-tree` conflict. |
| `NO_WORKER` | A task has been open 5+ min with no worker agent: never dispatched (`--next` without `--dispatch`), a failed dispatch, one still setting up after 15 min, or no agent on its host. Command-mode tasks and a worker already reported `GONE` never fire it. It holds its slot with nothing running. Inspect it (`route --listen <task>`, `--status`), then `--close` it and route it again with `--dispatch`. `--status` and the views warn too. |
| `STILL_STOPPED` | A task's worker stopped 30 min ago and the task is still open. Read it (`route --listen <task>`), then close it or start its next round. It fires once per stop. |
| `REAP` | An issue's leftover agents can go (see §5). Run `route --reap`, check the list, then `route --reap --apply`. |
| `QUIET` | An in-progress step hasn't changed for a while. Record its outcome, or mark it again if it's really still running. |
| `CI_FAIL` | Act only if the failure is real. Hosted CI may be advisory under the project's gate policy, and `[skip ci]` commits leave it stale. |
| `CI_PASS` | No action. |
| `MERGED` | Record the merge with `--mark … merge merged --sha`, deploy staging per your notes, then move Linear and mark it. |
| `PR_CLOSED` | Tell the human. Close any open task for it with `--outcome abandoned`. |
| `LINEAR` | Move the issue with the Linear MCP, then record it with `--mark … linear <state>`. |
| `HOT` | Start nothing there by hand until `COOL`. The router already queues work bound for it. |
| `COOL` / `REACHABLE` / `DRAIN` | Queued work may start: `route --next --dispatch`. |
| `UNREACHABLE` | §0: check reachability, and ask the human for any unlock. |
| `LIMIT` | Codex or Claude has used 90% of a plan window, or hit its limit (`USAGE` in `--watch`). Start no new work for that tool, except what's needed to finish what's in flight, until it resets, and tell the human. |
| `TIMEOUT` | Nothing happened. Start it again. |
| `ERROR` | A snapshot failed. One `ERROR` line alone doesn't end a run; three in a row exit with `4`. See above. |
| `ALREADY` | Another `--events` is running (exit 5). Don't start a second; wait for the one that's running. |

**Run reviews and long reads in the background too.**
- Start each Opus review (or any long research or check) as a **background** subagent, with `model: "opus"`.
- Name it issue-first (`APP-1 review: Opus security, PR #1`, see §4) and keep going; you're notified when it finishes.
- Have it return only the verdict, its findings and **the exact head SHA it reviewed**, and record the result with that SHA (`--mark … review <verdict> --sha <reviewed head>`). The PR head may have moved while it ran.
- Read long worker output in a subagent too, and bring back a summary.

If your own permission settings refuse `send-keys`, tell the human once. They can allow `Bash(herdr * agent send-keys *)` or `Bash(python3 */agent-routing/scripts/route.py --answer *)` (and `--question *`, `--reply *`, `--steer *`, `--reap *`).

### Reading state

`route --watch` is the live terminal view: each machine as a lane, tasks with their workers and child runs, spinners on active work, and blocked workers flagged. Run it in its own Herdr pane or terminal. `route --dashboard --every 10` keeps a visual map at `runs/dashboard.html`: one lane per machine, with orchestrators, tasks, workers and child runs glowing by state. Open it in a browser, on Windows via `\\wsl.localhost\Ubuntu\…`. Orchestrators are named in the config's `known_agents`.

```sh
route --listen <task_id> --handoff        # the worker's last reply + PRs, SHAs, tests, questions (cheap)
route --listen <task_id>                  # raw recent terminal output (long; use a subagent to summarise)
route --status                            # every open task, live, on both machines
```

- `--status` lists, for each task, its worker and any **children**: other agents in the task's Herdr tab (each issue has one workspace in Herdr, with a tab per task: `APP-1 › impl`, `› gate`), such as Codex or Claude runs the worker started. It also lists **untracked** agents that belong to no task. Work started outside this skill, such as `codex exec` in a shell, is invisible to routing. Route it instead.
- **Idle is not done.** Read the output and check the project's own handoff (PR, head SHA, workpad) before treating a task as finished.
- To answer a worker's question, use the link's `agent` name: `herdr agent prompt <name> "…"`, prefixed for the other machine as `--status`, `--listen` and the link print it (`herdr: …`). That prefix uses herdr's own profile label (`--machine Mac`, case-sensitive), not the routing host name `mac`. If the worker is `blocked` (an approval dialog, or a Codex question while it runs: `agent prompt` fails `agent_not_idle` then), handle it as a `BLOCKED` event (above).
- **Steering a working worker:** `route --steer <task_id> "<text>"` sends one instruction while it works, logged in its link. Codex folds it into the running turn (it isn't queued as a second task). Use it only to narrow scope or set a stop point, within the grant; never to widen the task. It refuses blocked workers (`BLOCKED` above), idle ones (send a fix round) and closed tasks. If it errors, read the worker before sending again: a timeout doesn't prove it wasn't delivered.

**Fix rounds go to the same worker** (`herdr agent prompt <worker> …`):
- Send one only when `herdr agent get <worker>` shows it idle and you've read its previous result. Never queue a second prompt.
- Make sure `--events` is running before you prompt, or the worker's next stop may pass unseen.
- The views keep showing that worker on its issue's row, whether or not its task is still open.

**Moving a task to the other machine** always goes through its branch, worktree to worktree:
1. Interrupt the worker. Then prompt it, or do it yourself in its worktree: commit everything on the task's own branch as `WIP (host move): APP-N [skip ci] [skip deploy]` and push that branch (no PR). The `draft-pr` grant allows exactly this one commit for a move.
2. Close the old task with `--outcome moved`, and route a new one with `--commit <that sha>` (fetch on the target first). It starts a fresh worktree at the WIP commit. Never copy files across or work in a main checkout.
3. The new worker re-verifies everything before it opens a PR or the work is reviewed: the WIP commit is unverified.

## 4. Keep the issue pipeline visible

**Name every subagent you start after its issue, issue key first:** `APP-1 review: Opus security, PR #1`, `APP-1 staging: manager settings`. Include the step word (`review`, `gate`, `staging`). The views put a running subagent on its issue's row (`review ⠋ … running in orchestrator`) only when its description names the issue (or its PR) and the step. Without them it shows up only under ORCHESTRATORS. The same goes for any pane or agent you start by hand: `app1-impl`, not a bare name.

The views show each issue's pipeline (`impl › review › gate › merge › deploy › staging`) across steps and machines. Routed steps and the PR's GitHub state are picked up automatically. **Record the steps routing can't see yourself, as they happen:**

```sh
route --mark APP-1 review running --note "Opus"                    # when the review starts
route --mark APP-1 review approved --sha "<REVIEWED_SHA>" --note "Opus"     # or changes_required
route --mark APP-1 merge merged --note "#1"
route --mark APP-1 deploy running --note "api+worker"               # then deployed (or n/a)
route --mark APP-1 deploy deployed --note "api+worker <DEPLOYED_SHA>"
route --mark APP-1 staging verified
route --mark APP-1 linear in-review                                 # mirror every Linear move
```

- **Always pass `--sha` for a review or gate result:** the commit it was for. The views compare it with the PR's current head on GitHub and show `↻ re-run` once the head moves past it (a fix round, a rebase, a merge from main). A result without a SHA can't go stale, so it can mislead.
- **Close a routed gate or review with its result:** `route --close app1-gate --outcome passed` (or `failed`, `approved`, `changes_required`). `--close` refuses those tasks without `--outcome`; a step closed with no result shows as `? no result`.
- **Keep in-progress steps moving.** A step recorded as in progress (`running`, `build-verified`, ...) that doesn't change for a while (deploy 45 min, review 1 h, gate 3 h, staging 24 h) is flagged `⚠ quiet`. Record the outcome, or record it again if it's genuinely still going.
- **Tracker.** Each project's profile supplies `pipeline`, `tracker` and `tracker_flow`; `tracker: none` omits the tracker line. Linear projects show status from, in order: Linear itself when a personal API key is in `~/.config/agent-routing/linear-api-key` on the routing home (mode 600, outside this repo; or `LINEAR_API_KEY`); your last `--mark … linear` with its age; otherwise the profile's implied status, labelled "from the pipeline". example-app maps active work to In Progress, merge to In Review, staging to Done. A lagging status shows `⚠ should be in-review`: make that move with the Linear MCP, then mark it.

An issue moves to the **COMPLETED** backlog (kept 14 days) once its profile's last pipeline step is done or its tracker says Done. For example-app that is `staging verified` or `linear done`; merged and deployed but unchecked stays in flight.

**What the step marks mean:** `✓` done · `✗` failed · `⠋` worker running · `⚑ decision` worker blocked on a dialog: answer it (§3) · `◌ stopped` worker idle: read its output, it hasn't necessarily finished · `↻ re-run` result is for an older PR head, or a fix round is in progress · `⧗ queued` waiting for a heavy slot · `?` no result recorded, or the machine is unreachable · `…` recorded as in progress (`⚠ quiet` if stale) · `– n/a`. Open PRs that routing never touched are listed on one line (`○ N other open PRs`), with failing CI flagged.

## 5. Finish

```sh
route --close <task_id> --outcome passed --next --dispatch   # record the result, free its slot, start the oldest queued task
route --queue                               # what's open and waiting per host
```

Close a task only after its result is recorded. Never re-send a prompt or
replay an interrupted mutating step automatically.

**Orphaned dev servers** show in `--status` as `ORPHAN` lines under their machine: an `expo start`, Metro, Vite or Next server whose starter is gone (its parent is launchd, init or systemd), with its age, port, memory and directory. Routing only lists them. Stopping one is the human's call, and only once no open task owns its directory: tell the human which ones look abandoned.

**Reap leftover agents** when a `REAP` event names an issue. `route --reap` lists idle or done agents in issue workspaces that can go: their issue is finished (PR merged or closed, the profile's last pipeline step done, or tracker Done, Canceled or Duplicate), or neither the issue nor the agent's own session has been active for 24 h. While any task on the issue is open or queued, its agents stay, so fix rounds keep their worker; a fix round also restarts the agent's 24 h clock. `route --reap --apply` (routing home only) re-checks each agent live and closes only its tab, and skips a tab that holds anything else (a dev server, a shell, a live agent). The worktree, branch and session file stay, so `codex resume <session>` (logged in `runs/reaped.jsonl`) brings it back. It never touches orchestrators or anything outside an issue's workspace.

## 5b. Report routing friction

Whenever routing makes you work around something, file one line for the routing
maintainer, a separate session that fixes, tests and ships agent-routing changes on
its own:
- a wrong host or brief, a missing slot or port, a misleading view, a missed or false
  event, a command that didn't fit;
- also anything you had to do by hand that routing should do.

```sh
route --feedback "gate had no port slot without --compose" --task app1-gate
```

It returns at once. You'll get a two-line note when it's fixed. Don't wait for it, and
keep working around the problem meanwhile.

## 6. Staging reviews (admin and end-user screens)

When explicitly authorized, route staging checks with the `staging-review` grant and use the configured synthetic staging test roles. The grant must cover signing in and creating test data:

```sh
route --issue APP-1 --requires web --grant staging-review --summary "Staging: settings page shows the new label" --dispatch
```

- **It runs in its own worktree**, `.worktrees/<issue>-staging`, detached at `--commit` (default `origin/main`: fetch first, and keep the default, since staging runs deployed main). It never reuses the issue's worktree, whose feature head can predate fixes to the session helper. Either machine works (`staging-login`). Confirm that the configured test role is available on the selected host.
- **The reviewer signs in with `scripts/credentials.sh [--repo NAME] <role> [--worktree DIR]`**, run from its worktree. The repo defaults to `default_repo`; `projects.<repo>.staging` configures roles, Keychain/pass names, session directory and helper command (`{pm}` uses this host's `repos.<repo>.pnpm_cmd`). Use the configured role and credential-store names; a project-specific environment variable may override the session directory. It prints a session-file path; the reviewer loads it as Playwright `storageState`. It never sees, reads or types a password, and never prints the session file. The same goes for your own in-process reviewer subagents: give them the grant's rules and the helper, never a password.
- **Allowed and forbidden actions are in the grant** (`config/hosts.example.json` → `grants.staging-review`): staging only, test roles only, test data through the UI; no account, user, password or settings changes, no provider exports, no production. Findings are reported as text; screenshots stay on the host.
- **Close with the result:** `route --close app1-staging --outcome verified` (or `failed`). `--close` requires an outcome for staging tasks. Then `--mark APP-1 staging verified`. Run `scripts/credentials.sh [--repo NAME] --clear` for that project when the review ends.
- **Deploys stay yours.** Keep deploy credentials in the orchestrator's configured deploy helper and never pass them to a worker.
