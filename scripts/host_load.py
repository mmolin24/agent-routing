"""Live load on the machine it runs on (locally, or piped over SSH).

Prints one JSON object: cores, load average, memory, swap and Docker memory.
Standard library only; Linux (/proc) and macOS (sysctl, vm_stat, memory_pressure).
A missing source leaves its field null instead of failing.
"""

import json
import os
import platform
import re
import shutil
import subprocess


# Without a session bus, repeated probes can spawn persistent D-Bus daemons and exhaust resources.
NO_BUS = {"DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "disabled:"}


def run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **NO_BUS})
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def linux_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, v = ln.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    out = {"mem_total": info.get("MemTotal"), "mem_avail": info.get("MemAvailable"),
           "swap_total": info.get("SwapTotal"), "swap_used": (info.get("SwapTotal") or 0) - (info.get("SwapFree") or 0)}
    p = psi_pressure()
    if p:
        out["pressure"] = p
    return out


def psi_pressure(path="/proc/pressure/memory"):
    """Linux's memory pressure (PSI), as the Mac's levels: the share of the last minute
    tasks stalled on memory. full avg60 >= 10% critical, some avg60 >= 10% warn."""
    try:
        with open(path) as f:
            avg = {ln.split()[0]: float(ln.split()[2].split("=")[1]) for ln in f if ln.strip()}
    except (OSError, ValueError, IndexError):
        return None
    return "critical" if avg.get("full", 0) >= PSI_PCT else "warn" if avg.get("some", 0) >= PSI_PCT else "normal"


PSI_PCT = 10.0


def mac_mem():
    total = int(run(["sysctl", "-n", "hw.memsize"]).strip() or 0) or None
    avail = None
    m = re.search(r"free percentage:\s*(\d+)%", run(["memory_pressure"], timeout=8))
    if m and total:
        avail = total * int(m.group(1)) // 100        # macOS's own estimate, incl. reclaimable memory
    swap_total = swap_used = None
    sw = run(["sysctl", "-n", "vm.swapusage"])        # total = 11264.00M  used = 10195.00M  free = ...
    t, u = re.search(r"total = ([\d.]+)M", sw), re.search(r"used = ([\d.]+)M", sw)
    if t and u:
        swap_total, swap_used = int(float(t.group(1)) * 2**20), int(float(u.group(1)) * 2**20)
    out = {"mem_total": total, "mem_avail": avail, "swap_total": swap_total, "swap_used": swap_used}
    # macOS's own verdict on memory (compression and swapping), not swap size: swap
    # files grow on demand, so "% used" is no limit. 1 normal, 2 warn, 4 critical.
    level = run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    if level in PRESSURE:
        out["pressure"] = PRESSURE[level]
    return out


PRESSURE = {"1": "normal", "2": "warn", "4": "critical"}


def to_bytes(s):
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", s.strip())
    if not m:
        return 0
    mult = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}
    return int(float(m.group(1)) * mult.get(m.group(2), 1))


def docker():
    exe = shutil.which("docker") or next((p for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                                                      "/Applications/Docker.app/Contents/Resources/bin/docker")
                                          if os.path.exists(p)), None)
    if not exe:
        return {"docker": None}
    info = run([exe, "info", "--format", "{{.MemTotal}} {{.ContainersRunning}}"]).split()
    if len(info) != 2 or not info[0].isdigit():
        return {"docker": "down"}
    limit, running = int(info[0]), int(info[1]) if info[1].isdigit() else 0
    used = 0
    if running:                                   # `docker stats` samples ~0.5s; skip it when idle
        for ln in run([exe, "stats", "--no-stream", "--format", "{{.MemUsage}}"], timeout=10).splitlines():
            used += to_bytes(ln.split("/")[0])
    return {"docker": "up", "docker_limit": limit, "docker_used": used, "containers": running}


# Orphaned development servers can outlive their starting task or session.
# Routing lists them read-only; stopping one is the human's call.
DEV_RE = re.compile(r"\b(expo start|vite|next dev|next start|webpack serve|storybook dev|react-native start)\b")
INIT_RE = re.compile(r"^(/sbin/launchd|launchd|/init|init|/sbin/init|.*/systemd|systemd|Relay\(\d+\))(\s|$)")
MAX_CWD = 10          # directories read per probe (one lsof each on the Mac)
PORT_RE = re.compile(r"--port[= ](\d+)")


def etime_s(text):
    """ps etime ([[dd-]hh:]mm:ss) in seconds."""
    days, _, rest = text.strip().rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def orphans(ps_text=None, cwd_of=None):
    """Dev servers whose parent is init (launchd, /init, systemd, WSL's Relay): their
    starter is gone. One entry per orphaned tree: pid, age, ports from --port, the
    tree's memory and its directory."""
    if ps_text is None:
        ps_text = run(["ps", "-axo", "pid=,ppid=,etime=,rss=,command="])
    procs = {}
    for ln in ps_text.splitlines():
        f = ln.split()
        if len(f) >= 5 and f[0].isdigit() and f[1].isdigit():
            try:      # single-spaced like Go's strings.Fields, so both readers match alike
                procs[int(f[0])] = {"ppid": int(f[1]), "age": etime_s(f[2]), "rss": int(f[3]) * 1024, "cmd": " ".join(f[4:])}
            except ValueError:
                continue
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)
    out = []
    for pid, p in sorted(procs.items()):
        if not DEV_RE.search(p["cmd"]):
            continue
        parent = procs.get(p["ppid"])
        if p["ppid"] != 1 and not (parent and INIT_RE.match(parent["cmd"])):
            continue
        tree, stack = [], [pid]
        while stack:
            q = stack.pop()
            tree.append(q)
            stack.extend(kids.get(q, []))
        ports = sorted({int(m) for q in tree for m in PORT_RE.findall(procs[q]["cmd"])})
        out.append({"pid": pid, "age_s": p["age"], "ports": ports,
                    "rss": sum(procs[q]["rss"] for q in tree),
                    "cwd": (cwd_of or proc_cwd)(pid) if len(out) < MAX_CWD else "",   # lsof per orphan: cap the cost
                    "cmd": DEV_RE.search(p["cmd"]).group(1)})
    return out


def proc_cwd(pid):
    """A process's working directory, or "" when it can't be read."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    for ln in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=4).splitlines():
        if ln.startswith("n"):
            return ln[1:]
    return ""


def boot_at():
    """When the machine last booted (epoch seconds): a change means every process on it died."""
    try:
        with open("/proc/stat") as f:
            for ln in f:
                if ln.startswith("btime "):
                    return int(ln.split()[1])
    except (OSError, ValueError):
        pass
    m = re.search(r"sec = (\d+)", run(["sysctl", "-n", "kern.boottime"]))
    return int(m.group(1)) if m else None


def boot_id():
    """This boot's id: it changes on every restart and nothing else (boot_at can shift
    when a WSL VM resumes after Windows sleeps)."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            kernel = f.read().strip()
        with open("/proc/1/stat") as f:          # a WSL distro restart keeps the VM's kernel boot id
            init = f.read().rsplit(")", 1)[1].split()[19]
        return f"{kernel}/{init}" if kernel else None
    except (OSError, IndexError):
        return run(["sysctl", "-n", "kern.bootsessionuuid"]).strip() or None


def disk_path():
    """The volume that really holds the machine's disks. On WSL that is Windows' C:, which
    backs ext4.vhdx and Docker's docker_data.vhdx. The virtual disk can show free space
    while its backing volume is full, causing host or Docker failures."""
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            wsl = "microsoft" in f.read().lower()
    except OSError:
        wsl = False
    if wsl and os.path.isdir("/mnt/c"):
        return "/mnt/c"
    if platform.system() == "Darwin":
        return "/System/Volumes/Data"     # where Docker.raw lives
    return "/"


def disk():
    p = disk_path()
    try:
        st = os.statvfs(p)
    except OSError:
        return {"disk_path": p, "disk_free": None, "disk_total": None}
    return {"disk_path": p, "disk_free": st.f_bavail * st.f_frsize, "disk_total": st.f_blocks * st.f_frsize}


def snapshot():
    out = {"os": platform.system(), "cores": os.cpu_count()}
    try:
        out["load1"], out["load5"], out["load15"] = (round(x, 2) for x in os.getloadavg())
    except OSError:
        out["load1"] = out["load5"] = out["load15"] = None
    try:
        out.update(linux_mem() if out["os"] == "Linux" else mac_mem())
    except (OSError, ValueError):
        pass
    out.update(docker())
    out["boot_at"] = boot_at()
    out["boot_id"] = boot_id()
    out.update(disk())
    out["orphans"] = orphans()
    return out


if __name__ == "__main__":
    print(json.dumps(snapshot()))
