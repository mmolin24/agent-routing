"""Codex question dialogs vs approval prompts (dispatch.blocked/answer/question/reply),
with herdr faked. No live systems; links live in a temp ROOT."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R
import dispatch as D

CFG = R.load_json(os.path.join(R.ROOT, "tests", "fixtures", "hosts.golden.json"))
R.ROOT = tempfile.mkdtemp(prefix="qdlg-")
COLLAPSED = """• Running pnpm test
  ◦ esc to interrupt
  Queued follow-up inputs
  ? 1 question
    shift+left to answer"""
OPENED = """  Question 1/1
  Which staging account should the smoke use?
  1. qa-bot (Recommended)
  2. Other
  enter to submit"""
APPROVAL = """  Would you like to run the following command?
  $ git push origin feature/APP-1
  1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `git push` (p)
  3. No, and tell Codex what to do differently (esc)"""

state = {}
sent = []

def fake_herdr(cfg, host, orch, *args, timeout=60, want_json=True):
    if args[:2] == ("agent", "get"):
        return {"result": {"agent": {"agent_status": state["status"], "pane_id": "w1:p1"}}}
    if args[:2] == ("agent", "read"):
        return state["screen"]
    sent.append(args)
    if args[:3] == ("pane", "send-keys", "w1:p1") and args[3] == "shift+left":
        state["screen"] = OPENED
    return "" if not want_json else {}

D.herdr = fake_herdr
D.time.sleep = lambda s: None
os.makedirs(os.path.join(R.ROOT, "runs", "tasks", "t1"))
link_path = os.path.join(R.ROOT, "runs", "tasks", "t1", "link.json")
with open(link_path, "w") as f:
    json.dump({"host": "desktop", "agent": "t1-impl", "root_pane": "w1:p1"}, f)
active = [(None, {"task_id": "t1", "authorization": {"scope": "draft-pr"},
                  "link": {"host": "desktop", "agent": "t1-impl"}}, "desktop")]

fails = []
def check(name, ok):
    print(f"{'PASS' if ok else 'FAIL'} {name}")
    ok or fails.append(name)

def raises(fn, *a):
    try:
        fn(*a)
    except D.DispatchError as e:
        return str(e)
    return None

state.update(status="blocked", screen=COLLAPSED)
b = D.blocked(CFG, "desktop", active)
check("collapsed question is a question on a running worker", b[0]["kind"] == "question" and b[0]["running"])
check("--answer esc refused on a question", "interrupt" in (raises(D.answer, "t1", "esc", {}, "desktop") or ""))
check("--reply refused while collapsed", "collapsed" in (raises(D.reply, "t1", "qa-bot", {}, "desktop") or ""))
check("nothing typed so far", sent == [])
txt = D.question("t1", {}, "desktop")
check("--question opens it with shift+left and prints it", "staging account" in txt
      and sent == [("pane", "send-keys", "w1:p1", "shift+left")])
D.question("t1", {}, "desktop")
check("--question on an open question sends no more keys", len(sent) == 1)
D.reply("t1", "use  qa-bot\n", {}, "desktop")
check("--reply types one line then enter", sent[1:] == [("pane", "send-text", "w1:p1", "use qa-bot"),
                                                         ("pane", "send-keys", "w1:p1", "enter")])
log = json.load(open(link_path))["answers"]
check("keys and reply logged in the link", [x.get("key") or x.get("reply") for x in log] == ["shift+left", "use qa-bot"])

state.update(screen=APPROVAL); del sent[:]
check("approval prompt is an approval", D.blocked(CFG, "desktop", active)[0]["kind"] == "approval")
check("--reply refused on an approval", "approval" in (raises(D.reply, "t1", "yes", {}, "desktop") or ""))
check("--question refused on an approval", "approval" in (raises(D.question, "t1", {}, "desktop") or ""))
check("approval mentioning a question stays an approval", D.dialog_kind(APPROVAL + "\nQuestion 1") == "approval")
D.answer("t1", "esc", {}, "desktop")
check("--answer esc still works on an approval", sent == [("agent", "send-keys", "t1-impl", "esc")])

state.update(status="working", screen=OPENED); del sent[:]
check("nothing typed into a worker that isn't blocked",
      raises(D.reply, "t1", "x", {}, "desktop") and raises(D.question, "t1", {}, "desktop") and sent == [])

# Review findings: approval wording in the worker's own output, other keys, loose prose, partial replies
state.update(status="blocked", screen='Editing SKILL.md: use p ("don\'t ask again") only for reads\n' + COLLAPSED); del sent[:]
check("collapsed footer wins over approval prose", D.dialog_kind(state["screen"]) == "question")
check("--answer esc still refused then", "interrupt" in (raises(D.answer, "t1", "esc", {}, "desktop") or ""))
check("--answer 1 refused on a question (would land in the composer)",
      raises(D.answer, "t1", "1", {}, "desktop") and sent == [])
CLAUDE_EDIT = "I have 2 questions for you\n Do you want to make this edit to a.py?\n 1. Yes\n 2. Yes, allow all edits\n 3. No (esc)"
state.update(screen=CLAUDE_EDIT)
check("Claude edit prompt with question prose is an approval", D.dialog_kind(CLAUDE_EDIT) == "approval")
check("--reply refused on it", raises(D.reply, "t1", "1", {}, "desktop") and sent == [])
state.update(screen="I have 2 questions about Question 3 of the spec\n  ◦ esc to interrupt")
check("question prose without a dialog is unknown", D.dialog_kind(state["screen"]) == "unknown")
check("--reply/--question refused on unknown", raises(D.reply, "t1", "x", {}, "desktop")
      and raises(D.question, "t1", {}, "desktop") and sent == [])
check("--answer refused on unknown with esc to interrupt", raises(D.answer, "t1", "esc", {}, "desktop")
      and raises(D.answer, "t1", "enter", {}, "desktop") and sent == [])
state.update(screen=OPENED)
real = D.herdr
def enter_fails(cfg, host, orch, *args, **kw):
    if args[:2] == ("pane", "send-keys"):
        raise D.DispatchError("ssh dropped")
    return real(cfg, host, orch, *args, **kw)
D.herdr = enter_fails
raises(D.reply, "t1", "partial", {}, "desktop")
D.herdr = real
last = json.load(open(link_path))["answers"][-1]
check("typed text is logged even when enter fails", last.get("reply") == "partial" and last.get("enter") == "pending")
D.reply("t1", "full", {}, "desktop")
check("a full reply is logged with enter sent", json.load(open(link_path))["answers"][-1].get("enter") == "sent")

print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
