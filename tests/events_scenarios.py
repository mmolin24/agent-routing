"""Throwaway simulation of scripts/events.py. No live systems, no runs/."""
import io, os, sys, tempfile, contextlib, copy
TMP = tempfile.mkdtemp(prefix="evstate-")
os.environ["ROUTE_EVENTS_STATE"] = os.path.join(TMP, "state.json")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import events as E
E.RP.closed_last = lambda: {}          # never the real runs/closed
E.RP.on_home = lambda *args: True

class Exhausted(BaseException): pass

def fresh(name):
    E.STATE_FILE = os.path.join(TMP, name + ".json")
    if os.path.exists(E.STATE_FILE): os.remove(E.STATE_FILE)

def task(tid, host, status=None, agent=None):
    ags = [] if status is None else [{"name": agent or tid, "status": status, "role": "worker", "pane_id": "p-" + tid}]
    return {"task_id": tid, "host": host, "agents": ags}

def cap(hot=False, heavy=0, mx=2, overall="ok", unknown=False):
    lv = {"overall": "unknown", "hot": []} if unknown else {"overall": "hot" if hot else overall, "hot": ["load 40 on 16 cores"] if hot else []}
    return {"heavy": heavy, "max_heavy": mx, "levels": lv}

def snap(tasks=(), untracked=(), issues=(), queues=None, caps=None, unreachable=()):
    return ({"tasks": list(tasks), "untracked": list(untracked), "issues": list(issues), "unreachable": list(unreachable)},
            queues or {"desktop": [], "mac": []}, caps or {"desktop": cap(), "mac": cap()})

def run_once(snaps, follow=False):
    """One `route.py --events` instance: returns (rc, lines, snapshots consumed)."""
    it = iter(snaps); n = [0]
    def fetch():
        try: s = next(it)
        except StopIteration: raise Exhausted()
        n[0] += 1
        if isinstance(s, Exception): raise s
        return copy.deepcopy(s)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try: rc = E.run(fetch, 0, follow, 0)
        except Exhausted: rc = "exhausted"
    lines = [" ".join(l.split()[1:]) for l in buf.getvalue().splitlines()]
    return rc, lines, n[0]

def until_idle(snaps):
    """Restart loop like the orchestrator: restart after each exit until snapshots run out."""
    out, rest = [], list(snaps)
    for _ in range(50):
        rc, lines, used = run_once(rest)
        out.append(lines); rest = rest[used:]
        if rc == "exhausted": break
    return out

results = []
def check(n, title, ok, got, note=""):
    results.append((n, ok)); print(f"\n[{n}] {'PASS' if ok else 'FAIL'} {title}"); [print("   ", l) for l in got]; note and print("    note:", note)
def flat(batches): return [l for b in batches for l in b]
def kinds(lines): return [l.split()[0] for l in lines]

# baseline: a state file exists with nothing interesting
def seed(s): run_once([s])

# 1 working -> blocked -> working -> idle
fresh("s1"); T = "app1-impl"
seed(snap([task(T, "desktop", "working")]))
b = until_idle([snap([task(T, "desktop", st)]) for st in ["working", "blocked", "blocked", "working", "idle", "idle"]])
check(1, "blocked then stopped", kinds(flat(b)) == ["BLOCKED", "STOPPED"], flat(b))

# 2a mac snapshot raises mid-run
fresh("s2a"); T = "app2-impl"
seed(snap([task(T, "mac", "working")]))
b = until_idle([snap([task(T, "mac", "working")]), RuntimeError("ssh dev-mac timed out"), snap([task(T, "mac", "done")])])
check("2a", "fetch raises, mac returns done", kinds(flat(b)) == ["ERROR", "STOPPED"], flat(b),
      "ERROR line printed but does not end the batch (fine)")
# 2b mac listed unreachable, its task has no agents, mac was hot, queue waiting
fresh("s2b")
step_work = {"issue": "APP-2", "steps": {"impl": {"cls": "working", "label": "running on mac"}}}
step_unk = {"issue": "APP-2", "steps": {"impl": {"cls": "unknown", "label": "mac unreachable"}}}
step_wait = {"issue": "APP-2", "steps": {"impl": {"cls": "wait", "label": "worker stopped on mac"}}}
q = {"desktop": [], "mac": ["app9-gate"]}
up = lambda st, iss: snap([task(T, "mac", st)], issues=[iss], queues=q, caps={"desktop": cap(), "mac": cap(hot=True, heavy=1)})
down = snap([task(T, "mac", None)], issues=[step_unk], queues=q, unreachable=["mac: ssh: connect to host dev-mac port 22: Operation timed out"],
            caps={"desktop": cap(), "mac": cap(unknown=True, heavy=1)})
seed(up("working", step_work))
b = until_idle([up("working", step_work), down, down, up("done", step_wait)])
got = flat(b)
spurious = [l for l in got if l.split()[0] in ("GONE", "COOL", "DRAIN", "HOT", "UNKNOWN")]
check("2b", "mac unreachable then back done", not spurious and "STOPPED" in kinds(got), got,
      "expected only UNREACHABLE / REACHABLE / STOPPED")

# 3 restart semantics
fresh("s3"); T = "app3-impl"
r1 = run_once([snap([task(T, "desktop", "working")]), snap([task(T, "desktop", "blocked")])])
# change while nothing runs: blocked -> idle
r2 = run_once([snap([task(T, "desktop", "idle")])])
r3 = run_once([snap([task(T, "desktop", "idle")])] * 3)
check(3, "change between runs reported once", kinds(r1[1]) == ["BLOCKED"] and kinds(r2[1]) == ["STOPPED"] and r3[1] == [], r1[1] + r2[1] + r3[1])

# 4 a second instance on the same state file is refused (lock), so nothing is reported twice
fresh("s4")
import subprocess
holder = subprocess.Popen([sys.executable, "-c", (
    "import sys; sys.path.insert(0, %r); import events as E; E.STATE_FILE = %r\n"
    "s = ({'tasks': [], 'untracked': [], 'issues': [], 'unreachable': []}, {}, {})\n"
    "E.run(lambda: s, 1, True, 0)") % (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"), E.STATE_FILE)])
import time as _time; _time.sleep(1.5)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc4 = E.run(lambda: snap([task("app4-impl", "desktop", "blocked")]), 0, False, 1)
holder.terminate(); holder.wait()
check(4, "second instance refused", rc4 == 5 and "ALREADY" in buf.getvalue(), buf.getvalue().strip().splitlines())

# 5 task closed while worker done
fresh("s5"); T = "app5-impl"
seed(snap([task(T, "desktop", "working")]))
u = {"name": T, "host": "desktop", "pane_id": "p-" + T, "status": "done", "issue": "APP-5"}
b = until_idle([snap([task(T, "desktop", "done")]), snap(untracked=[u]), snap(untracked=[u]),
                snap(untracked=[dict(u, status="working")]), snap(untracked=[dict(u, status="done")])])
check(5, "close while done, then fix round", kinds(flat(b)) == ["STOPPED", "STOPPED"], flat(b),
      "1st STOPPED = task done, 2nd = fix-round worker finishing; nothing at the close itself")

# 6 host move
fresh("s6"); T = "app6-impl"
old = {"name": T, "host": "desktop", "pane_id": "w1-p3", "status": "done", "issue": "APP-6"}
seed(snap(untracked=[old]))
b = until_idle([snap([task(T, "mac", "working")], [old]), snap([task(T, "mac", "working")], [old]), snap([task(T, "mac", "done")], [old])])
check(6, "host move, same name", kinds(flat(b)) == ["STOPPED"] and "mac" in flat(b)[0], flat(b))

# 7 stale step, PR head moves again
fresh("s7")
iss = lambda cls, lab: {"issue": "APP-7", "steps": {"gate": {"cls": cls, "label": lab}}}
seed(snap(issues=[iss("done", "passed")]))
b = until_idle([snap(issues=[iss("stale", "passed on aaaa, PR head is now bbbb: re-run")]),
                snap(issues=[iss("stale", "passed on aaaa, PR head is now cccc: re-run")])] + [snap(issues=[iss("stale", "x")])] * 3)
check(7, "stale once", kinds(flat(b)) == ["STALE"], flat(b))

# 8 CI
fresh("s8")
pr = lambda **ck: {"issue": "APP-8", "steps": {}, "pr": {"number": 840, "state": "open", "checks": ck}}
seed(snap(issues=[pr(pending=3)]))
b = until_idle([snap(issues=[pr(pending=1, pass_=2)]), snap(issues=[pr(**{"pass": 3})]), snap(issues=[pr(**{"pass": 3})]),
                snap(issues=[pr(**{"pass": 2, "fail": 1})]), snap(issues=[pr(**{"pass": 2, "fail": 1})])])
check(8, "CI pending->pass, pass->fail", kinds(flat(b)) == ["CI_PASS", "CI_FAIL"], flat(b))

# 9 DRAIN once
fresh("s9"); q = {"desktop": ["app9-a", "app9-b"], "mac": []}
full = snap(queues=q, caps={"desktop": cap(heavy=2), "mac": cap()})
free = snap(queues=q, caps={"desktop": cap(heavy=1), "mac": cap()})
seed(full)
b = until_idle([full, free, free, free, free])
check(9, "drain once", kinds(flat(b)) == ["DRAIN"], flat(b))

# 10 HOT once then COOL
fresh("s10")
seed(snap())
hot = snap(caps={"desktop": cap(hot=True), "mac": cap()})
b = until_idle([hot] * 8 + [snap(caps={"desktop": cap(overall="warn"), "mac": cap()})] * 2)
check(10, "hot once, cool", kinds(flat(b)) == ["HOT", "COOL"], flat(b))

# 11 first-ever run
fresh("s11")
s = snap([task("app11-impl", "desktop", "blocked"), task("app12-gate", "mac", "done")],
         issues=[{"issue": "APP-11", "steps": {"impl": {"cls": "need", "label": "needs a decision on desktop"}}}])
b = until_idle([s, s, s])
k = kinds(flat(b))
check(11, "first run reports blocked+done once", k.count("BLOCKED") == 1 and k.count("STOPPED") == 1 and len(b[1:]) <= 1 and not flat(b[1:]),
      flat(b), "NEED is the same blocked worker seen through the issue row (a duplicate line)")
# 11b first run with an untracked fix-round worker already blocked
fresh("s11b")
s = snap(untracked=[{"name": "app13-impl", "host": "desktop", "pane_id": "p9", "status": "blocked", "issue": "APP-13"}])
b = until_idle([s, s])
check("11b", "first run, re-prompted worker already blocked", "BLOCKED" in kinds(flat(b)), flat(b))

# 12 wake loops under unchanging state
# 12a unwritable state dir
E.STATE_FILE = "/proc/nope/state.json"
s = snap([task("app14-impl", "desktop", "blocked")])
b = [run_once([s])[1] for _ in range(3)]
check("12a", "state not writable: ERROR, never repeated events", flat(b) and all(l.startswith("ERROR") for l in flat(b)), flat(b))
# 12b unreachable entries whose text before the first ':' varies
fresh("s12b")
seed(snap())
vary = [snap(unreachable=[f"routing home desktop unreachable (timed out after {t}s); showing this machine's records"]) for t in (10, 11, 12)]
b = until_idle(vary)
check("12b", "varying unreachable text", len(flat(b)) <= 1, flat(b))
# 12c pr state None on first run
fresh("s12c")
try:
    r = run_once([snap(issues=[{"issue": "APP-15", "steps": {}, "pr": {"number": 1, "state": None}}])]); ok = True; got = r[1]
except Exception as e:
    ok, got = False, [repr(e)]
check("12c", "first run with a null value", ok, got)
# 12d steady state after one batch
fresh("s12d")
s = snap([task("app16-impl", "desktop", "idle")], queues={"desktop": ["x"], "mac": []}, caps={"desktop": cap(hot=True), "mac": cap()})
b = until_idle([s] * 6)
check("12d", "steady state wakes once", len([x for x in b if x]) == 1, flat(b))

# 13 OVERTIME: once at the timeout, again at twice it, only while the worker works
def otask(tid, status, el, to=120, host="desktop"):
    t = task(tid, host, status); t["elapsed_min"], t["timeout_min"] = el, to; return t
fresh("s13")
seed(snap([otask("app17-impl", "working", 30)]))
b = until_idle([snap([otask("app17-impl", "working", el)]) for el in (60, 119, 120, 150, 239, 240, 300)])
check(13, "OVERTIME at 1x and 2x, once each", kinds(flat(b)) == ["OVERTIME", "OVERTIME"]
      and "120 min against a 120" in flat(b)[0] and "240 min" in flat(b)[1], flat(b))
fresh("s13b")
seed(snap([otask("app18-impl", "working", 30)]))
b = until_idle([snap([otask("app18-impl", "idle", 200)]), snap([otask("app18-impl", "idle", 300)])])
check("13b", "a stopped worker over its timeout: STOPPED only, no OVERTIME", "OVERTIME" not in kinds(flat(b)), flat(b))
fresh("s13c")
seed(snap([otask("app19-impl", "working", 100, host="mac")]))
b = until_idle([snap([otask("app19-impl", "working", 130, host="mac")], unreachable=["mac: ssh timed out"]),
                snap([otask("app19-impl", "working", 131, host="mac")])])
check("13c", "unreachable host: OVERTIME waits for real data, fires once", kinds(flat(b)).count("OVERTIME") == 1, flat(b))
fresh("s13d")
b = until_idle([snap([otask("app20-impl", "working", 316)])] * 2)
check("13d", "first run reports a task already far over", kinds(flat(b)) == ["OVERTIME"] and "316 min" in flat(b)[0], flat(b))

fresh("s13e")
seed(snap([otask("app21-impl", "working", 100)]))
b = until_idle([snap([otask("app21-impl", st, el)]) for st, el in (("working", 130), ("idle", 140), ("working", 150), ("unknown", 155), ("working", 160))])
check("13e", "a fix round or a blip after OVERTIME doesn't repeat it", kinds(flat(b)).count("OVERTIME") == 1, flat(b))

# 14 UNPUSHED: once when the oldest unpushed commit passes the checkpoint cadence
import time as _t
def gtask(tid, mins, n=3, err=None):
    t = task(tid, "desktop", "working")
    t["git"] = {"error": err} if err else {"unpushed": n, "dirty": 2, "stash": 0, "pushed": True,
                                            **({"oldest_unpushed": _t.time() - mins * 60} if n else {})}
    return t
fresh("s14")
seed(snap([gtask("app22-impl", 10)]))
b = until_idle([snap([gtask("app22-impl", m)]) for m in (30, 61, 90)] + [snap([gtask("app22-impl", 0, n=0)]),
               snap([gtask("app22-impl", 70)])])
check(14, "UNPUSHED past the cadence, again only after a push", kinds(flat(b)) == ["UNPUSHED", "UNPUSHED"]
      and "for 61 min" in flat(b)[0], flat(b))
fresh("s14b")
seed(snap([gtask("app23-impl", 10)]))
b = until_idle([snap([gtask("app23-impl", 0, err="timeout")]), snap([gtask("app23-impl", 0, err="timeout")])])
check("14b", "a git read error is never UNPUSHED", "UNPUSHED" not in kinds(flat(b)), flat(b))
fresh("s14c")
seed(snap([gtask("app24-impl", 10)]))
nogit = task("app24-impl", "desktop", "working")
b = until_idle([snap([gtask("app24-impl", 61)]), snap([gtask("app24-impl", 0, err="timeout")]), snap([nogit]),
               snap([gtask("app24-impl", 63)])])
check("14c", "late, then a failed or missing read, then late: one UNPUSHED", kinds(flat(b)) == ["UNPUSHED"], flat(b))

# 15 REAP: one line per issue when its leftover agents become reapable; a host blip doesn't re-fire
fresh("s15")
merged = {"issue": "APP-31", "pr": {"number": 31, "state": "merged"}, "steps": {}, "updated": 1}
left = [{"name": n, "issue": "APP-31", "status": "done", "host": "mac", "pane_id": n} for n in ("app31-impl", "app31-fixes-impl")]
seed(snap())
b = until_idle([snap(untracked=left, issues=[merged]), snap(untracked=left, issues=[merged]),
                snap(issues=[merged], unreachable=["mac: ssh: connect to host dev-mac port 22: Operation timed out"]),
                snap(untracked=left, issues=[merged]), snap(issues=[merged])])
check(15, "REAP once per issue, not again after a host blip or once reaped",
      kinds(flat(b)).count("REAP") == 1 and "2 leftover agent(s) on mac (PR #31 merged)" in flat(b)[0], flat(b))

# 16 STALE_BASE: an open PR stacked on a merged branch, routed or not, once
fresh("s16")
def spr(key, stale, unrouted=False):
    sb = {"branch": "feature/APP-40", "number": 40, "state": "merged"} if stale else None
    return {"issue": key, "unrouted": unrouted, "steps": {}, "updated": 1,
            "pr": {"number": 41, "state": "open", "draft": True, "stale_base": sb}}
seed(snap(issues=[spr("APP-41", False, True)]))
b = until_idle([snap(issues=[spr("APP-41", True, True)]), snap(issues=[spr("APP-41", True, True)]),
                snap(issues=[spr("APP-41", False, True)])])
check(16, "STALE_BASE once for an unrouted stacked PR, quiet after the restack",
      kinds(flat(b)) == ["STALE_BASE"] and "base feature/APP-40 (#40) was merged" in flat(b)[0], flat(b))

# 16b a GitHub blip hides the stacked PR: no second STALE_BASE when it comes back
fresh("s16b")
seed(snap(issues=[spr("APP-41", False, True)]))
blip = snap(issues=[])
blip[0]["degraded"] = ["github"]                 # what issues_view reports when gh pr list fails
b = until_idle([snap(issues=[spr("APP-41", True, True)]), blip,
                snap(issues=[spr("APP-41", True, True)])])
check("16b", "STALE_BASE once across a GitHub blip", kinds(flat(b)).count("STALE_BASE") == 1, flat(b))

# 17 DRAFT_IDLE: a routed issue's draft untouched 24 h, once; never for unrouted PRs
fresh("s17")
def dpr(key, hours, unrouted=False):
    at = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - hours * 3600))
    return {"issue": key, "unrouted": unrouted, "steps": {}, "updated": 1,
            "pr": {"number": 42, "state": "open", "draft": True, "updated_at": at}}
seed(snap(issues=[dpr("APP-42", 2), dpr("APP-43", 2, True)]))
b = until_idle([snap(issues=[dpr("APP-42", 25), dpr("APP-43", 25, True)]),
                snap(issues=[dpr("APP-42", 26), dpr("APP-43", 26, True)])])
check(17, "DRAFT_IDLE once for a routed draft, never for an unrouted one",
      kinds(flat(b)) == ["DRAFT_IDLE"] and "APP-42 #42: draft untouched for 25 h" in flat(b)[0], flat(b))

# 18 STILL_STOPPED: once when a stopped worker's task stays open 30 min; a blip keeps it; a new round re-arms it
fresh("s18")
def stask(tid, status, mins_ago, host="desktop"):
    t = task(tid, host, status)
    t["agents"][0]["active_at"] = _t.time() - mins_ago * 60
    return t
seed(snap([stask("app50-gate", "working", 0)]))
blip = snap([task("app50-gate", "mac")], unreachable=["mac: ssh: connect to host dev-mac port 22: Operation timed out"])
b = until_idle([snap([stask("app50-gate", "done", 5, "mac")]), snap([stask("app50-gate", "done", 31, "mac")]),
                blip, snap([stask("app50-gate", "done", 40, "mac")]),
                snap([stask("app50-gate", "working", 0, "mac")]), snap([stask("app50-gate", "idle", 35, "mac")])])
k = kinds(flat(b))
check(18, "STILL_STOPPED once per stop, not after a blip, again after a new round",
      k.count("STILL_STOPPED") == 2 and "stopped 31 min ago" in next(l for l in flat(b) if l.startswith("STILL_STOPPED")), flat(b))

# 18b a session time older than the dispatch (another worker's file in the same worktree) never fires
fresh("s18b")
old_file = stask("app52-review", "idle", 300)
old_file["elapsed_min"] = 2
seed(snap([stask("app52-review", "working", 0)]))
b = until_idle([snap([old_file]), snap([old_file])])
check("18b", "STILL_STOPPED ignores a session time from before the dispatch", "STILL_STOPPED" not in kinds(flat(b)), flat(b))

# 19 --follow never exits on failed snapshots: it backs off and keeps streaming
fresh("s19")
seed(snap([task("app51-impl", "desktop", "working")]))
boom = RuntimeError("ssh: Connection reset")
rc, lines, n = run_once([boom] * 7 + [snap([task("app51-impl", "desktop", "idle")])], follow=True)
check(19, "--follow outlives 7 failed snapshots and still reports the stop",
      rc == "exhausted" and n == 8 and "STOPPED" in kinds(lines) and kinds(lines).count("ERROR") < 7, (rc, n, lines))
rc, lines, n = run_once([boom] * 3)
check("19b", "one-shot still exits 4 after 3 failed snapshots", rc == 4, (rc, lines))

# 20 every poll touches the heartbeat; route warns when tasks are open and nobody reads events
fresh("s20")
hb = E.STATE_FILE + ".heartbeat"
if os.path.exists(hb): os.remove(hb)
run_once([snap()])
os.environ["ROUTE_EVENTS_STATE"] = E.STATE_FILE
fresh_idle = E.R.events_idle_min()
os.utime(hb, (_t.time() - 20 * 60,) * 2)
old_idle = E.R.events_idle_min()
w_old = E.R.events_warning({"tasks": [{}], "events_idle_min": old_idle})
os.remove(hb)
w_never = E.R.events_warning({"tasks": [{}], "events_idle_min": E.R.events_idle_min()})
del os.environ["ROUTE_EVENTS_STATE"]
check(20, "heartbeat: fresh after a poll, warning at 20 min idle and when never started, none with no tasks",
      fresh_idle == 0 and old_idle == 20 and "IDLE 20 MIN" in (w_old or "") and "NEVER RUN" in (w_never or "")
      and E.R.events_warning({"tasks": [], "events_idle_min": 99}) is None
      and E.R.events_warning({"tasks": [{}], "events_idle_min": 3}) is None, (fresh_idle, old_idle, w_old, w_never))

# 21 NO_WORKER: an open task with no worker for 5+ min (--next without --dispatch)
def ntask(tid, age, host="mac", dispatched=False, status=None, el=None, agent=True):
    t = task(tid, host, status); t["age_min"] = age; t["dispatch"] = "dispatched" if dispatched else "recorded, not dispatched"
    t["agent"] = tid if dispatched and agent else None
    if el is not None: t["elapsed_min"] = el
    return t
fresh("s21")
seed(snap())
b = until_idle([snap([ntask("app60-gate", a)]) for a in (0, 4, 6, 30, 90)])
check(21, "NO_WORKER once, after 5 min, for a task never dispatched",
      kinds(flat(b)) == ["NO_WORKER"] and "never dispatched" in flat(b)[0] and "6 min" in flat(b)[0], flat(b))
fresh("s21b")
seed(snap([ntask("app61-impl", 0, dispatched=True, status="working", el=20)]))
b = until_idle([snap([ntask("app61-impl", 0, dispatched=True, el=30)]), snap([ntask("app61-impl", 0, dispatched=True, el=40)])])
check("21b", "a worker that disappears: GONE only, not NO_WORKER as well", kinds(flat(b)) == ["GONE"], flat(b))
fresh("s21c")
seed(snap())
down = ["mac: ssh: connect to host dev-mac port 22: Operation timed out"]
b = until_idle([snap([ntask("app62-gate", 10)], unreachable=down), snap([ntask("app62-gate", 12)])])
check("21c", "an unreachable host never says NO_WORKER; the first good read does", kinds(flat(b)) == ["UNREACHABLE", "NO_WORKER", "REACHABLE"], flat(b))
fresh("s21e")
seed(snap([ntask("app63-impl", 0, dispatched=True, status="working", el=1)]))
b = until_idle([snap([ntask("app63-impl", 0, dispatched=True, el=e)]) for e in (2, 6, 20)])
check("21e", "a worker that dies in its first 5 min: GONE, never NO_WORKER after it", kinds(flat(b)) == ["GONE"], flat(b))
fresh("s21f")
seed(snap())
cmd = ntask("app64-cmd", 0, dispatched=True, el=30, agent=False)
setup = ntask("app65-impl", 0, el=8); setup["dispatch"] = None          # dispatch still running (Python reader)
failed = ntask("app66-impl", 0, el=7); failed["dispatch"] = "failed at worktree verify"
b = until_idle([snap([cmd, setup, failed])])
check("21f", "command mode never; a running dispatch not before 15 min; a failed dispatch does",
      kinds(flat(b)) == ["NO_WORKER"] and "app66-impl" in flat(b)[0] and "dispatch failed at worktree verify" in flat(b)[0], flat(b))
nw = E.R.no_worker({"tasks": [ntask("a", 10), ntask("b", 3), ntask("c", 10, status="working"),
                              ntask("d", 0, dispatched=True, el=8), ntask("e", 10, host="desktop")],
                    "unreachable": ["desktop: ssh timed out"]})
check("21d", "route.no_worker: old enough, no worker, reachable host",
      [(t["task_id"], m, why) for t, m, why in nw] == [("a", 10, "never dispatched (recorded only, or its plan run by hand)"), ("d", 8, "no worker agent on its host")], nw)

# 22 HOST_RESET + INTERRUPTED: a new boot id, once; interrupted tasks say INTERRUPTED, not STOPPED/STILL_STOPPED
def boot_snap(boot_id, tasks=(), unreachable=()):
    s = snap(tasks, unreachable=unreachable)
    if boot_id:
        s[0]["loads"] = {"desktop": {"boot_id": boot_id, "boot_at": 1_791_653_282}}
    return s
def itask(tid, status):
    t = stask(tid, "desktop" and status, 45); t["host"] = "desktop"
    t["interrupted"] = {"boot_at": 1_791_653_282, "commit": "4236d25f3ed9"}; return t
fresh("s22")
seed(boot_snap("boot-1", [task("app70-gate", "desktop", "working")]))
down = ["desktop: herdr agent list failed"]
b = until_idle([boot_snap(None, [task("app70-gate", "desktop")], unreachable=down),   # the VM is down: no reading
                boot_snap("boot-2", [itask("app70-gate", "idle")]),
                boot_snap("boot-2", [itask("app70-gate", "idle")])])
k = kinds(flat(b))
check(22, "HOST_RESET and INTERRUPTED once each across the outage; no STOPPED/STILL_STOPPED/NO_WORKER",
      k.count("HOST_RESET") == 1 and k.count("INTERRUPTED") == 1 and not {"STOPPED", "STILL_STOPPED", "NO_WORKER"} & set(k)
      and "4236d25f3ed9" in next(l for l in flat(b) if l.startswith("INTERRUPTED")), flat(b))
fresh("s22b")
seed(boot_snap("boot-1"))
b = until_idle([boot_snap("boot-1"), boot_snap("boot-1")])
check("22b", "the same boot id never fires", "HOST_RESET" not in kinds(flat(b)), flat(b))

# 23 GPG_LOCKED: once when the home's agent loses its passphrase, again only after an unlock
fresh("s23")
def gsnap(state):
    s = snap(); s[0]["signing"] = state; return s
seed(gsnap("unlocked"))
b = until_idle([gsnap("locked"), gsnap("locked"), gsnap("unlocked"), gsnap("locked")])
check(23, "GPG_LOCKED once per lock", kinds(flat(b)) == ["GPG_LOCKED", "GPG_LOCKED"], flat(b))

print("\nSUMMARY", " ".join(f"{n}:{'P' if ok else 'F'}" for n, ok in results))
sys.exit(0 if all(ok for _, ok in results) else 1)
