"""scripts/heavy: at most N at once, first come first served, a killed holder frees its slot."""
import json, os, signal, subprocess, sys, tempfile, time
H = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "heavy")
d = tempfile.mkdtemp(prefix="heavy-test-")
env = dict(os.environ, RT_HEAVY_DIR=d, RT_HEAVY_SLOTS="2")
fails = []
def st():
    s = json.loads(subprocess.check_output([H, "--status"], env=env))
    return [h["task"] for h in s["holders"]], [w["task"] for w in s["waiting"]]
log = os.path.join(d, "log")
procs = [subprocess.Popen([H, "sh", "-c", f"echo start {i} $(date +%s.%N) >> {log}; sleep 1; echo end {i} $(date +%s.%N) >> {log}"],
                          env=dict(env, RT_TASK=f"job{i}"), stderr=subprocess.DEVNULL) for i in range(5) if not time.sleep(0.15)]
for p in procs:
    p.wait()
ev = sorted((l.split() for l in open(log)), key=lambda e: float(e[2]))
cur = mx = 0; order = []
for k, i, _ in ev:
    cur += 1 if k == "start" else -1; mx = max(mx, cur)
    if k == "start": order.append(i)
if mx != 2: fails.append(f"max concurrent {mx}, want 2")
if order != ["0", "1", "2", "3", "4"]: fails.append(f"start order {order}, want arrival order")
p1 = subprocess.Popen([H, "sleep", "30"], env=dict(env, RT_TASK="crasher"), stderr=subprocess.DEVNULL); time.sleep(0.4)
p2 = subprocess.Popen([H, "sleep", "30"], env=dict(env, RT_TASK="holder2"), stderr=subprocess.DEVNULL); time.sleep(0.4)
p3 = subprocess.Popen([H, "true"], env=dict(env, RT_TASK="waiter"), stderr=subprocess.DEVNULL); time.sleep(0.8)
if st() != (["crasher", "holder2"], ["waiter"]): fails.append(f"before kill: {st()}")
os.kill(p1.pid, signal.SIGKILL)
try:
    p3.wait(timeout=10)
except subprocess.TimeoutExpired:
    fails.append("waiter never got the killed holder's slot")
p2.terminate(); p2.wait()
subprocess.run(["pkill", "-f", "^sleep 30$"], check=False)
if st() != ([], []): fails.append(f"after: {st()}")
print("heavy slots:", "PASS" if not fails else "FAIL " + "; ".join(fails))
sys.exit(1 if fails else 0)
