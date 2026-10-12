#!/usr/bin/env bash
# Everything a routing change must pass before it merges (the maintainer runs this
# in its worktree). Read-only against live systems: no dispatch, no state writes.
set -u
cd "$(dirname "$0")/.."
fail=0
step() { printf '%-44s' "$1"; shift; if out=$("$@" 2>&1); then echo ok; else echo FAIL; echo "$out" | tail -20 | sed 's/^/    /'; fail=1; fi; }

step "python compiles"            python3 -m py_compile scripts/*.py scripts/heavy
step "far-side scripts are 3.8-safe" python3 -c "import ast,sys; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('scripts/probe.py','scripts/agent_profile.py','scripts/host_load.py','scripts/heavy','scripts/host_identity.py')]"
step "shell scripts parse"        bash -n scripts/preflight.sh scripts/credentials.sh scripts/claude-statusline.sh
step "config is valid JSON"       python3 -c "import json; json.load(open('config/hosts.example.json'))"
step "go vet + tests (collector)" sh -c 'cd collector && go vet ./... && go test ./...'
step "SSH probe bundle defines what it calls" python3 -B -c "import sys; sys.path.insert(0,'scripts'); import dispatch as D; ns={}; exec(D.probe_script().rsplit('print(', 1)[0], ns); [ns[f] for f in ('probe','usage','profiles','snapshot','tree_git','heavy_slots')]"
step "docker calls never autolaunch dbus"  python3 -B tests/no_dbus.py
step "memory pressure in the load grade"  python3 -B tests/pressure.py
step "events scenarios"           python3 -B tests/events_scenarios.py
step "stale base"                python3 -B tests/stale_base.py
step "orphan dev servers"        python3 -B tests/orphans.py
step "step from worker name"      python3 -B tests/task_step.py
step "--next: dry run, blocked head skipped" python3 -B tests/next_drain.py
step "project routing profiles"    python3 -B tests/project_profiles.py
step "generic example routes (no checkouts)" python3 -B tests/example_config.py
step "configured host identity and views" python3 -B tests/host_identity.py
step "example worktree helper parses" bash -n examples/issue-worktree.sh
step "multi-project views"         python3 -B tests/project_views.py
step "offline routing goldens (frozen fixture)" python3 -B tests/golden_routing.py
step "offline view goldens"     python3 -B tests/golden_views.py
step "host reset marks tasks interrupted" python3 -B tests/host_reset.py
step "heavy slots"                python3 -B tests/heavy_slots.py
step "question vs approval dialogs" python3 -B tests/question_dialog.py
step "herdr machine selector"     python3 -B tests/herdr_prefix.py
step "steer: working workers only"     python3 -B tests/steer.py
step "staging worktree setup (git-detached)" python3 -B tests/git_detached.py
step "project staging logins (fake credentials)" python3 -B tests/credentials.py
step "reap: leftover agents only"     python3 -B tests/reap.py
step "profiles by session id"      python3 -B tests/profile_by_session.py
step "worktree git state (Python)"   python3 -B tests/worktree_git.py
step "route.py --queue runs"      python3 -B scripts/route.py --queue
# The legacy route.py smoke runs use the frozen fixture and resolve commits in its checkout;
# without one (a fresh clone of this repo) they are skipped, not failed.
PROJECT="$(python3 -c "import sys; sys.path.insert(0, 'scripts'); import route as r; c=r.load_config('tests/fixtures/hosts.golden.json'); h=c['hosts'][r.default_linux_host(c)]; print(h['repos'][c.get('default_repo', next(iter(h['repos'])))]['path'])" 2>/dev/null)"
if [ -d "$PROJECT/.git" ]; then
step "route.py dry run renders"   sh -c 'python3 -B scripts/route.py --config tests/fixtures/hosts.golden.json --issue APP-9999 --requires web --grant draft-pr --summary selftest >/dev/null'
step "review grant routes (--issue)"  sh -c 'python3 -B scripts/route.py --config tests/fixtures/hosts.golden.json --issue APP-9999 --grant review --summary selftest | grep -q "status: *ROUTED"'
step "review brief: no gate, no DB"   sh -c 'python3 -B scripts/route.py --config tests/fixtures/hosts.golden.json --issue APP-9999 --grant review --role security --summary selftest --json | python3 -c "import json,sys; d=json.load(sys.stdin); n=d[\"handoff_note\"]; assert \"ready:check\" not in n and \"compose up\" not in n and \"dev database\" in n"'
step "staging-review: own detached worktree" sh -c 'python3 -B scripts/route.py --config tests/fixtures/hosts.golden.json --issue APP-9999 --grant staging-review --summary selftest --host mac --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | grep -q "scripts/detached-worktree.sh /Users/dev/example-app/.worktrees/APP-9999-staging "'
step "pane commands ignore cd aliases" sh -c 'out=$(python3 -B scripts/route.py --config tests/fixtures/hosts.golden.json --issue APP-9999 --requires web --grant draft-pr --summary selftest; python3 -B scripts/route.py --config tests/fixtures/hosts.golden.json examples/tasks/linux-unit-tests.json); echo "$out" | grep -q "builtin cd" && ! echo "$out" | sed "s/builtin cd //g" | grep -Eq "(^|[^[:alnum:]_])cd "'
else
  printf '%-44s%s\n' "route.py smoke runs" "skipped (no project checkout at ${PROJECT:-?})"
fi
step "status line handles input"  sh -c 'echo "{\"model\":{\"display_name\":\"x\"}}" | HOME=$(mktemp -d) scripts/claude-statusline.sh | grep -q "usage n/a"'
rm -rf scripts/__pycache__ tests/__pycache__
[ $fail = 0 ] && echo "SELFTEST PASS" || echo "SELFTEST FAIL"
exit $fail
