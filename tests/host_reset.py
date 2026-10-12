"""route.mark_interrupted: a task dispatched in an earlier boot of its host is
interrupted (boot_id recorded at dispatch; the clock, with slack, for older tasks)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R  # noqa: E402

R.ROOT = tempfile.mkdtemp(prefix="reset-")
ACTIVE = os.path.join(R.ROOT, "runs", "active")
os.makedirs(ACTIVE)
BOOT = 1_800_000_000


def iso(e):
    return R.time.strftime("%Y-%m-%dT%H:%M:%SZ", R.time.gmtime(e))


def task(tid, dispatched, boot_id=None):
    d = os.path.join(R.ROOT, "runs", "tasks", tid)
    os.makedirs(d)
    with open(os.path.join(ACTIVE, tid + ".json"), "w") as f:
        json.dump({"task_id": tid, "project_commit": "abcdef0123456789"}, f)
    if dispatched is not None:
        with open(os.path.join(d, "link.json"), "w") as f:
            json.dump({"dispatched_at": iso(dispatched), "state": "dispatched"}, f)
    if boot_id:
        with open(os.path.join(d, "decision.json"), "w") as f:
            json.dump({"preflight": {"boot_id": boot_id}}, f)
    return {"task_id": tid, "host": "desktop", "agents": []}


rows = [task("old-id", BOOT + 9999, "boot-1"),      # recorded in boot 1: interrupted, whatever the clock says
        task("same-id", BOOT - 9999, "boot-2"),     # recorded in this boot: not (a clock shift can't fool it)
        task("old-clock", BOOT - 600),              # no boot id: dispatched well before the boot
        task("slack", BOOT - 60),                   # no boot id, within the slack: not
        task("never", None),                        # never dispatched
        dict(task("mac-down", BOOT - 9999, "boot-1"), host="mac")]   # host unread: no verdict
rep = {"tasks": rows, "loads": {"desktop": {"boot_id": "boot-2", "boot_at": BOOT}}}
R.mark_interrupted(rep, ACTIVE)
got = sorted(r["task_id"] for r in rows if r.get("interrupted"))
want = ["old-clock", "old-id"]
ok = got == want and rows[0]["interrupted"] == {"boot_at": BOOT, "commit": "abcdef012345"}
lines = R.reset_lines(rep)
ok = ok and len(lines) == 1 and "HOST RESET desktop" in lines[0] and "old-id @ abcdef012345" in lines[0]
nw = R.no_worker({"tasks": [dict(rows[0], dispatch="dispatched", agent="x", elapsed_min=99)]})
ok = ok and nw == []                                 # HOST RESET covers it, not NO_WORKER
hint = R.unreachable_hint("mac: ssh dev-mac: dev@h: Permission denied (publickey,password).")
ok = ok and "SSH AGENT LOCKED" in hint and R.unreachable_hint("mac: ssh timed out") == "mac: ssh timed out"
print("host_reset ok" if ok else f"host_reset FAIL {got} {lines} {nw} {hint}")
sys.exit(0 if ok else 1)
