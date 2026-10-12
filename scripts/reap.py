"""Leftover agents in issue workspaces (route.py --reap).

An agent is reapable when it is idle or done, sits in its issue's Herdr workspace,
the issue has no open task (fix rounds keep their worker until then), and either
the issue's pipeline or tracker is finished, its PR is closed, or nothing happened on it
for IDLE_AFTER_H hours (counting the agent's own last session write, so a fix round
after its task closed restarts the clock). Reaping closes only the agent's tab: the worktree, the
branch and the agent's session file stay, so it can be resumed. The orchestrator
runs it; routing never reaps on its own. Every closed tab is logged in runs/reaped.jsonl.
"""
import glob
import json
import os
import time

import route as R

IDLE_AFTER_H = 24
REAPABLE = ("idle", "done")
LOG = lambda: os.path.join(R.ROOT, "runs", "reaped.jsonl")  # noqa: E731


def closed_last():
    """Issue -> when its last routed task closed, from every closed record."""
    last = {}
    for path in glob.glob(os.path.join(R.ROOT, "runs", "closed", "*.json")):
        try:
            c = R.load_json(path)
        except (OSError, ValueError):
            continue
        key = R.issue_key(c.get("task_id"))
        if key and c.get("closed_at"):
            last[key] = max(last.get(key, 0), c["closed_at"])
    return last


def finished(row):
    """Why an issue row counts as finished, or None."""
    if not row:
        return None
    pr = row.get("pr") or {}
    # Retiring idle PR workers is independent of completion of later pipeline
    # checks. Preserve the lifecycle's merged/closed PR retirement rule.
    if pr.get("state") == "closed" or ("merge" in R.row_pipeline(row) and pr.get("state") == "merged"):
        return f"PR #{pr.get('number')} {pr['state']}"
    lin = R.tracker_state(row)
    if lin in ("done", "completed", "canceled", "cancelled", "duplicate"):
        return f"{R.tracker_label(row)} {lin}"
    if R.is_completed(row):
        name = R.row_pipeline(row)[-1]
        if name == "merge" and pr.get("state") == "merged":
            return f"PR #{pr.get('number')} merged"
        return f"{name} {(row['steps'].get(name) or {}).get('state', 'done')}"
    return None


def on_home(cfg=None, orch=None):
    """True on the routing home, where runs/closed and the open-task list live."""
    if cfg is None:
        local = os.path.join(R.ROOT, "config", "hosts.local.json")
        cfg = R.load_config(local if os.path.exists(local) else os.path.join(R.ROOT, "config", "hosts.example.json"))
    return not cfg.get("routing_home") or (orch or R.detect_orchestrator(cfg)) == cfg["routing_home"]


def candidates(report, queues=None, last=None, now=None):
    """Reapable agents from a gathered report: the untracked rows plus the reason.
    Nothing when the report isn't the routing home's (its task list may be empty)."""
    if any(str(u).startswith("routing home") for u in report.get("unreachable", [])):
        return []
    now = now or time.time()
    last = closed_last() if last is None else last
    rows = {r["issue"]: r for r in report.get("issues") or []}
    waiting = [tid for q in (queues or {}).values() for tid in q]
    open_issues = {R.issue_key(tid) for tid in [t.get("task_id") for t in report.get("tasks", [])] + waiting}
    out = []
    for u in report.get("untracked", []):
        key = u.get("issue")
        if not key or key in open_issues or u.get("status") not in REAPABLE or u.get("parent"):
            continue
        if R.issue_data_degraded(report, rows.get(key) or {"issue": key}):
            continue
        why = finished(rows.get(key))
        if not why:
            if not u.get("active_at"):    # no session file: we can't tell when it last worked
                continue
            seen = max(last.get(key, 0), (rows.get(key) or {}).get("updated") or 0, u["active_at"])
            hours = (now - seen) / 3600
            if hours < IDLE_AFTER_H:
                continue
            why = f"no open task for {int(hours)} h"
        out.append({"host": u.get("host"), "name": u.get("name"), "pane_id": u.get("pane_id"),
                    "agent": u.get("agent"), "status": u.get("status"), "cwd": u.get("cwd"),
                    "issue": key, "reason": why})
    return out


def apply(cands, cfg, orch, herdr, active_dir=None):
    """Re-check each candidate live, then close its tab. Never retried: a failure is
    reported and the next candidate goes ahead."""
    results, live = [], {}
    for c in cands:
        try:
            # A candidate is only a proposal. Re-read task/PR/tracker evidence
            # before inspecting the tab; degraded data or a new task vetoes it.
            report, queues, _ = R.gather(cfg, orch, active_dir or os.path.join(R.ROOT, "runs", "active"))
            eligible = candidates(report, queues)
            if not any((x["issue"], x["host"], x["name"], x["pane_id"]) ==
                       (c["issue"], c["host"], c["name"], c["pane_id"]) for x in eligible):
                raise RuntimeError("the issue is no longer safely reapable in the current snapshot")
            if c["host"] not in live:
                got = herdr(cfg, c["host"], orch, "agent", "list").get("result")
                live[c["host"]] = (got.get("agents") if isinstance(got, dict) else got) or []
            agents = live[c["host"]]
            ag = next((a for a in agents if a.get("pane_id") == c["pane_id"]), None)
            if not ag or ag.get("name") != c["name"]:
                raise RuntimeError("the agent is gone or its pane changed hands")
            if ag.get("agent_status") == "closed":
                results.append({**c, "result": "reaped with its tab"})
                continue
            if ag.get("agent_status") not in REAPABLE:
                raise RuntimeError(f"it is {ag.get('agent_status')} now")
            tab = ag.get("tab_id")
            got = herdr(cfg, c["host"], orch, "pane", "list", "--workspace", ag.get("workspace_id")).get("result")
            panes = [p for p in ((got.get("panes") if isinstance(got, dict) else got) or []) if p.get("tab_id") == tab]
            if not any(p.get("pane_id") == c["pane_id"] for p in panes):
                raise RuntimeError("its pane isn't in its tab's pane list")
            mine = {x["pane_id"] for x in cands if x["host"] == c["host"]
                    and any((x["host"], x["pane_id"], x["name"]) == (e["host"], e["pane_id"], e["name"])
                            for e in eligible)}
            by_pane = {a.get("pane_id"): a for a in agents}
            extra = [p.get("pane_id") for p in panes if p.get("pane_id") not in mine
                     or (by_pane.get(p.get("pane_id")) or {}).get("agent_status") not in REAPABLE + ("closed",)]
            if extra:                        # a dev server, a shell or a live agent would go with the tab
                names = [(by_pane.get(x) or {}).get("name") or f"{x} (no agent)" for x in extra]
                raise RuntimeError(f"its tab also holds {', '.join(map(str, names))}")
            others = [by_pane[p["pane_id"]] for p in panes if p.get("pane_id") != c["pane_id"] and p.get("pane_id") in by_pane]
            ws = herdr(cfg, c["host"], orch, "workspace", "get", ag.get("workspace_id")).get("result", {})
            label = (ws.get("workspace") or ws).get("label") or ""
            if R.issue_key(label) != c["issue"]:
                raise RuntimeError(f"its workspace is {label!r}, not {c['issue']}'s")
            herdr(cfg, c["host"], orch, "tab", "close", tab)
            for a in others:                 # their panes went with the tab
                a["agent_status"] = "closed"
            entry = {**c, "tab_id": tab, "session": (ag.get("agent_session") or {}).get("value"), "result": "reaped"}
        except Exception as e:  # noqa: BLE001  one candidate's failure must not stop the rest
            entry = {**c, "result": f"skipped: {e}"}
        entry.update(at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), by=orch)
        with open(LOG(), "a") as f:
            f.write(json.dumps(entry) + "\n")
        results.append(entry)
    return results
