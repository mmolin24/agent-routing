# Adding a project

Use `config/hosts.example.json` as a starting point. It has a pnpm web monorepo,
a native mobile repo and an OpenTofu repo sharing a Linux workstation and a
macOS laptop. Replace the placeholder paths, remotes and helper commands before
using it for real work. A project's own `AGENTS.md` and skills still define its
workflow and verification.

## The model

**Hosts are machines.** They describe installed capabilities, checkout paths,
how to reach the machine and how much work it can carry. **Projects describe how
a repo works:** worktrees, command examples, runtime, tracker and pipeline.
**Grants describe authority:** what a worker may do and where it must stop.
A configured grant is a preset, not permission by itself; the human must grant
that scope for the task.

Pick one `routing_home`. Its `runs/` holds all projects' open tasks, queues,
links and results. Record, dispatch and close there so separate orchestrators
see the same occupied capacity. Other machines can show views and plans.
There is no background scheduler: the orchestrator listens and drains queues.

Heavy slots are shared across **all projects on a machine**, both when routing
heavy tasks and when workers wrap heavy commands in `scripts/heavy`. Port
ranges are configured **per project runtime on each host**. A Compose task gets
exported ports and an image name in its own Herdr tab; its services must actually
read those variables. Slot numbers are reserved **host-wide across projects**:
a task using slot 1 prevents any other project on that host from using slot 1,
even with different port ranges. This conservative allocation keeps numeric
slots from colliding. Slot 0 belongs to the main checkout.

## 1. Describe the machines

Clone agent-routing on each machine and copy the example into the ignored local
config. Keep the host and project settings consistent across those copies.

```sh
cp config/hosts.example.json config/hosts.local.json
herdr --version
herdr status
herdr machine list --json
```

Set these fields before adding repos:

| Field | Example | What it changes |
| --- | --- | --- |
| `routing_home` | `mac` | The single owner of task state and queues. |
| `default_linux_host` | `linux` | Tie-breaker when several capable hosts have equal scores. |
| `hosts.<id>.os` | `linux` or `darwin` | Local identity fallback and view icon/colour (`macos` is an alias for `darwin`). |
| `hosts.<id>.match.hostname` | `builder-1` | Optional short hostname identifying this machine when several hosts share an OS. |
| `hosts.<id>.label`, `description` | `Build server`, `Ubuntu build machine` | Optional lane title and subtitle. |
| `hosts.<id>.capabilities` | `["linux", "web", "docker", "opentofu"]` | Tags a task can request with `--requires`. Tags describe usable tools, not planned installs. |
| `capability_tools` | `"opentofu": ["tofu"]` | Executables preflight checks for a required tag. Add mappings for your own tools. |
| `tools` | `["git"]` | Tools preflight checks for every task on that host. Keep platform-specific tools in capability mappings. |
| `herdr_bin`, `path_prepend` | `$HOME/.local/bin/herdr`, `["/opt/homebrew/bin"]` | Herdr and tool discovery in non-interactive shells. List your actual installation paths. |
| `reach` | `"linux": {"mode":"machine", "selector":"Mac Laptop", "ssh_alias":"mac-laptop"}` on `mac` | How the source host `linux` reaches target `mac`. Profile labels are case-sensitive and belong to the source's Herdr server. |
| `routing_repo` | `/Users/you/projects/agent-routing` | Where dispatch finds routing helpers on that target. |
| `max_heavy`, `max_queue` | `3`, `10` | Concurrent heavy tasks/phases and maximum waiting tasks on that machine. |
| `share` | `0.6` | Relative placement weight. Lower gives a laptop less shared work. |
| `hot_limits` | `load_per_core: 1.0`, `min_mem_avail_pct: 10`, `max_docker_pct: 90` | Thresholds that make heavy work wait or move to another capable host. |

For local reach use `{"mode":"local"}`. For a remote host use `machine` with a
saved Herdr selector and SSH alias, or `ssh` with an alias. Each target needs a
reach entry for every source that will orchestrate it. Configure and authenticate
those connections yourself; the router does not create profiles or change SSH
trust. Use a private network such as Tailscale or WireGuard rather than
exposing SSH publicly; a stable private address also keeps SSH aliases and Herdr
profiles working when a laptop changes networks.

Start with small heavy caps. Run a representative build or integration suite and
measure load per core, available memory, memory pressure and Docker's memory
limit. On Linux use `uptime`, `free -h` and `/proc/pressure/memory`; on macOS use
`uptime` and `memory_pressure`. `route.py --status` also reports load. Raise caps
only when concurrent heavy runs still finish reliably. Check the physical volume
holding VM/Docker disks too; disk free space is reported but does not yet grade
placement.

Host IDs are your names for machines (for example `build1`, `build2`, `laptop`);
`desktop` and `mac` have no special routing meaning. Use the same IDs in
`routing_home`, `default_linux_host`, reach keys, `--host` and `--from`.
`--host` selects a target; `--from` identifies the source for an explicit plan.
Neither changes which machine owns task state.

Automatic local identity uses, in order:

1. `ROUTE_HOST`, if set; it must name a configured host.
2. `hosts.<id>.match.hostname` matching this machine's short hostname (the part
   before the first dot).
3. The single host with `os` matching the platform (`linux` or `darwin`;
   `macos` is also accepted for existing configs).

If multiple hosts match, or none does, detection fails with the configured
options. Set `export ROUTE_HOST=build1` on that machine, or configure
`"match": {"hostname": "my-builder"}`. The heavy wrapper and staging helper use
the same identity rules. Set `default_linux_host` when more than one Linux host
can tie; a sole Linux host is the default when the field is omitted.

Views order the routing home first, then hosts in config order. Set optional
`label` for the lane title and `description` for its subtitle; icons and colours
follow `os`.

## 2. Check out the repo on each eligible host

For example, on Linux:

```sh
mkdir -p /home/you/projects
cd /home/you/projects
git clone https://github.com/your-org/web-app.git
```

On the Mac use `/Users/you/projects`. Under `hosts.<id>.repos.web-app`, set
`path` and the expected `remote` on each host that can run it. Set `pnpm_cmd` to
the repo's pinned version, for example `pnpm` or `npx -y pnpm@10.33.2`. For plain
git worktrees, `worktree_root: /home/you/worktrees/mobile` puts worker directories
outside the main checkout; otherwise they resolve under it.

Make every advertised capability usable with the configured PATH. Install the
repo's dependencies and prepare its local environment per its own docs. Ignore
`.worktrees/` if using directories inside the checkout. The router never fixes a
main checkout: dirty files and untracked files stay where they are. Fetch remote
refs before choosing a base; it does not fetch for you.

## 3. Write the project profile

Add `projects.<repo>`, with the same repo key used in every host's `repos`.
The example supplies all the following fields for `web-app`; `mobile` and
`infra` omit fields they do not need.

| Field | Example | What it changes |
| --- | --- | --- |
| `issue_prefix` | `APP` | `--issue APP-1` selects this project; `--issue 1` uses the selected/default project's prefix. Use 2–10 letters and a different prefix per repo. |
| `worktree.kind` | `helper` or `git` | Runs the repo's helper or renders plain `git worktree add`. |
| `worktree.dir`, `branch` | `.worktrees/{issue}`, `feature/{issue}` | Work directory and occupied branch. Helper dirs resolve under the checkout; git dirs under `worktree_root` or the checkout. |
| `worktree.create`, `check` | `bash scripts/issue-worktree.sh {issue} {commit} {deps} {env}` | Helper setup and optional check after entering the worktree. The helper must create the declared path and branch at the exact commit. |
| `package_manager` | `pnpm` | Adds the command to briefs, pin warnings and protection against installs through linked `node_modules`. Omit for repos without a package manager. |
| `brief.gate_example`, `focused_example` | `{pm} gate <PR>`, `{pm} typecheck` | Heavy-command examples for implementation/gate and review briefs; these fields do not execute checks. |
| `brief.env_notes` | `"POSTGRES_HOST_PORT": "Use $POSTGRES_HOST_PORT; never edit a shared .env."` | Adds instructions only when that variable is exported for the task. |
| `gate_runtime` | `compose` or `none` | Whether `--grant gate` needs a runtime slot. Gates are heavy either way; add `--requires docker` for a Compose gate. |
| `tracker`, `tracker_flow` | `linear` with `active: in-progress`, `after: {merge: in-review, staging: done}` | Live/recorded tracker status, implied status after steps, and lag warnings using the `rank` mapping. `none` omits the tracker line. |
| `pr.checkout` | `/Users/you/projects/web-app` | Checkout on the routing home where `gh pr list` reads PRs. Omit to disable lookup; `--mark` can record merge results manually. |
| `pipeline` | `["impl", "review", "gate", "merge", "deploy", "staging"]` | Ordered view steps and accepted marks. Completion is the last step done or tracker Done. This does not run a workflow. |
| `staging` | See below | Detached review directory, test roles, credential store names and browser-session helper. |

Worktree and brief templates accept `{pm}`, `{issue}`, `{commit}`, `{deps}` and
`{env}`. `{pm}` uses this host's repo command. Directories must be safe relative
paths, with no spaces or `..`, and cannot resolve to the main checkout.
Plain git tasks append `--suffix` to the issue in both path and branch; helper
tasks keep their helper's issue directory, so use separate hosts or a helper
profile designed for your required isolation.

For the example helper, copy this repo's small script into the web repo:

```sh
cp examples/issue-worktree.sh /Users/you/projects/web-app/scripts/issue-worktree.sh
```

It creates `.worktrees/APP-1` and `feature/APP-1`, supports dependency
`link`/`install`/`none` and environment `link`/`none`, and stops if a branch or
worktree already exists. Extend it per the project's rules to validate and reuse
existing worktrees for later review/gate tasks. With `deps=link`, never install
inside the worktree: that writes into the main checkout's `node_modules`. Use
`--deps install` for dependency changes. Match the helper's `pnpm` version to the
repo pin.

Configure Compose ports in **each host's repo entry**, not the project profile:

```json
"port_slots": {
  "base": {"API_HOST_PORT": 3000, "POSTGRES_HOST_PORT": 5432, "FRONTEND_HOST_PORT": 8081},
  "step": 10,
  "max_slot": 3,
  "extra_env": {"APP_RUNTIME_IMAGE": "web-app-runtime:test-{task_id}"}
}
```

Slot 1 exports `3010`, `5442`, `8091`; slot 2 adds another 10. Choose port ranges
that do not overlap other projects or manual servers, and make Compose use them.
Use unique task image tags and Compose project names too. Today the allocator
reserves the numeric slot across the whole host even when projects have different
ranges, so different runtimes can exhaust slots before exhausting heavy capacity.

Staging needs `worktree_dir`, `roles`, `keychain_service`, `pass_entry`,
`session_dir` and `session_cmd`. For example, `web-app` uses roles `admin`/`viewer`,
store names `web-app-staging-{role}` and `web-app/staging/{role}`, sessions in
`~/.cache/web-app-staging`, and
`{pm} --silent session:create --role {role} --out {out}`. These command
placeholders must occupy whole arguments. Provide that helper in your repo; it
reads credentials on stdin and writes browser storage state. Store credentials
in this machine's Keychain or `pass`, never in the routing config. Reviews use
`scripts/credentials.sh --repo web-app admin --worktree <DIR>` and finish with
`scripts/credentials.sh --repo web-app --clear`.

## 4. Pick grants

Set `operator` to the human granting authority, then review each preset against
that human's instructions and the project's rules:

- `local-only`: edit and test in the task's worktree; no commits.
- `verify-only`: run platform verification; no tracked-file edits or commits.
- `draft-pr`: commit and push the task's own branch and open one checked draft PR.
- `review`: inspect a diff and run focused checks; no edits or database work.
- `gate`: run the readiness gate once and report the exact checked head.
- `staging-review`: use configured test roles on staging within the explicit grant.

Keep `scope`, `allowed`, `forbidden` and `stop_condition` concrete. A grant may
add `requires`, as staging review adds `staging-login`. Use `--stop` and a task
brief to narrow work to a deliverable; neither may widen the grant.

## 5. Validate a plan and preflight

Run on the routing home (the Mac in the example), after replacing placeholders.
The prefix selects the project; `--repo` makes selection explicit.

```sh
git -C /Users/you/projects/web-app fetch origin
python3 scripts/route.py --issue APP-1 --requires web --grant local-only
python3 scripts/route.py --issue MOB-2 --requires ios-simulator --grant verify-only
python3 scripts/route.py --repo infra --issue OPS-3 --requires opentofu --grant local-only
python3 scripts/route.py --issue APP-1 --requires web --grant local-only --run-preflight
```

Fetch on the selected target too before resolving `origin/main`. A ref name is
resolved there as of its last fetch; dispatch verifies the exact commit.
Preflight checks reachability, tools, Herdr, origin, dirty state, commit
availability, signing when needed, and free runtime ports. It is read-only and
never repairs blockers. Inspect every rendered command.

For a config-only check in a fresh clone, with no project checkout:

```sh
python3 -B tests/example_config.py
python3 scripts/route.py --config config/hosts.example.json --from mac \
  --issue APP-1 --requires web --grant draft-pr \
  --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The test uses an exact synthetic SHA to skip ref resolution, routes all three
projects plus the Compose gate, and forbids live calls. The second command is
an unverified plan only; that synthetic commit is not a dispatch base. Without
an active directory, plans do not inspect occupied capacity or collisions.

## 6. First dispatch, listen and close

With the human's grant, run from the routing home:

```sh
python3 scripts/route.py --issue APP-1 --requires web --grant local-only \
  --summary "APP-1: add the settings page" --timeout 60 --dispatch
python3 scripts/route.py --events --follow
```

Dispatch implies preflight and recording. It opens the task's tab in its issue
workspace, prepares the worktree, verifies HEAD and starts one worker with one
brief. Save the returned task id (`app1-impl`) and link. A failed step leaves its
partial link for inspection; never replay it automatically.

Keep exactly one event consumer running under a background line watcher or in a
separate terminal. Read stopped or blocked workers, answer only within the grant,
and apply the project's parent verification. An idle worker has not necessarily
succeeded.

```sh
python3 scripts/route.py --listen app1-impl --handoff
python3 scripts/route.py --status
python3 scripts/route.py --close app1-impl --outcome done --next --dispatch
python3 scripts/route.py --queue
```

Record the project's handoff and routing facts in its `result.json` before
closing. Closing frees its occupied slots; `--next --dispatch` starts queued
work that now fits. For a later review/gate result use `--outcome approved` or
`passed` (or the failure result), and mark the exact reviewed SHA, for example:

```sh
python3 scripts/route.py --mark APP-1 review approved --sha <40-hex-reviewed-head>
```

## Mixing projects

Load sharing uses **all open tasks** on each capable host, weighted by `share`.
Heavy placement also avoids hot hosts, full heavy caps and waiting queues when
another capable host has room. A mobile simulator task stays on the Mac; infra
checks stay on the example's only OpenTofu host. Neither project gets a private
heavy quota. Light workers can start freely, but their builds and full suites
must use the shared heavy wrapper.

Use distinct issue prefixes. Config loading rejects two projects with the same
prefix, case-insensitively, even when `--repo` is explicit. Shared issue keys
would make views and marks ambiguous.

PR lookups and tracker queries are per project. If one repo's GitHub lookup or
Linear status fails, that project shows degraded/unknown data; other projects
keep their own results. `tracker: none` makes no tracker query. Credentials stay
outside the repo; optional Linear read access comes from `LINEAR_API_KEY` or
`~/.config/agent-routing/linear-api-key` on the routing home.

## What the router never does

A placement decision never authorizes execution. Without `--dispatch`, it does
not start workers; a plain dry run does not record task state. The router never
pulls, resets or cleans main checkouts, deploys, applies infrastructure, pushes,
opens or merges PRs, changes tracker status, or changes credentials and trust.
Those actions remain with the human or a worker's explicit grant and project
rules. It never silently changes an explicitly requested host or retries an
interrupted mutation. The narrow cleanup exception is `--reap --apply`: it
rechecks and closes eligible leftover idle issue tabs, preserving worktrees and
sessions (AGENTS.md §4).
