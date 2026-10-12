"""Every docker call routing makes runs with DBUS_SESSION_BUS_ADDRESS=disabled: (unless a
real bus is set), so WSL never autolaunches a dbus-daemon per call. A fake docker on PATH
records the env it saw; both readers (Python host_load, Go collector) are checked."""
import json, os, stat, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
T = tempfile.mkdtemp(prefix="nodbus-")
log = os.path.join(T, "seen")
fake = os.path.join(T, "docker")
open(fake, "w").write(f'#!/bin/sh\necho "${{DBUS_SESSION_BUS_ADDRESS-unset}}" >> {log}\necho 1\n')
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
fails = []
def check(name, ok, got=None):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"  got {got}"))
    ok or fails.append(name)
def seen():
    v = open(log).read().split() if os.path.exists(log) else []
    os.path.exists(log) and os.remove(log)
    return v
env = {k: v for k, v in os.environ.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
env["PATH"] = T + os.pathsep + env["PATH"]
code = "import host_load; host_load.docker()"
subprocess.run([sys.executable, "-B", "-c", code], cwd=os.path.join(HERE, "..", "scripts"), env=env, capture_output=True)
v = seen(); check("Python host_load: docker sees disabled:", v and set(v) == {"disabled:"}, v)
subprocess.run([sys.executable, "-B", "-c", code], cwd=os.path.join(HERE, "..", "scripts"),
               env={**env, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/real"}, capture_output=True)
v = seen(); check("Python host_load: a real bus is kept", v and set(v) == {"unix:path=/tmp/real"}, v)
exe = os.path.join(T, "agent-collect")
b = subprocess.run(["go", "build", "-o", exe, "./cmd/agent-collect"], cwd=os.path.join(HERE, "..", "collector"), capture_output=True, text=True)
if b.returncode:
    check("go build", False, b.stderr[-300:])
else:
    # Probe lists Herdr before reading load. Keep the env check independent of
    # a live server so it actually reaches the fake Docker on fresh worktrees.
    fake_herdr = os.path.join(T, "herdr")
    open(fake_herdr, "w").write('#!/bin/sh\necho \'{"result":{"agents":[]}}\'\n')
    os.chmod(fake_herdr, os.stat(fake_herdr).st_mode | stat.S_IEXEC)
    probe = subprocess.run([exe, "probe", json.dumps({"herdr": fake_herdr})],
                           env=env, capture_output=True, text=True, timeout=60)
    check("Go probe reaches load", probe.returncode == 0, probe.stderr[-300:])
    v = seen(); check("Go collector: docker sees disabled:", v and set(v) == {"disabled:"}, v)
src = open(os.path.join(HERE, "..", "scripts", "route.py")).read()
check("route.py sets it for every child", 'os.environ["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"' in src)
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
