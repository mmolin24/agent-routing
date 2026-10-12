# Task and Result Contract (v1)

A task file says **where** work should run and **with what authority**. It
doesn't describe **how** the work is done: the project's `AGENTS.md` and skills
own that (in example-app: worktree helper, verification gates, commit and PR shape,
workpad, model routing, worker report). Don't copy those into the task.

Examples are in `examples/tasks/`. `route.py --record` writes real runs to the
git-ignored `runs/tasks/<task_id>/`.

## Task fields

| Field | Required | Meaning |
| --- | --- | --- |
| `contract_version` | yes | `1`. |
| `task_id` | yes | Unique, `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, e.g. `rt-app722-web`. Its issue key (`APP-722`) labels the issue's Herdr workspace, and the rest (`web`) labels the task's tab in it. Also the `runs/` directory name. |
| `summary` | yes | One line with the issue and the outcome. |
| `authorization` | yes | `scope`, `granted_by` (a human), and `allowed[]`/`forbidden[]` in plain words. Scopes: `read-only` (existing checkout, nothing mutating); `worktree-write` (own worktree, no commits or pushes); `project-workflow` (the worker follows the project's own issue workflow up to `stop_condition`; for example-app that can include its draft-PR flow). Merge, deploy, staging/provider writes and status transitions stay with the parent or human per the project. Routing never widens a grant. |
| `routing.requested_host` | yes (may be `null`) | `desktop`, `mac` or `null`. A non-null value is always honored. If that host can't do the work, the task is blocked, never moved. |
| `routing.allow_behind` | no | `true` lets a gate start behind `origin/main` (`--allow-behind`): preflight warns instead of blocking, and the brief tells the gate to stop on a `git merge-tree` conflict with main. |
| `routing.requires` | yes | Capability tags, defined per host in the config's `capabilities` (today: `web` and `docker` on both machines, `linux`,  `macos`, `ios`, `xcode`, `ios-simulator`, `android-emulator`, `opentofu`, and `staging-login` on both). A grant can add its own tags (`staging-review` adds `staging-login`). AGENTS.md §2 maps work types to tags. A tag no host has blocks the task. |
| `repository` | yes | `name` (a key in the host config's `repos`) and `remote`, the expected `origin` URL. |
| `project_commit` | yes | The exact 40-hex commit the work starts from, usually `origin/main` after a fetch. A branch name is rejected. |
| `worktree` | yes | `strategy`: `pnpm-worktree-issue` (the project's issue helper, `projects.<name>.worktree.kind=helper`: `issue`, `deps` = `link`/`install`/`none`, `env` = `link`/`none`), `git-detached` (`path` as for `git-worktree`, no branch: a detached worktree at `project_commit`, reused and moved to it on later runs; in a pnpm repo `node_modules` is linked from the main checkout when the lockfiles match, else installed; used by `staging-review`), `git-worktree` (`path` absolute, or relative to the repo's configured `worktree_root` on the chosen host (default: the repo itself, e.g. `.worktrees/APP-623-activation` in example-app, which ignores it); `branch`), or `existing-checkout` (`read-only` only; HEAD must equal `project_commit`). Optional `require_clean_base` (bool). |
| `worker` | yes | `kind` (Herdr agent kind, e.g. `codex`, `claude`) and `name` (`[a-z][a-z0-9_-]{0,31}`, unique among live agents on that host). Optional `model` and `effort`, taken from the project's routing guide; the router only renders them as native CLI flags. |
| `mode` | no | `agent` (default): one worker gets one brief and follows the project's workflow. `command`: plain commands run in a pane, with no agent. |
| `commands[]` | `command` mode only | `cwd` (`<WORKTREE>` or `<WORKTREE>/rel/path`), `run`, `mutating` (bool), optional `long_running` (bool) and `ready_regex`. A leading `pnpm` means the host's `pnpm_cmd`. In agent mode, an optional list of extra commands the human wants run; the project's skill still owns verification. |
| `resources` | yes | `timeout_minutes` (required, 1..1440); `class`: `heavy` (gates, full suites) or `light`; if omitted, a task that requires `docker` is heavy and anything else light. `runtime`: `none` (default) or `compose` (gets a port slot and is always heavy, AGENTS.md §3); optional `port_slot` to pin one; `ports[]` for any other host port the task binds (e.g. Metro `8081`); `compose_project`, `database`, `notes`. |
| `stop_condition` | yes | The stop point in the project's terms, e.g. "one draft PR targeting main; report PR URL, branch and head SHA" or "implementation plus local verification". |
| `evidence_required` | no | Only routing-specific extras. The project's handoff is the main evidence. |

The router adds `selected_host`, `routing_ref` and, for compose runtimes, `port_slot` and the slot's `ports` when it records a task.

### Open tasks, heavy cap and collisions

- `route.py --record` writes `runs/active/<task_id>.json`. Delete that file once the result is recorded.
- With `--active-dir` (implied by `--record`), routing blocks on:
  - no free port slot
  - the same worker name, a shared port, Compose project (case-insensitive) or database, or the same worktree path or branch, on that host
  - the same `task_id` already being open
  - an invalid active file
  - a missing directory (exit 2)
- **A heavy task over `max_heavy`, or with no free port slot, is queued, not blocked** (exit `4`), first in first out per host. A full queue (`max_queue`) refuses it (exit `3`). Drain with `--close <id> --next --dispatch` (without `--dispatch`, `--next` is a dry run). See AGENTS.md §3.
- Without `--active-dir`, the router warns that none of this was checked.

### Command-mode guardrails

These apply to `commands[]` (and only matter much in command mode). A `run` value is rejected when it:

- starts with `-` or ends with `\`
- contains control or bidi characters
- trips the forbidden-command tripwire (AGENTS.md §8; examples, not exhaustive, and not a security boundary)

For pnpm projects, the router also blocks `deps: link` combined with any
`pnpm install/add/remove/update`. Each command gets a numbered `__RT_DONE_<n>__ rc=` sentinel. Long-running commands get their own pane and wait for `ready_regex`.

## Projects

For setup and a complete mixed-project example, see [Adding a project](adding-a-project.md).

The top-level `projects` object is keyed by repository name, matching
`hosts.<host>.repos`. Hosts keep checkout paths, remote URLs, tool commands and
port slots; a project profile supplies:

- `issue_prefix`: 2–10 letters (`[A-Za-z]{2,10}`), normalized to uppercase when
  read, used to validate issue keys. `--issue OPS-3` selects
  the matching project unless `--repo` is given; a number uses the selected
  project's prefix (by default `default_repo`). Missing `default_repo` is an error
  when `--repo` is omitted. Duplicate prefixes require explicit `--repo`.
- `package_manager`: optional. `pnpm` enables the dependency-link guard and pin
  warning. The host command remains `repos.<name>.pnpm_cmd` (`pm_cmd` is also
  accepted). Without a package manager the brief omits that line.
- `worktree`: `kind` is `helper` or `git` (default). `dir` and `branch` are
  templates. A helper also supplies `create` and optional `check` commands;
  substitutions are `{pm}`, `{issue}`, `{commit}`, `{deps}` and `{env}`. Helper
  directories resolve against the checkout; git directories resolve against
  `worktree_root` or the checkout. Rendered directory templates must be non-empty
  relative paths of `[A-Za-z0-9._/-]`, with no `..` segment, and must not resolve
  to `.` or the repo root. Git tasks with a suffix append it to `{issue}`
  for both templates, preserving separate worker paths and branches. Built helper
  tasks retain the contract strategy name `pnpm-worktree-issue`.
- `brief.gate_example`, `brief.focused_example`: heavy command examples, using
  the same substitutions. `brief.env_notes` maps exported environment variable
  names to brief text, shown only when the variable is present.
  Brief and setup substitutions use the task's issue and its worktree's `deps`
  and `env` values (defaulting to `link` when omitted).
- `gate_runtime`: `none` (default) or `compose`; gates remain heavy in either
  case. `--compose` explicitly requests Compose.
- `staging.worktree_dir`: optional detached staging directory template. Without
  it, `{issue}-staging` sits beside the issue directory. The same directory
  validation applies. Formatting errors identify the project and template key.
- `pr.checkout`: optional checkout on the routing home for `gh pr list`. Without
  it there is no PR lookup or degradation warning. `ROUTE_PR_REPO` overrides it
  for a legacy single-project lookup on `default_repo`.
- `pipeline`: ordered list of at least two unique step names, starting with `impl`.
  Each is a single lowercase word matching `[a-z]{2,20}` (`linear` and `none` are reserved); defaults
  to `["impl", "review", "gate", "merge"]`. Rows and `--mark` resolve their project
  by the issue's recorded repository, then its prefix, else `default_repo`, and
  carry the project's pipeline. Completion
  means its last step is done, or its tracker says done.
- `tracker`: `linear` or `none` (default). Only Linear projects query Linear;
  `none` omits the tracker step. Credentials still come from `LINEAR_API_KEY`,
  else `~/.config/agent-routing/linear-api-key` on the routing home.
  `tracker_flow.active` is the status while work or a PR exists;
  `tracker_flow.after` maps completed pipeline steps to statuses (the latest
  step in pipeline order wins). `tracker_flow.rank` maps status names to integer
  ranks to flag lagging live or recorded statuses. The web-app example maps
  active work to `in-progress`, merge to `in-review`, staging to `done`.
- Staging logins also require `staging.roles`, `keychain_service`, `pass_entry`,
  `session_dir` and `session_cmd`. Add these fields to existing local configs;
  missing keys fail with a migration message, with no hard-coded defaults.
  `--clear` requires only `session_dir`. Credential names substitute `{role}`;
  `session_dir` expands a leading `~/`. `STAGING_SESSION_DIR` and the legacy
  `APP_STAGING_SESSION_DIR` override it with the same validation: absolute,
  normalized (no `.`/`..` segments), neither `/` nor HOME, outside the worktree,
  and, if present, a real directory owned by the user, never a symlink.
  Spaces in directory overrides are unsupported. The command is split with
  `shlex`, with `{pm}`, `{role}` and `{out}` substituted only as whole arguments;
  `{pm}` uses the current
  host's `repos.<name>.pnpm_cmd`. Literal `pnpm` stays literal; use `{pm}` to
  select the host's configured command. The first word must be `pnpm`, `{pm}`, or
  an executable relative path inside the worktree with no `..` segments.
  `projects.<repo>.staging.session_cmd` runs with the credentials on stdin;
  the config is trusted.
  This is trusted code like every configured command; the first-word check
  catches mistakes, rather than restricting what an allowed helper can do.
  Roles match `[a-z][a-z0-9-]{0,31}` and rendered names, paths and command
  arguments allow only `[A-Za-z0-9._/@:-]`. Template results and command first
  words cannot start with `-`; literal option arguments are allowed.
  Sessions are regular, non-symlink `<role>.json` files (mode 600) inside the
  directory (mode 700). Login removes old role state before reading credentials;
  failed or interrupted logins remove their output. `--clear` deletes only direct
  regular `*.json` files and reports failure if any matching entries remain.

Without a profile, issue validation accepts `[A-Za-z]{2,10}-[0-9]+`, git
worktrees use `{issue}` and `feature/{issue}`, and the heavy example is
`<your full test command>`. A legacy host `repos.<name>.issue_worktree` set to
`pnpm-worktree-issue` requires a `projects.<name>` profile, even if only one host
has the declaration. Tasks and active records using that strategy require a
helper profile with a `create` command. Missing profiles raise `Invalid` with
instructions to add one; the router supplies no legacy helper defaults.
A leading `pnpm` routing command still uses the host's `repos.<name>.pnpm_cmd`,
whether or not the repository has a project profile.

Recording stores `_occupies: {host, path, branch}` in the active record. Collision
checks prefer that resolved identity, so later profile edits cannot move an open
task's occupied directory or branch. Records without `_occupies` are resolved
from the current config.
`tracker`, `pr` and `pipeline` drive the issue views and step marks; they do not
run project checks or change the worker's authority.

## Result (routing part)

The project's own handoff is the result: for example-app, the workpad, the PR and its head SHA. The orchestrator adds only the routing facts to `runs/tasks/<task_id>/result.json` (see `examples/result.example.json`):

| Field | Meaning |
| --- | --- |
| `task_id`, `routing_ref` | From the decision. |
| `host`, `herdr_target` | Host, selector (`local`, `--machine "Desktop WSL"`, `ssh dev-mac`), workspace ID, pane ID, agent name. `--dispatch` writes these to `runs/tasks/<id>/link.json` from live JSON. |
| `work_dir`, `head_before`, `head_after` | Verified by the orchestrator, not just reported. |
| `status` | `succeeded`, `failed`, `blocked`, `timeout` or `interrupted`. |
| `project_handoff` | Pointer to the project's own evidence (PR URL, workpad link, evidence paths). |
| `blockers[]` | Anything that stopped the work and the decision a human must make. |
| `interrupted_mutation` | A mutating command cut off by a disconnect, reboot or timeout, plus what was observed afterwards. **Never replay it automatically.** |

## Exit codes

- `scripts/route.py`: `0` routed (or `--next --dispatch` started a task), `4` queued (or `--next` started nothing, including its dry run), `3` blocked (including a full queue), `2` invalid task or config.
- `scripts/preflight.sh`: `0` ok, `20` blocked, `2` usage error. Over SSH, `255` means unreachable.
- Herdr CLI: `1` server error (JSON on stderr), `2` CLI syntax error. A timeout or `agent_prompt_stalled` doesn't prove the prompt wasn't delivered.
