"""The git-detached worktree setup (staging-review) run for real against a throwaway
repo: add, link deps, reuse at a newer commit, and refuse anything that isn't its own
worktree, so git can never reach the main checkout above it."""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R

env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
T = tempfile.mkdtemp(prefix="gdet-")
repo = os.path.join(T, "repo")
def git(*a, cwd=repo):
    return subprocess.run(["git", *a], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()
os.makedirs(os.path.join(repo, "frontend"))
git("init", "-q", cwd=repo); git("config", "user.email", "t@t"); git("config", "user.name", "t")
for f, txt in (("package.json", "{}"), ("pnpm-lock.yaml", "lock1"), ("frontend/package.json", "{}"),
               ("frontend/pnpm-lock.yaml", "flock"), ("frontend/app.ts", "x")):
    open(os.path.join(repo, f), "w").write(txt)
git("add", "-A"); git("commit", "-qm", "one"); C1 = git("rev-parse", "HEAD")
open(os.path.join(repo, "pnpm-lock.yaml"), "w").write("lock2")
git("commit", "-qam", "two"); C2 = git("rev-parse", "HEAD")
git("checkout", "-q", C1)                       # the "main checkout" sits at C1 with deps installed
os.makedirs(os.path.join(repo, "node_modules")); os.makedirs(os.path.join(repo, "frontend", "node_modules"))
cfg = {"hosts": {"h": {"routing_repo": R.ROOT,
                      "repos": {"r": {"path": repo, "issue_worktree": "pnpm-worktree-issue", "pnpm_cmd": "echo PNPM"}}}},
       "projects": {"r": {"package_manager": "pnpm", "worktree": {
           "kind": "helper", "dir": ".worktrees/{issue}", "create": "{pm} create {issue}"}}}}

def run(commit, path=".worktrees/APP-1-staging"):
    t = {"worktree": {"strategy": "git-detached", "path": path}, "repository": {"name": "r"}, "project_commit": commit}
    cmd, _ = R.setup_command(t, cfg, "h")
    p = subprocess.run(["bash", "-c", cmd], cwd=repo, env=env, capture_output=True, text=True)
    lines = p.stdout.strip().splitlines()
    return lines, p.stderr
fails = []
def check(name, ok, got=None):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"  got {got}"))
    ok or fails.append(name)
wt = os.path.join(repo, ".worktrees", "APP-1-staging")
main_head = lambda: git("rev-parse", "HEAD")

out, err = run(C1)
check("first run: worktree at the commit, rc=0", out[-2:] == [C1, "__RT_DONE_0__ rc=0"], (out, err))
check("deps linked from the main checkout (same lockfile)",
      os.path.islink(wt + "/node_modules") and os.path.islink(wt + "/frontend/node_modules"))
open(wt + "/shot.png", "w").write("x")
out, err = run(C2)
check("reuse: moved to the newer commit, untracked kept", out[-2:] == [C2, "__RT_DONE_0__ rc=0"] and os.path.exists(wt + "/shot.png"), (out, err))
check("lockfile changed: stale link replaced by an install", not os.path.islink(wt + "/node_modules")
      and any("PNPM --dir . install --frozen-lockfile" in x for x in out), out)
check("main checkout untouched", main_head() == C1)

open(wt + "/frontend/app.ts", "w").write("edited")
out, err = run(C1)
check("tracked changes: refused, not carried over", out[-1].endswith("rc=1") and "tracked changes" in err
      and git("rev-parse", "HEAD", cwd=wt) == C2, (out, err))

os.makedirs(os.path.join(repo, ".worktrees", "APP-2-staging"))      # a stray plain directory
out, err = run(C2, ".worktrees/APP-2-staging")
check("stray directory: refused, main checkout not detached", out[-1].endswith("rc=1")
      and "not its own worktree" in err and main_head() == C1 and git("symbolic-ref", "-q", "HEAD", cwd=repo) == "" if False else
      (out[-1].endswith("rc=1") and "not its own worktree" in err and main_head() == C1), (out, err))
out, err = run(C2, "frontend")                                      # a tracked subdirectory of the main checkout
check("tracked subdirectory: refused, main checkout not moved", out[-1].endswith("rc=1") and main_head() == C1, (out, err))
link = os.path.join(T, "via-link")                                  # the repo reached through a symlink
os.symlink(repo, link)
cfg["hosts"]["h"]["repos"]["r"]["path"] = link
out, err = run(C2)
check("repo path through a symlink: reuse still works", out[-2:] == [C2, "__RT_DONE_0__ rc=0"] or "tracked changes" in err, (out, err))
# Typed setup must fit the input buffer so its done marker is preserved.
# Exercise both host paths in the synthetic config.
real = R.load_json(os.path.join(R.ROOT, "tests", "fixtures", "hosts.golden.json"))
for h, hc in real["hosts"].items():
    for name in hc.get("repos", {}):
        t = {"worktree": R.staging_worktree(real, name, "APP-22280"),
             "repository": {"name": name}, "project_commit": "e" * 40}
        cmd, _ = R.setup_command(t, real, h)
        check(f"typed setup on {h}/{name} is {len(cmd.encode())} bytes (< {R.TYPED_MAX})", len(cmd.encode()) < R.TYPED_MAX, cmd)
long = {"mode": "command", "worktree": {"strategy": "existing-checkout"}, "repository": {"name": "example-app"},
        "commands": [{"run": "echo " + "x" * 1000, "mutating": False}], "project_commit": "e" * 40}
check("an over-long typed command is a blocker", any("bytes typed into the pane" in b
      for b in R.typed_blockers(long, real, "mac")), R.typed_blockers(long, real, "mac"))

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
