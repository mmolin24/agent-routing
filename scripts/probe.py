"""One round trip per machine: agents, pane tags, agent profiles and load.

Runs on the target machine (in-process locally, or piped over SSH together
with agent_profile.py and host_load.py, see route.probe_script()). Input is
JSON {"herdr": <herdr binary>, "workspaces": [...], "trees": [{"task", "path",
"branch"}]}; output is one JSON object. Python 3.8+ and the standard library only, since the far side may
fall back to its system Python.
"""

import json
import os
import re
import subprocess
import sys
import time

GIT_BUDGET_S, GIT_CMD_S = 10, 5   # all worktrees' git together; one git command


def herdr_json(herdr, *args):
    try:
        p = subprocess.run([herdr, *args], capture_output=True, text=True, timeout=20)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def probe(req, profiles, snapshot):
    herdr = os.path.expandvars(req.get("herdr") or "herdr")
    agents = herdr_json(herdr, "agent", "list").get("result", {}).get("agents")
    if agents is None:
        return {"error": "herdr agent list failed"}
    tokens = {}
    for ws in req.get("workspaces") or []:
        for pn in herdr_json(herdr, "pane", "list", "--workspace", ws).get("result", {}).get("panes", []):
            tokens[pn.get("pane_id")] = pn.get("tokens") or {}
    profs = profiles([{"kind": a.get("agent"), "cwd": a.get("cwd"),
                       "session": (a.get("agent_session") or {}).get("value")} for a in agents])
    out = {"agents": agents, "tokens": tokens, "profiles": profs, "load": snapshot(), "heavy": heavy_slots(),
           "usage": usage()}
    if req.get("trees"):
        deadline = time.monotonic() + GIT_BUDGET_S       # one budget for every tree: never the whole probe
        out["git"] = {t["task"]: tree_git(t["path"], t["branch"], deadline) for t in req["trees"]}
    return out


class GitError(Exception):
    pass


def git_lines(path, *args, deadline=None, absent_ok=False):
    """Read-only git in a worker's worktree: no optional locks (no index refresh
    writes), no fsmonitor, no lazy fetch. Raises GitError("timeout"/"git failed");
    with absent_ok, exit 1 (e.g. rev-parse --verify on a missing ref) returns None."""
    left = GIT_CMD_S if deadline is None else min(GIT_CMD_S, deadline - time.monotonic())
    if left <= 0:
        raise GitError("timeout")
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    try:
        p = subprocess.run(["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "-C", path, *args],
                           capture_output=True, text=True, timeout=left, env=env)
    except subprocess.TimeoutExpired:
        raise GitError("timeout")
    except OSError:
        raise GitError("git failed")
    if p.returncode == 1 and absent_ok:
        return None
    if p.returncode != 0:
        raise GitError("git failed")
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


TREE_ERROR = {"dirty": 0, "stash": 0, "unpushed": 0, "pushed": False}   # the Go reader's zero values


def tree_git(path, branch, deadline=None):
    """Work a worker hasn't pushed yet: {"dirty", "stash", "unpushed", "pushed", "oldest_unpushed"?}.
    Unpushed counts commits on HEAD that no origin ref has (as of the last fetch or
    push), so main merged or rebased in doesn't count. Stashes are shared by every
    worktree of a repo, so only those made on this branch count. Mirrors collector/gitstate.go."""
    if not os.path.isdir(path):
        return dict(TREE_ERROR, error="no worktree")
    try:                                 # any failure or timeout: an error, never false zeros
        dirty = git_lines(path, "status", "--porcelain", deadline=deadline)
        pushed = git_lines(path, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + branch,
                           deadline=deadline, absent_ok=True) is not None
        times = git_lines(path, "log", "--format=%ct", "HEAD", "--not", "--remotes=origin", deadline=deadline)
        stashes = git_lines(path, "stash", "list", "--format=%gs", deadline=deadline)
    except GitError as e:
        return dict(TREE_ERROR, error=str(e))
    own = re.compile(r"(?:WIP on|On) " + re.escape(branch) + ":")
    out = {"dirty": len(dirty), "stash": sum(1 for s in stashes if own.match(s)),
           "unpushed": len(times or []), "pushed": pushed}
    if times:
        out["oldest_unpushed"] = min(int(t) for t in times)
    return out


def heavy_slots():
    """Who holds this machine's heavy slots and who waits (scripts/heavy), oldest first.
    Entries whose process is gone are skipped: their lock died with them."""
    import glob
    d = os.path.expanduser(os.environ.get("RT_HEAVY_DIR", "~/.cache/agent-routing/heavy"))

    def runs(pattern):
        out = []
        for p in sorted(glob.glob(os.path.join(d, pattern))):
            try:
                with open(p) as f:
                    r = json.load(f)
                os.kill(int(r["pid"]), 0)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            out.append({k: r.get(k) for k in ("pid", "task", "cmd", "since")})
        return out
    holders = sorted(runs("slot-*.json"), key=lambda r: r.get("since") or 0)
    return {"holders": holders, "waiting": runs(os.path.join("waiting", "*.json"))}



def usage():
    """Codex's latest rate_limits (newest session files) and Claude's (the file its
    status line writes), as {"codex": {...}, "claude": {...}}. Mirrors collector/usage.go."""
    import calendar, glob, time
    out = {}
    home = os.path.expanduser("~")
    days = sorted(glob.glob(os.path.join(home, ".codex", "sessions", "*", "*", "*")), reverse=True)[:2]
    files = [f for d in days for f in glob.glob(os.path.join(d, "rollout-*.jsonl"))]
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:6]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 2000000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
                rl = ev["payload"]["rate_limits"]
            except (ValueError, KeyError, TypeError):
                continue
            if not rl:
                continue
            u = {"plan": rl.get("plan_type") or "", "exhausted": rl.get("rate_limit_reached_type") or ""}
            ts = ev.get("timestamp", "")
            try:
                main, _, frac = ts.rstrip("Z").partition(".")
                u["at"] = calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S")) + float("0." + (frac or "0"))
            except ValueError:
                u["at"] = 0
            for w in (rl.get("primary"), rl.get("secondary")):
                if not w:
                    continue
                win = {"used_percent": w.get("used_percent"), "resets_at": w.get("resets_at"),
                       "window_minutes": w.get("window_minutes")}
                u["five_hour" if 0 < (w.get("window_minutes") or 0) <= 600 else "week"] = win
            bal = (rl.get("credits") or {}).get("balance")
            if bal:
                try:
                    u["credits"] = str(int(round(float(bal))))
                except ValueError:
                    pass
            if "week" in u or "five_hour" in u:
                out["codex"] = {k: v for k, v in u.items() if v not in ("", None)}   # as the Go reader omits them
                break
        if "codex" in out:
            break
    try:
        with open(os.path.join(home, ".claude", "usage", "rate_limits.json")) as fh:
            c = json.load(fh)
        now, u = time.time(), {"at": c.get("captured_at") or 0}
        for src, dst in (("five_hour", "five_hour"), ("seven_day", "week")):
            w = (c.get("rate_limits") or {}).get(src)
            if w and (not w.get("resets_at") or w["resets_at"] > now):   # past its reset: says nothing now
                u[dst] = {"used_percent": w.get("used_percentage"), "resets_at": w.get("resets_at")}
        if "week" in u or "five_hour" in u:
            out["claude"] = u
    except (OSError, ValueError, AttributeError):
        pass
    return out


if __name__ == "__main__" and "PROBE_EMBEDDED" not in globals():
    import agent_profile, host_load   # noqa: E401  (local CLI use)
    print(json.dumps(probe(json.loads(sys.argv[1]), agent_profile.profiles, host_load.snapshot)))
