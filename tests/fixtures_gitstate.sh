#!/usr/bin/env bash
# Builds the worktree layout both tree_git readers are tested against (tests/worktree_git.py,
# collector/gitstate_test.go). Usage: fixtures_gitstate.sh <dir>
set -eu
d=$1; export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null     # no user hooks, signing or ignores
g() { git -c init.defaultBranch=main -c commit.gpgsign=false "$@"; }
g init -q --bare "$d/origin.git"
g clone -q "$d/origin.git" "$d/main" 2>/dev/null
cd "$d/main"
echo a > a; g add a; GIT_COMMITTER_DATE=@1000000000 g commit -qm base; g push -q origin main
base=$(git rev-parse HEAD)
g worktree add -q -b feature/APP-1 "$d/wt-pushed" "$base"
g worktree add -q -b feature/APP-2 "$d/wt-never" "$base"
cd "$d/wt-pushed"
echo 1 > f; g add f; GIT_COMMITTER_DATE=@1000000100 g commit -qm one; g push -q origin feature/APP-1
echo 2 >> f; g commit -qam two --date=@1000000200; GIT_COMMITTER_DATE=@1000000200 g commit -q --amend --no-edit
echo 3 >> f; g add f; GIT_COMMITTER_DATE=@1000000300 g commit -qm three
echo s > s; g add s; g stash -q                       # a stash on feature/APP-1
echo d1 > d1; echo d2 > d2                            # two untracked files
echo 4 >> f                                           # one modified file
cd "$d/wt-never"
echo x > x; g add x; g stash -q                       # a stash on feature/APP-2: not APP-1's
echo n > n; g add n; GIT_COMMITTER_DATE=@1000000400 g commit -qm never
cd "$d/main"                                          # main moves on; the never-pushed branch rebases onto it
echo m1 > m; g add m; g commit -qm m1; echo m2 >> m; g commit -qam m2; g push -q origin main
cd "$d/wt-never"
GIT_COMMITTER_DATE=@1000000500 g rebase -q origin/main
