"""Event stream for the orchestrator: `route.py --events`.

Instead of blocking on `herdr agent wait` per worker, the orchestrator runs one
background command that watches every open task, the issue pipeline and both
machines, and prints a line only when something needs it. Synthetic examples:

  2000-01-01T00:00:00Z BLOCKED  app1-impl on desktop: answer it (route --blocked)
  2000-01-01T00:01:00Z STOPPED  app1-gate on desktop: read its output (route --listen app1-gate)
  2000-01-01T00:02:00Z CI_FAIL  APP-1 #1: 1 failing check
  2000-01-01T02:00:00Z OVERTIME app1-impl on desktop: working 120 min against a 120 min timeout
  2000-01-01T02:01:00Z UNPUSHED app1-impl on desktop: 1 commit(s) unpushed for 61 min (1 dirty files)

By default it waits for the first batch of events, prints it and exits, so a
Claude Code background command wakes the orchestrator; it handles them and
starts the command again. `--follow` keeps streaming instead.

State. The last state seen is kept in runs/.events-state.json, so a restart
reports only changes. When part of a snapshot is degraded (a machine
unreachable, GitHub or Linear not answering), the previous values for that part
are carried forward, so a blip neither raises false alarms nor re-reports old
news when the data comes back. Only one instance runs per state file (a lock);
a second exits at once with exit 5.

It never prompts, answers, closes or starts anything. It writes only its state
file, its lock, and the routing caches (PR and Linear lookups).
"""

import json
import os
import sys
import time

import reap as RP
import route as R

STATE_FILE = os.environ.get("ROUTE_EVENTS_STATE") or os.path.join(R.ROOT, "runs", ".events-state.json")
ATTENTION = {"stale", "fail", "need", "unknown"}
STOPPED_STATES = {"idle", "done"}
CONSUMER_IDLE_MIN = 15  # no --events poll for this long while tasks are open: --status and the views warn
STILL_STOPPED_MIN = 30  # a task's worker stopped this long and the task still open: remind once
MAX_BACKOFF_S = 300     # --follow never exits on failed snapshots; it backs off up to this
STATE_VERSION_KEY, STATE_VERSION = "_version", 3   # bump when keys change, so old state reads as a first run
SETTLE_POLLS = 2      # a machine must read hot (or not hot) this many polls in a row before HOT/COOL
MAX_ERRORS = 3        # consecutive failed snapshots before exiting with ERROR (exit 4)
OVER_RANK = {"over": 1, "over2": 2}   # OVERTIME fires at the timeout and again at twice it
CHECKPOINT_MIN = 60   # the draft-pr grant's checkpoint push cadence (config/hosts.example.json)
DRAFT_IDLE_H = 24     # a routed issue's draft PR untouched this long is a forgotten follow-up


def iso_ts(text):
    """Seconds since the epoch from a GitHub ISO timestamp; now when unreadable."""
    import calendar
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return time.time()


def degraded_parts(report, hosts):
    """What this snapshot can't be trusted for: {"hosts": {...}, "github", "linear", "all"}."""
    out = {"hosts": set(), "projects": {**report.get("issue_projects", {}),
                                         **{r["issue"]: r.get("project") for r in report.get("issues", [])}},
           "prefixes": report.get("project_prefixes", {}), "default_project": report.get("default_project")}
    for u in report.get("unreachable", []):
        head = u.split(":", 1)[0].strip()
        if head in hosts:
            out["hosts"].add(head)
        elif u.startswith("routing home"):
            out["all"] = True          # read away from the home: its records are missing
        elif u.startswith("agent-collect failed"):
            pass                       # the Python readers stood in: the data is complete
        elif u.startswith("issue view failed"):
            out["github"] = True       # no issue rows this time: keep the last ones
        else:
            out["other"] = True
    for part in report.get("degraded", []):
        out[part] = True
    return out


def data_bad(bad, part, key):
    """A failed lookup freezes only that project's observations."""
    issue = key.split(":")[1]
    project = bad.get("projects", {}).get(issue)
    if not project:
        project = (issue.split("#", 1)[0] if "#" in issue and not issue.startswith("#") else
                   bad.get("prefixes", {}).get(issue.split("-", 1)[0], bad.get("default_project")))
    return bool(bad.get(part) or bad.get(f"{part}:{project}"))


def observe(snap, cfg=None, orch=None):
    """Flatten a snapshot into ({key: value}, info, degraded)."""
    report, queues, cap = snap
    obs, info = {}, {}
    hosts = set(cap) | {t.get("host") for t in report.get("tasks", [])}
    bad = degraded_parts(report, hosts)
    # Keys never collide across kinds or machines: an open task is keyed by its id,
    # any other agent by machine and pane. (A finished agent from an earlier task can
    # share a task's name, e.g. after a host move; keyed by name, its constant "done"
    # would hide the real worker's transitions.)
    for h, ld in (report.get("loads") or {}).items():   # a new boot time: everything on it died
        if (ld or {}).get("boot_id"):
            obs[f"boot:{h}"] = ld["boot_id"]
            info[f"boot:{h}"] = {"host": h, "boot_at": ld.get("boot_at") or 0,
                                 "tasks": [t["task_id"] for t in report.get("tasks", [])
                                           if t["host"] == h and t.get("interrupted")]}
    for t in report.get("tasks", []):
        w = next((a for a in t.get("agents", []) if a.get("role") == "worker"), None)
        key = f"task:{t['task_id']}"
        obs[key] = (w or {}).get("status") or "missing"
        info[key] = {"name": (w or {}).get("name") or t["task_id"], "task": t["task_id"], "host": t["host"]}
        intr = t.get("interrupted")            # dispatched before its host last booted: INTERRUPTED says it all
        if intr:
            obs[f"intr:{t['task_id']}"] = "yes"
            info[f"intr:{t['task_id']}"] = {**info[key], **intr}
            info[key]["interrupted"] = True
        started = time.time() - t["elapsed_min"] * 60 if t.get("elapsed_min") is not None else 0
        # Stopped with the task still open. A session time from before this dispatch belongs to an
        # earlier worker in the same directory (shared profile), so it says nothing about this one.
        if not intr and obs[key] in STOPPED_STATES and (w or {}).get("active_at") and w["active_at"] >= started - 60:
            mins = int((time.time() - w["active_at"]) // 60)
            obs[f"still:{t['task_id']}"] = "late" if mins >= STILL_STOPPED_MIN else "ok"
            info[f"still:{t['task_id']}"] = {**info[key], "mins": mins}
        if w:                                  # once seen, a missing worker is GONE's news, not NO_WORKER's
            obs[f"seen:{t['task_id']}"] = "yes"
        nw = R.no_worker_age(t)
        if t["host"] not in bad["hosts"] and not intr:   # open with no worker: its slot holds nothing (--next without --dispatch)
            nkey = f"noworker:{t['task_id']}"
            obs[nkey] = "late" if obs[key] == "missing" and nw else "ok"
            info[nkey] = {**info[key], "age": nw[0] if nw else 0, "why": nw[1] if nw else ""}
        el, to = t.get("elapsed_min"), t.get("timeout_min")
        if el is not None and to and not intr:              # over its timeout while its worker still works
            okey = f"over:{t['task_id']}"      # the level follows the clock; the event needs a running worker
            obs[okey] = "over2" if el >= 2 * to else "over" if el >= to else "ok"
            info[okey] = {**info[key], "elapsed": el, "timeout": to, "running": obs[key] in ("working", "blocked")}
        g = t.get("git") or {}
        if g and not g.get("error"):          # draft-pr worktrees only (the probe reads no others)
            ukey = f"unpushed:{t['task_id']}"
            age = int((time.time() - g["oldest_unpushed"]) // 60) if g.get("oldest_unpushed") else 0
            obs[ukey] = "late" if age >= CHECKPOINT_MIN else "ok"
            info[ukey] = {**info[key], "age": age, "count": g.get("unpushed", 0), "dirty": g.get("dirty", 0)}
    for u in report.get("untracked", []):
        if u.get("issue") and u.get("name"):          # re-prompted worker of a closed task (fix round)
            key = f"agent:{u.get('host')}:{u.get('pane_id') or u['name']}"
            obs[key] = u.get("status") or "missing"
            info[key] = {"name": u["name"], "task": u["name"], "host": u.get("host")}
    try:
        leftovers = RP.candidates(report, queues) if RP.on_home(cfg, orch) else []
    except Exception:                                  # the reap view must never break events
        leftovers = []
    for c in leftovers:                                # one line per issue, not per agent
        key = f"reap:{c['issue']}"
        if bad.get("all") or data_bad(bad, "github", key) or data_bad(bad, "linear", key):
            continue
        obs[key] = "reap"
        i = info.setdefault(key, {"name": c["issue"], "reason": c["reason"], "count": 0, "hosts": set()})
        i["count"] += 1
        i["hosts"].add(c["host"])
    for r in report.get("issues", []):
        k, pr = r["issue"], r.get("pr") or {}
        if pr.get("state") == "open":        # an open PR can remain stacked on a merged branch
            sb = pr.get("stale_base")
            obs[f"base:{k}"] = "stale" if sb else "ok"
            info[f"base:{k}"] = {"issue": k, "number": pr.get("number"), "branch": (sb or {}).get("branch"),
                                 "number_under": (sb or {}).get("number"), "state": (sb or {}).get("state")}
        if r.get("unrouted"):
            continue
        if pr.get("state") == "open" and pr.get("draft") and pr.get("updated_at"):
            age = (time.time() - iso_ts(pr["updated_at"])) / 3600
            obs[f"draft:{k}"] = "idle" if age >= DRAFT_IDLE_H else "ok"
            info[f"draft:{k}"] = {"issue": k, "number": pr.get("number"), "hours": int(age)}
        for step, st in r["steps"].items():
            if step == R.tracker_kind(r):
                if st.get("behind"):
                    obs[f"{step}:{k}"] = st["behind"]
                    info[f"{step}:{k}"] = {"issue": k, "state": st.get("state"), "want": st["behind"],
                                           "tracker_label": R.tracker_label(r)}
                continue
            if step not in R.row_pipeline(r):
                continue
            key = f"step:{k}:{step}"
            obs[key] = str(st.get("cls") or "") + ("+quiet" if st.get("quiet") else "")
            # A live worker's own state is reported through its task key; its step row
            # (need/unknown) would only repeat it.
            info[key] = {"issue": k, "step": step, "label": st.get("label", ""), "live": bool(st.get("active")),
                         "host": st.get("host")}
        if pr:
            obs[f"pr:{k}"] = str(pr.get("state"))
            info[f"pr:{k}"] = {"issue": k, "number": pr.get("number")}
            ck = pr.get("checks") or {}
            if sum(ck.values() or [0]):
                obs[f"ci:{k}"] = "fail" if ck.get("fail") else "pending" if ck.get("pending") else "pass"
                info[f"ci:{k}"] = {"issue": k, "number": pr.get("number"), "fail": ck.get("fail", 0)}
    if report.get("signing"):                          # the routing home's GPG agent (empty after a restart)
        obs["sign:home"] = report["signing"]
        info["sign:home"] = {}
    for h, c in cap.items():
        lv = c.get("levels") or {}
        obs[f"host:{h}"] = "hot" if lv.get("hot") else lv.get("overall", "unknown")
        info[f"host:{h}"] = {"host": h, "why": "; ".join(lv.get("hot") or [])}
        waiting = len(queues.get(h, []))
        free = c.get("max_heavy") is None or c.get("heavy", 0) < c["max_heavy"]
        obs[f"queue:{h}"] = "drain" if waiting and free and not lv.get("hot") else "ok"
        info[f"queue:{h}"] = {"host": h, "waiting": waiting}
    for tool, u in (report.get("usage") or {}).items():
        worst = max([float((u.get(k) or {}).get("used_percent") or 0) for k in ("five_hour", "week")] or [0])
        obs[f"limit:{tool}"] = "limit" if u.get("exhausted") or worst >= 90 else "ok"
        info[f"limit:{tool}"] = {"tool": tool, "pct": worst, "exhausted": u.get("exhausted")}
    for h in bad["hosts"]:
        obs[f"reach:{h}"] = "unreachable"
        info[f"reach:{h}"] = {"host": h, "why": next(u for u in report["unreachable"] if u.startswith(h + ":"))}
    if bad.get("other") or bad.get("all"):     # one stable key, whatever the message says
        obs["reach:_data"] = "degraded"
        info["reach:_data"] = {"host": "data", "why": "; ".join(u for u in report.get("unreachable", [])
                                                                  if u.split(":", 1)[0].strip() not in hosts)[:240]}
    return obs, info, bad


def carry_forward(prev, obs, info, bad):
    """Keep the previous value for every key this snapshot can't vouch for."""
    if not prev:
        return obs

    def doubtful(key):
        if key.startswith("reach:") or key == STATE_VERSION_KEY:
            return False
        if bad.get("all"):
            return True
        kind = key.split(":", 1)[0]
        host = (info.get(key) or {}).get("host")
        if kind in ("task", "agent", "host", "queue", "over", "unpushed", "still", "noworker", "seen", "intr", "boot"):
            return (host or key.split(":")[1]) in bad["hosts"]
        if kind == "step":
            return data_bad(bad, "github", key) or (info.get(key, {}).get("live") and host in bad["hosts"])
        if kind in ("pr", "ci", "base", "draft"):
            return data_bad(bad, "github", key)
        if kind == "linear":
            return data_bad(bad, "linear", key) or data_bad(bad, "github", key)
        if kind == "reap":
            return data_bad(bad, "github", key) or data_bad(bad, "linear", key)
        return False

    out = {}
    for key, val in obs.items():
        if key.startswith("reap:") and doubtful(key) and key not in prev:
            continue                          # carry-forward cannot invent completion
        out[key] = prev[key] if doubtful(key) and key in prev else val
    for key, val in prev.items():            # keys this snapshot couldn't see at all
        if key not in out and (doubtful_missing(key, bad) or unread_git(key, obs)):
            out[key] = val
    return out


def unread_git(key, obs):
    """An open task whose worktree git couldn't be read this time (a timeout, an
    unreachable host) keeps its UNPUSHED level, so the next good read doesn't re-fire."""
    kind, _, tid = key.partition(":")
    if kind == "still":           # unread this time (a blip, no session time): kept while the task is open
        return obs.get(f"task:{tid}") not in (None, "working", "blocked")   # and its worker hasn't resumed
    if kind == "boot":            # a host's last boot time: kept while it can't be read
        return True
    if kind == "intr":            # kept while the task is open (an unreachable host can't vouch either way)
        return f"task:{tid}" in obs
    if kind == "seen":            # a worker was seen once: remembered while the task is open
        return f"task:{tid}" in obs
    return kind == "unpushed" and f"task:{tid}" in obs


def doubtful_missing(key, bad):
    if key.startswith("reach:") or key == STATE_VERSION_KEY:
        return False
    if bad.get("all"):
        return True
    kind, _, rest = key.partition(":")
    if kind == "agent":
        return rest.split(":")[0] in bad["hosts"]
    if kind == "reap":                       # a host or GitHub blip hides its agents or its PR state
        return bool(bad["hosts"]) or data_bad(bad, "github", key) or data_bad(bad, "linear", key)
    if kind in ("step", "pr", "ci", "base", "draft"):
        return data_bad(bad, "github", key)
    if kind == "linear":
        return data_bad(bad, "linear", key) or data_bad(bad, "github", key)
    return False


def settle(prev, obs, streaks):
    """HOT/COOL hysteresis: a machine's level changes only after SETTLE_POLLS polls in
    a row agree, and `unknown` never changes it (a missing reading isn't news)."""
    for key in [k for k in obs if k.startswith("host:")]:
        old, new = prev.get(key), obs[key]
        if old is None or new == old:
            streaks.pop(key, None)
            continue
        if new == "unknown":
            obs[key] = old
            continue
        crossing = (new == "hot") != (old == "hot")
        if not crossing:                  # ok <-> warn: no event either way, take it as is
            streaks.pop(key, None)
            continue
        streaks[key] = streaks.get(key, 0) + 1
        if streaks[key] < SETTLE_POLLS:
            obs[key] = old
        else:
            streaks.pop(key, None)
    return obs


def events(prev, obs, info, first=False):
    """Transitions worth the orchestrator's attention, as (kind, text)."""
    out = []
    for key, new in obs.items():
        if key == STATE_VERSION_KEY:
            continue
        old = prev.get(key)
        if old == new:
            continue
        i = info.get(key, {})
        kind = key.split(":", 1)[0]
        if kind in ("task", "agent"):
            where = f"{i.get('name', key)} on {i.get('host')}" + \
                (f" (task {i['task']})" if i.get("task") and i.get("task") != i.get("name") else "")
            if kind == "agent" and old is None and new != "blocked" and not first:
                continue                              # a leftover agent first seen: not news
            if i.get("interrupted") and new != "blocked":
                continue                              # its host restarted: INTERRUPTED, not STOPPED or GONE
            if new == "blocked":
                out.append(("BLOCKED", f"{where}: answer it within its grant (route --blocked)"))
            elif new in STOPPED_STATES and old not in STOPPED_STATES and (kind == "task" or old is not None):
                out.append(("STOPPED", f"{where}: read its output (route --listen {i.get('task')}); idle isn't success"))
            elif new == "missing" and old not in (None, "missing"):
                out.append(("GONE", f"{where}: its worker pane disappeared; inspect before anything else"))
        elif kind == "step":
            cls, quiet = str(new).split("+")[0], str(new).endswith("+quiet")
            was_cls = str(old or "").split("+")[0]
            if cls in ATTENTION and cls != was_cls:
                if cls in ("need", "unknown") and (i.get("live") or "unreachable" in str(i.get("label"))):
                    continue                          # the task key or UNREACHABLE already says it
                out.append((cls.upper(), f"{i['issue']} {i['step']}: {i['label']}"))
            elif quiet and not str(old or "").endswith("+quiet"):
                out.append(("QUIET", f"{i['issue']} {i['step']}: {i['label']}"))
        elif kind == "linear":
            label = i.get("tracker_label", "Linear")
            out.append((label.upper(), f"{i['issue']} is {i['state']} in {label}; the pipeline says {i['want']}"))
        elif kind == "pr" and old is not None:
            if new == "merged":
                out.append(("MERGED", f"{i['issue']} #{i['number']}"))
            elif new == "closed":
                out.append(("PR_CLOSED", f"{i['issue']} #{i['number']} closed without merging"))
        elif kind == "over" and i.get("running") and OVER_RANK.get(new, 0) > OVER_RANK.get(old, 0):
            out.append(("OVERTIME", f"{i['name']} on {i['host']}: working {i['elapsed']} min against a {i['timeout']} "
                                    f"min timeout: check its progress (route --listen {i['task']} --handoff); "
                                    f"ask the human whether to extend or stop it"))
        elif kind == "reap" and new == "reap" and old != "reap":
            out.append(("REAP", f"{i['name']}: {i['count']} leftover agent(s) on {', '.join(sorted(i['hosts']))} "
                                f"({i['reason']}): list with route --reap, close their tabs with --reap --apply"))
        elif kind == "base" and new == "stale" and old != "stale":
            out.append(("STALE_BASE", f"{i['issue']} #{i['number']}: its base {i['branch']} (#{i['number_under']}) was "
                                      f"{i['state']}: restack it onto main or retarget the PR before more work"))
        elif kind == "draft" and new == "idle" and old != "idle":
            out.append(("DRAFT_IDLE", f"{i['issue']} #{i['number']}: draft untouched for {i['hours']} h: "
                                      f"finish, hand off or close it (ask the human)"))
        elif kind == "still" and new == "late" and old != "late":
            out.append(("STILL_STOPPED", f"{i['name']} on {i['host']}: stopped {i['mins']} min ago and task "
                                         f"{i['task']} is still open: read it (route --listen {i['task']}) "
                                         f"and close it or start its next round"))
        elif kind == "boot" and old is not None:
            ts = i.get("tasks") or []
            out.append(("HOST_RESET", f"{i['host']} restarted at {R.utc_hm(i['boot_at'])}: every process on it died "
                                      f"(workers, children, containers); {len(ts)} open task(s) interrupted"
                                      + (f": {', '.join(ts)}" if ts else "") + "; re-dispatch only with the human's OK"))
        elif kind == "sign" and new == "locked" and old != "locked":
            out.append(("GPG_LOCKED", "the routing home's GPG agent has no passphrase (lost on restart): signed "
                                      "commits, pushes and merges fail and draft-pr is blocked; ask the human to run, "
                                      "in their own terminal: echo test | gpg --clearsign >/dev/null"))
        elif kind == "intr" and new == "yes" and old != "yes":
            out.append(("INTERRUPTED", f"{i['task']} on {i['host']}: its host restarted at {R.utc_hm(i['boot_at'])} "
                                       f"while it was open (dispatched @ {i.get('commit') or '?'}): inspect its worktree "
                                       f"(unpushed work, reports), record it, then --close {i['task']} --outcome "
                                       f"interrupted; re-dispatch only with the human's OK"))
        elif kind == "noworker" and new == "late" and old != "late" \
                and prev.get(f"task:{i['task']}") in (None, "missing") and not prev.get(f"seen:{i['task']}"):  # GONE said it
            out.append(("NO_WORKER", f"{i['task']} on {i['host']}: open {i['age']} min with no worker ({i['why']}); "
                                     f"it holds its slot with nothing running: inspect (route --listen {i['task']}), "
                                     f"then --close it and route it again with --dispatch"))
        elif kind == "unpushed" and new == "late" and old != "late":
            out.append(("UNPUSHED", f"{i['name']} on {i['host']}: {i['count']} commit(s) unpushed for {i['age']} min "
                                    f"({i['dirty']} dirty files); its grant asks for a checkpoint push every "
                                    f"{CHECKPOINT_MIN} min: remind it at its next stop"))
        elif kind == "ci":
            if new == "fail":
                out.append(("CI_FAIL", f"{i['issue']} #{i['number']}: {i['fail']} failing check(s)"))
            elif new == "pass" and old in ("pending", "fail"):
                out.append(("CI_PASS", f"{i['issue']} #{i['number']}"))
        elif kind == "host":
            if new == "hot":
                out.append(("HOT", f"{i['host']}: {i['why']}; new work there queues"))
            elif old == "hot" and new != "unknown":
                out.append(("COOL", f"{i['host']} is {new} again"))
        elif kind == "queue" and new == "drain":
            out.append(("DRAIN", f"{i['host']} has {i['waiting']} queued and a free slot: route --next --dispatch"))
        elif kind == "limit" and new == "limit":
            what = f"limit reached ({i['exhausted']})" if i.get("exhausted") else f"{i['pct']:.0f}% of a plan window used"
            out.append(("LIMIT", f"{i['tool']}: {what}; slow down new {i['tool']} work until it resets"))
        elif kind == "reach":
            out.append(("UNREACHABLE", i["why"]))
    if not first:
        for key in prev:                     # a host (or the data) coming back
            if key.startswith("reach:") and key not in obs:
                what = key.split(":", 1)[1]
                out.append(("REACHABLE", "every data source answers again" if what == "_data" else f"{what} answers again"))
    return out


def first_run_view(obs):
    """On a first run, only what already needs action counts."""
    keep = {}
    for k, v in obs.items():
        if k == STATE_VERSION_KEY:
            continue
        v = str(v)
        if (v in ("blocked", "drain", "hot", "unreachable", "degraded", "over", "over2", "late", "reap", "stale") or v.split("+")[0] in ATTENTION
                or (k.startswith("task:") and v in STOPPED_STATES)):
            keep[k] = v
    return keep


def load_state():
    try:
        return R.load_json(STATE_FILE)
    except (OSError, ValueError):
        return None


def save_state(obs):
    """Atomic, per-process temp file. Raises OSError when it can't be saved."""
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obs, f)
    os.replace(tmp, STATE_FILE)


def forget(task_id):
    """Drop a task's key from the saved state, so the next run re-reports its worker's
    state if it still needs action (route.py --answer calls this: a worker that opens
    another dialog right after an answer must not wait for the heartbeat)."""
    st = load_state()
    if st and st.pop(f"task:{task_id}", None) is not None:
        try:
            save_state(st)
        except OSError:
            pass


def take_lock():
    """One instance per state file. Returns the open lock file, or None if another
    instance holds it (or locking isn't possible; then nothing else is guarded)."""
    try:
        import fcntl
    except ImportError:
        return True
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        f = open(STATE_FILE + ".lock", "a+")
    except OSError as e:
        return e                           # can't even create the lock: the state isn't writable
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def run(fetch, every, follow, timeout, cfg=None, orch=None):
    """Poll `fetch` every `every` seconds; print events; exit after the first batch
    unless `follow`. Exit codes: 0 events, 3 timeout, 4 repeated errors or state
    not writable, 5 another instance is already running."""
    lock = take_lock()
    if isinstance(lock, OSError):
        print(f"{stamp()} ERROR    can't write next to {STATE_FILE}: {lock}; every restart would repeat events",
              flush=True)
        return 4
    if lock is None:
        print(f"{stamp()} ALREADY  another --events is running on {STATE_FILE}; don't start a second one",
              flush=True)
        return 5
    try:
        return poll(fetch, every, follow, timeout, cfg, orch)
    finally:
        if hasattr(lock, "close"):
            lock.close()                     # releases the flock


def poll(fetch, every, follow, timeout, cfg=None, orch=None):
    prev = load_state()
    first = prev is None or prev.get(STATE_VERSION_KEY) != STATE_VERSION
    start, errors, streaks = time.time(), 0, {}
    while True:
        beat()
        try:
            obs, info, bad = observe(fetch(), cfg, orch)
            errors = 0
        except Exception as e:                       # one bad read never kills the stream
            errors += 1
            if follow:                               # a stream must outlive outages: back off, keep going
                if errors == 1 or errors % MAX_ERRORS == 0:
                    print(f"{stamp()} ERROR    snapshot failed ({errors} in a row, retrying): {str(e)[:160]}",
                          flush=True)
                time.sleep(max(0, min(MAX_BACKOFF_S, every * 2 ** min(errors, 6)) - every))
            else:
                print(f"{stamp()} ERROR    snapshot failed ({errors}/{MAX_ERRORS}): {str(e)[:160]}", flush=True)
                if errors >= MAX_ERRORS:
                    return 4
            obs = None
        if obs is not None:
            if first:
                batch = events({}, first_run_view(obs), info, first=True)
            else:
                obs = settle(prev, carry_forward(prev, obs, info, bad), streaks)
                batch = events(prev, obs, info)
            obs[STATE_VERSION_KEY] = STATE_VERSION
            for kind, text in batch:                 # print first: a lost save only means a repeat
                print(f"{stamp()} {kind:<8} {text}", flush=True)
            try:
                save_state(obs)
            except OSError as e:
                print(f"{stamp()} ERROR    can't save {STATE_FILE}: {e}; every restart would repeat events",
                      flush=True)
                return 4
            prev, first = obs, False
            if batch and not follow:
                return 0
        if timeout and time.time() - start > timeout:
            if not follow:
                print(f"{stamp()} TIMEOUT  nothing needed attention for {timeout // 60} min; start --events again",
                      flush=True)
                return 3
        time.sleep(every)


def beat():
    """Record that an --events consumer polled just now (never fatal)."""
    try:
        with open(STATE_FILE + ".heartbeat", "w") as f:   # route.events_idle_min reads its age
            f.write(str(int(time.time())))
    except OSError:
        pass


def consumer_idle_min(now=None):
    """Minutes since an --events consumer last polled, or None if none ever did."""
    try:
        return int(((now or time.time()) - os.path.getmtime(STATE_FILE + ".heartbeat")) // 60)
    except OSError:
        return None


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit("run it as: route.py --events [--follow] [--every N]")
