"""route.py --steer (dispatch.steer): only open tasks with a working worker, logged,
sent once. herdr faked; links live in a temp ROOT."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R
import dispatch as D

CFG = R.load_json(os.path.join(R.ROOT, "tests", "fixtures", "hosts.golden.json"))
R.ROOT = tempfile.mkdtemp(prefix="steer-")
state = {"status": "working", "fail": False}
sent = []

def fake_herdr(cfg, host, orch, *args, timeout=60, want_json=True):
    if args[:2] == ("agent", "get"):
        return {"result": {"agent": {"agent_status": state["status"], "pane_id": "w1:p1"}}}
    sent.append(args)
    if state["fail"]:
        raise D.DispatchError("herdr agent prompt: timeout")
    return {} if want_json else ""

D.herdr = fake_herdr
os.makedirs(os.path.join(R.ROOT, "runs", "tasks", "t1"))
link_path = os.path.join(R.ROOT, "runs", "tasks", "t1", "link.json")
with open(link_path, "w") as f:
    json.dump({"host": "desktop", "agent": "t1-impl", "root_pane": "w1:p1"}, f)
active = [(None, {"task_id": "t1"}, "desktop")]
link = lambda: json.load(open(link_path))

fails = []
def check(name, ok):
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    ok or fails.append(name)

def raises(*a):
    try:
        D.steer(*a)
    except D.DispatchError as e:
        return str(e)
    return None

D.steer("t1", "  stop after the API tests;\n report  ", CFG, "desktop", active)
check("working: one agent prompt, whitespace folded",
      sent == [("agent", "prompt", "t1-impl", "stop after the API tests; report")])
check("logged as steer, sent", link()["answers"][-1].get("steer") == "stop after the API tests; report"
      and link()["answers"][-1]["sent"] == "yes")
for status, word in (("blocked", "--answer"), ("idle", "fix round"), ("done", "fix round")):
    sent.clear(); state["status"] = status
    e = raises("t1", "x", CFG, "desktop", active)
    check(f"{status}: refused, nothing sent", e and word in e and not sent)
state["status"] = "working"
check("closed task refused", "not an open task" in (raises("t1", "x", CFG, "desktop", []) or "") and not sent)
check("empty refused", raises("t1", " \n ", CFG, "desktop", active) is not None and not sent)
check("over 2000 chars refused", raises("t1", "a" * 2001, CFG, "desktop", active) is not None and not sent)
check("leading dash refused", "'-'" in (raises("t1", "--stop now", CFG, "desktop", active) or "") and not sent)
seq = iter(["working", "idle"])
state_get = fake_herdr
def racing(cfg, host, orch, *args, **kw):
    if args[:2] == ("agent", "get"):
        return {"result": {"agent": {"agent_status": next(seq)}}}
    return state_get(cfg, host, orch, *args, **kw)
D.herdr = racing
out = D.steer("t1", "stop at the tests", CFG, "desktop", active)
check("went idle during the send: warns, logs landed", out.startswith("warning:") and link()["answers"][-1].get("landed") == "idle")
D.herdr = fake_herdr; sent.clear()
state["fail"] = True; n = len(link()["answers"])
check("send failure: raised once, never retried", raises("t1", "y", CFG, "desktop", active) and len(sent) == 1)
check("send failure: logged as pending", len(link()["answers"]) == n + 1 and link()["answers"][-1]["sent"] == "pending")
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
