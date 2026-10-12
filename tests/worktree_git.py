"""probe.tree_git on a fixture worktree layout (tests/fixtures_gitstate.sh); the Go
reader is tested against the same layout and expectations (collector/gitstate_test.go)."""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import probe as P

d = tempfile.mkdtemp(prefix="wtgit-")
subprocess.run([os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures_gitstate.sh"), d],
               capture_output=True, text=True, check=True)
fails = []
def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}: {got}")
    ok or fails.append(name)

check("pushed branch: 2 unpushed past origin, own stash only, 3 dirty",
      P.tree_git(os.path.join(d, "wt-pushed"), "feature/APP-1"),
      {"dirty": 3, "stash": 1, "unpushed": 2, "pushed": True, "oldest_unpushed": 1000000200})
check("never-pushed branch rebased onto a newer main: only its own commit",
      P.tree_git(os.path.join(d, "wt-never"), "feature/APP-2"),
      {"dirty": 0, "stash": 1, "unpushed": 1, "pushed": False, "oldest_unpushed": 1000000500})
check("missing worktree", P.tree_git(os.path.join(d, "gone"), "feature/APP-3"),
      {"dirty": 0, "stash": 0, "unpushed": 0, "pushed": False, "error": "no worktree"})
check("budget spent: timeout, no git run", P.tree_git(os.path.join(d, "wt-pushed"), "feature/APP-1", P.time.monotonic() - 1),
      {"dirty": 0, "stash": 0, "unpushed": 0, "pushed": False, "error": "timeout"})
real_run, calls = P.subprocess.run, []
def slow_after_status(*a, **kw):          # status answers, then the clock runs out
    calls.append(a[0][5])
    if len(calls) > 1:
        raise P.subprocess.TimeoutExpired(a[0], 5)
    return real_run(*a, **kw)
P.subprocess.run = slow_after_status
got = P.tree_git(os.path.join(d, "wt-pushed"), "feature/APP-1")
P.subprocess.run = real_run
check("timeout after status: an error, not false zeros", got,
      {"dirty": 0, "stash": 0, "unpushed": 0, "pushed": False, "error": "timeout"})
idx = os.path.join(d, "main", ".git", "worktrees", "wt-pushed", "index")
before = os.path.getmtime(idx)
P.tree_git(os.path.join(d, "wt-pushed"), "feature/APP-1")
check("read-only: the worktree's index isn't rewritten", os.path.getmtime(idx) == before, True)
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
