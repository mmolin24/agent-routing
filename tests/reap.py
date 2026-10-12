"""route.py --reap (scripts/reap.py): which leftover agents are reapable, and that
--apply re-checks each one live before closing only its tab. herdr faked."""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R
R.ROOT = tempfile.mkdtemp(prefix="reap-")
os.makedirs(os.path.join(R.ROOT, "runs"))
import reap as RP
RP.on_home = lambda *args: True

NOW = 1_800_000_000
H = 3600
def u(name, issue, status="done", host="desktop", pane=None, parent="", active=NOW - 40 * H):
    return {"name": name, "issue": issue, "status": status, "host": host, "pane_id": pane or f"p-{name}",
            "agent": "codex", "cwd": "/w", "parent": parent, "active_at": active}
report = {
    "tasks": [{"task_id": "app5-gate-gate"}],
    "issues": [{"issue": "APP-1", "pr": {"number": 1, "state": "merged"}, "steps": {}, "updated": NOW - H},
               {"issue": "APP-2", "pr": {"number": 2, "state": "open"}, "steps": {}, "updated": NOW - 30 * H},
               {"issue": "APP-3", "pr": {"number": 3, "state": "open"}, "steps": {}, "updated": NOW - 2 * H},
               {"issue": "APP-4", "pr": None, "steps": {"linear": {"state": "done", "live": True}}, "updated": NOW - H},
               {"issue": "APP-5", "pr": {"number": 5, "state": "merged"}, "steps": {}, "updated": NOW - H}],
    "untracked": [u("app1-impl", "APP-1"), u("app1-fixes-impl", "APP-1", "idle"), u("app1-busy", "APP-1", "working"),
                  u("app1-ask", "APP-1", "blocked"), u("app1-child", "APP-1", parent="app1-impl"),
                  u("app2-impl", "APP-2"), u("app3-impl", "APP-3"), u("app4-impl", "APP-4"),
                  u("app5-impl", "APP-5"), u("orchestrator", None), u("app9-hand", "APP-9")],
}
fails = []
def check(name, ok, got=None):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"  got {got}"))
    ok or fails.append(name)

c = RP.candidates(report, last={}, now=NOW)
names = sorted(x["name"] for x in c)
check("merged, Linear done and 24 h idle (routed or by hand) are reapable",
      names == ["app1-fixes-impl", "app1-impl", "app2-impl", "app4-impl", "app9-hand"], names)
check("reasons name the rule", {x["name"]: x["reason"] for x in c}["app2-impl"] == "no open task for 30 h"
      and {x["name"]: x["reason"] for x in c}["app1-impl"] == "PR #1 merged", c)
check("an open task keeps its issue's agents (fix rounds)", "app5-impl" not in names)
check("working, blocked, subagents, no issue: kept",
      not {"app1-busy", "app1-ask", "app1-child", "orchestrator"} & set(names), names)
c2 = RP.candidates(report, last={"APP-3": NOW - 25 * H}, now=NOW)
check("a recent issue update wins over an old close", "app3-impl" not in [x["name"] for x in c2])
fix = dict(report, untracked=[u("app2-impl", "APP-2", active=NOW - 2 * H)])
check("a fix round after the close restarts the clock", RP.candidates(fix, last={}, now=NOW) == [])
blind = dict(report, untracked=[u("app2-impl", "APP-2", active=0)])
check("no session time: the 24 h rule doesn't apply", RP.candidates(blind, last={}, now=NOW) == [])
check("a queued task keeps its issue's agents",
      "app1-impl" not in [x["name"] for x in RP.candidates(report, {"mac": ["app1-staging"]}, last={}, now=NOW)])
away = dict(report, unreachable=["routing home desktop unreachable (timed out); showing this machine's records"])
check("not the routing home's report: nothing", RP.candidates(away, last={}, now=NOW) == [])

# apply: live re-check per candidate
R.gather = lambda cfg, orch, active_dir: (report, {}, {})
RP.time.time = lambda: NOW
calls = []
live = [{"name": "app1-impl", "pane_id": "p-app1-impl", "agent_status": "done", "tab_id": "w1:t1", "workspace_id": "w1",
         "agent_session": {"value": "s-1"}},
        {"name": "app1-fixes-impl", "pane_id": "p-app1-fixes-impl", "agent_status": "working", "tab_id": "w1:t2", "workspace_id": "w1"},
        {"name": "app2-impl", "pane_id": "p-app2-impl", "agent_status": "idle", "tab_id": "w2:t1", "workspace_id": "w2"},
        {"name": "app2-helper", "pane_id": "p-x", "agent_status": "working", "tab_id": "w2:t1", "workspace_id": "w2"},
        {"name": "app4-impl", "pane_id": "p-app4-impl", "agent_status": "idle", "tab_id": "w9:t1", "workspace_id": "w9"}]
labels = {"w1": "APP-1", "w2": "APP-2", "w9": "orchestrator"}
panes = {"w1": [{"pane_id": "p-app1-impl", "tab_id": "w1:t1"}, {"pane_id": "p-app1-fixes-impl", "tab_id": "w1:t2"}],
         "w2": [{"pane_id": "p-app2-impl", "tab_id": "w2:t1"}, {"pane_id": "p-x", "tab_id": "w2:t1"}],
         "w9": [{"pane_id": "p-app4-impl", "tab_id": "w9:t1"}]}
def fake(cfg, host, orch, *args, **kw):
    calls.append(args)
    if args[:2] == ("agent", "list"):
        return {"result": {"agents": live}}
    if args[:2] == ("pane", "list"):
        return {"result": {"panes": panes[args[3]]}}
    if args[:2] == ("workspace", "get"):
        return {"result": {"workspace": {"label": labels[args[2]]}}}
    return {}
res = {e["name"]: e["result"] for e in RP.apply(c, {}, "desktop", fake)}
closes = [a[2] for a in calls if a[:2] == ("tab", "close")]
check("only the still-done agent's tab is closed", closes == ["w1:t1"], closes)
check("went working since the list: skipped", res["app1-fixes-impl"].startswith("skipped: it is working"), res)
check("tab shared with a working agent: skipped", "also holds app2-helper" in res["app2-impl"], res)
check("workspace not the issue's: skipped", "not APP-4's" in res["app4-impl"], res)
check("gone from herdr: skipped", res["app9-hand"].startswith("skipped: the agent is gone"), res)
check("one agent list per host", sum(a[:2] == ("agent", "list") for a in calls) == 1)
calls.clear()
panes["w1"].append({"pane_id": "p-metro", "tab_id": "w1:t1"})            # a dev server split, no agent
live[0]["agent_status"] = "done"
res = RP.apply([x for x in c if x["name"] == "app1-impl"], {}, "desktop", fake)
check("a pane with no agent in the tab: skipped, nothing closed",
      "p-metro (no agent)" in res[0]["result"] and not any(a[:2] == ("tab", "close") for a in calls), res)
panes["w1"].pop()
log = [json.loads(x) for x in open(os.path.join(R.ROOT, "runs", "reaped.jsonl"))]
check("every outcome logged, with the session id to resume", len(log) == 6 and log[0]["session"] == "s-1"
      and log[0]["result"] == "reaped", log[:1])
calls.clear()
def broken(cfg, host, orch, *args, **kw):
    if args[:2] == ("tab", "close"):
        raise RuntimeError("herdr tab failed")
    return fake(cfg, host, orch, *args, **kw)
res = RP.apply([c[0]] if c[0]["name"] == "app1-impl" else [x for x in c if x["name"] == "app1-impl"], {}, "desktop", broken)
check("a failed close is reported, not retried", res[0]["result"].startswith("skipped: herdr tab failed"), res)
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
