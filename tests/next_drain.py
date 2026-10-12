"""route.drain (--next): a dry run without --dispatch, "started" only once a worker
launched, a blocked head no longer holds up the queue. No live systems."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R  # noqa: E402

TMP = tempfile.mkdtemp(prefix="drain-")
R.QUEUE_DIR = os.path.join(TMP, "queue")
ACTIVE = os.path.join(TMP, "active")
CFG = {"hosts": {"mac": {}, "desktop": {}}}
calls = {"record": [], "dispatch": []}
R.handoff_note = lambda task, d, cfg, host: "note"
R.record = lambda task, d, note, active, cfg: calls["record"].append(task["task_id"]) or "runs/tasks/" + task["task_id"]
R.print_human = lambda d: None
STATE = {}          # task_id -> "ok" | "blocked" | "wait"; dispatch outcome per task
R.decide = lambda a, cfg, path, task: {
    "selected_host": task["_queued_for"], "task_id": task["task_id"],
    "blockers": ["abc is 3 commits behind origin/main"] if STATE[task["task_id"]] == "blocked" else [],
    "queue_reasons": ["mac heavy 2/2"] if STATE[task["task_id"]] == "wait" else []}
DISPATCH_RC = {}
R.run_dispatch = lambda a, task, d, cfg: calls["dispatch"].append(task["task_id"]) or DISPATCH_RC.get(task["task_id"], 0)


def queue(host, *ids, movable=False, clear=True, start=0):
    """Queue ids on host. Pinned (an explicit --host) unless movable (load-shared)."""
    d = os.path.join(R.QUEUE_DIR, host)
    os.makedirs(d, exist_ok=True)
    for g in CFG["hosts"]:
        gd = os.path.join(R.QUEUE_DIR, g)
        for f in (os.listdir(gd) if clear and os.path.isdir(gd) else []):
            os.remove(os.path.join(gd, f))
    for n, tid in enumerate(ids, start):
        with open(os.path.join(d, f"{n:04d}-{tid}.json"), "w") as f:
            json.dump({"task_id": tid, "routing": {"requested_host": None if movable else host}}, f)


STARTED_ON = {}
_dispatch = R.run_dispatch
R.run_dispatch = lambda a, task, d, cfg: STARTED_ON.__setitem__(task["task_id"], task["_queued_for"]) or _dispatch(a, task, d, cfg)


def run(**flags):
    calls["record"].clear(), calls["dispatch"].clear(), STARTED_ON.clear()
    a = types.SimpleNamespace(**{"config": None, "dispatch": False, "record": False, **flags})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = R.drain(a, CFG, ACTIVE)
    return rc, buf.getvalue(), [os.path.basename(p) for p in R.queue_files("mac")]


bad = []
def check(name, ok, got):
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print("   ", got)
        bad.append(name)


STATE.update({"t1": "ok", "t2": "ok"})
queue("mac", "t1", "t2")
rc, out, left = run()
check("no --dispatch: dry run (exit 4), nothing recorded or dequeued, never says started",
      rc == 4 and not calls["record"] and not calls["dispatch"] and len(left) == 2
      and "would start now" in out and "started" not in out.replace("nothing started", ""), (rc, out, left, calls))

rc, out, left = run(dispatch=True)
check("--dispatch: records, dequeues and dispatches the head only",
      rc == 0 and calls["record"] == ["t1"] and calls["dispatch"] == ["t1"] and len(left) == 1, (rc, out, left, calls))

queue("mac", "t1")
DISPATCH_RC["t1"] = 3
rc, out, left = run(dispatch=True)
check("a failed dispatch exits 3, not 0", rc == 3 and "started" not in out, (rc, out, calls))
DISPATCH_RC.clear()

STATE.update({"t1": "blocked", "t2": "ok"})
queue("mac", "t1", "t2")
rc, out, left = run(dispatch=True)
check("a blocked head is reported and skipped; the next task starts",
      rc == 0 and calls["dispatch"] == ["t2"] and "t1 is BLOCKED" in out and "3 commits behind" in out
      and left == ["0000-t1.json"], (rc, out, left, calls))

STATE.update({"t1": "wait", "t2": "ok"})
queue("mac", "t1", "t2")
rc, out, left = run(dispatch=True)
check("a head still waiting for a slot keeps the rest waiting (FIFO)",
      rc == 4 and not calls["dispatch"] and len(left) == 2 and "still waiting" in out, (rc, out, left, calls))

STATE.update({"t1": "ok"})
queue("mac", "t1")
rc, out, left = run(record=True)
check("--record alone records without starting and says NOT started",
      rc == 0 and calls["record"] == ["t1"] and not calls["dispatch"] and "NOT started" in out, (rc, out, calls))

# Load-shared (no explicit host) tasks start on whichever capable host has room.
STATE.clear()
STATE.update({"d1": "ok", "d2": "ok", "p1": "ok"})
DECIDE = R.decide
full = {"desktop"}
R.decide = lambda a, cfg, path, task: dict(DECIDE(a, cfg, path, task), queue_reasons=(
    ["desktop heavy 3/3"] if task["_queued_for"] in full else []))
queue("desktop", "d1", "d2", movable=True)
rc, out, left = run(dispatch=True)
check("a load-shared task queued on a full desktop starts on the Mac, in queue order",
      rc == 0 and STARTED_ON == {"d1": "mac"} and "queued on desktop, load-shared" in out, (rc, out, STARTED_ON))
queue("desktop", "p1")
rc, out, left = run(dispatch=True)
check("an explicit host request never moves", rc == 4 and not STARTED_ON and "still waiting" in out, (rc, out, STARTED_ON))
STATE.update({"m1": "ok"})
full = set()
queue("desktop", "d1", movable=True)
queue("mac", "m1", clear=False, start=1)
rc, out, left = run(dispatch=True, host="mac")
check("--host mac drains the Mac only (pulling movable tasks to it)",
      rc == 0 and STARTED_ON == {"d1": "mac"} and "desktop:" not in out, (rc, out, STARTED_ON))
R.decide = DECIDE

print("next_drain ok" if not bad else "next_drain FAIL")
sys.exit(1 if bad else 0)
