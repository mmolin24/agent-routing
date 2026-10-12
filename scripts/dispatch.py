"""Dispatch a routed task through Herdr, and listen to it afterwards.

Called from route.py (--dispatch, --listen, --status). Every Herdr call is an
argument list (no local shell); over SSH the remote command is quoted once.
Nothing is retried: a failed step stops and is recorded, so a human (or the
orchestrator) inspects before anything is sent again.
"""

import json
import os
import re
import shlex
import subprocess
import time

import route as R


class DispatchError(Exception):
    pass


def herdr_argv(cfg, host, orch, *args):
    """Argument list that runs `herdr <args>` against `host` from `orch`."""
    match R.reach_for(cfg, host, orch):
        case {"mode": "local"}:
            return [os.path.expandvars(cfg["hosts"][host]["herdr_bin"]), *args]
        case {"mode": "machine", "selector": sel}:
            return [os.path.expandvars(cfg["hosts"][orch]["herdr_bin"]), "--machine", sel, *args]
        case {"mode": "ssh", "ssh_alias": alias}:
            remote = cfg["hosts"][host]["herdr_bin"] + " " + " ".join(shlex.quote(a) for a in args)
            return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, remote]
    raise DispatchError(f"unknown reach for {host} from {orch}")


def herdr(cfg, host, orch, *args, timeout=60, want_json=True):
    argv = herdr_argv(cfg, host, orch, *args)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=R.clean_git_env())
    except subprocess.TimeoutExpired:
        raise DispatchError(f"herdr {' '.join(args[:2])} timed out after {timeout}s "
                            "(a timeout does not prove nothing happened: inspect before retrying)")
    if p.returncode != 0:
        raise DispatchError(f"herdr {' '.join(args[:2])} failed (exit {p.returncode}): "
                            f"{(p.stderr or p.stdout).strip()[-400:]}")
    if not want_json:
        return p.stdout
    if not p.stdout.strip():
        return {}   # e.g. `pane run` prints nothing on success
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise DispatchError(f"herdr {' '.join(args[:2])} returned non-JSON: {p.stdout[:200]!r}")


def link_commands(link, cfg, orch):
    """Shell commands the orchestrator can run (from `orch`) to listen in."""
    host = link["host"]
    hc = lambda *a: R.herdr_cmd(cfg, host, orch, *a)  # noqa: E731
    if link.get("agent"):
        a = link["agent"]
        return {"status": hc("agent", "get", a),
                "read": hc("agent", "read", a, "--source", "recent-unwrapped", "--lines", "200"),
                "wait": hc("agent", "wait", a, "--timeout", "600000"),
                "children": hc("pane", "list", "--workspace", link["workspace_id"])}
    return {"read": hc("pane", "read", link["root_pane"], "--source", "recent-unwrapped", "--lines", "200"),
            "children": hc("pane", "list", "--workspace", link["workspace_id"])}


def dispatch(task, d, cfg, orch, run_dir, marker):
    """Start the routed task on its host. Returns the link (also saved as link.json)."""
    host = d["selected_host"]
    repo_cfg = cfg["hosts"][host]["repos"][task["repository"]["name"]]
    link = {"task_id": task["task_id"], "host": host, "orchestrator_host": orch,
            "herdr": R.herdr_prefix(cfg, host, orch),   # as typed on the machine it was dispatched from
            "reach": R.reach_for(cfg, host, orch)["mode"], "mode": task.get("mode", "agent"),
            "worktree": R.worktree_dir(task, cfg, host), "branch": R.worktree_identity(task, cfg, host)[1],
            "project_commit": task["project_commit"], "dispatched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": []}

    def step(name, fn):
        link["steps"].append({"step": name, "state": "running"})
        try:
            out = fn()
        except DispatchError as e:
            link["steps"][-1].update(state="failed", error=str(e))
            link["state"] = f"failed at {name}"
            save(link, run_dir, marker)
            raise
        link["steps"][-1]["state"] = "ok"
        return out

    # 0. Agent names are unique per Herdr server: a finished agent from an earlier
    # task (e.g. before a host move) can still hold this one. Pick the first free
    # name (app123-impl, app123-impl-2, ...) before anything is created.
    if link["mode"] == "agent":
        base = task["worker"]["name"]
        for n in range(1, 10):
            cand = base if n == 1 else f"{base[:29]}-{n}"
            try:
                herdr(cfg, host, orch, "agent", "get", cand, timeout=30)
            except DispatchError as e:
                if "agent_not_found" not in str(e):      # only "not found" means free
                    raise
                task["worker"]["name"] = cand
                break
        else:
            raise DispatchError(f"agent names {base}..{base}-9 are all taken on {host}")
        if task["worker"]["name"] != base:
            link["agent_name_note"] = f"{base} is held by an earlier agent on {host}; using {task['worker']['name']}"

    # 1. The task's own tab, inside one workspace per issue (APP-123 > impl, gate, ...),
    # so Herdr lists issues rather than every task. The env (port slots) goes on the
    # tab: tabs don't share it. A task without an issue gets a workspace of its own.
    group, tab_label = R.herdr_group(task["task_id"])
    link["group"], link["tab_label"] = group, tab_label
    env = dict(d.get("env") or {}, RT_TASK=task["task_id"])     # RT_TASK names this task's heavy slots
    if (d.get("preflight") or {}).get("home"):                   # durable reports (not /tmp): see the brief
        env["ROUTE_REPORT_DIR"] = R.report_dir(d, task["task_id"])
    env_args = [x for k, v in env.items() for x in ("--env", f"{k}={v}")]

    def open_tab():
        listing = herdr(cfg, host, orch, "workspace", "list").get("result", {}).get("workspaces", [])
        mine = next((w for w in listing if w.get("label") == group), None) if group != task["task_id"] else None
        if mine:
            return herdr(cfg, host, orch, "tab", "create", "--workspace", mine["workspace_id"], "--cwd",
                         repo_cfg["path"], "--label", tab_label, *env_args, "--no-focus")
        made = herdr(cfg, host, orch, "workspace", "create", "--cwd", repo_cfg["path"], "--label", group,
                     *env_args, "--no-focus")
        tab_id = made.get("result", {}).get("tab", {}).get("tab_id")
        if tab_id and tab_label != group:
            try:
                herdr(cfg, host, orch, "tab", "rename", tab_id, tab_label)
            except DispatchError:
                pass                                    # cosmetic: the tab keeps its number
        return made

    ws = step("tab open", open_tab)
    res = ws.get("result", {})
    pane = res.get("root_pane", {})
    link["workspace_id"] = pane.get("workspace_id") or res.get("workspace", {}).get("workspace_id")
    link["tab_id"] = pane.get("tab_id") or res.get("tab", {}).get("tab_id")
    link["root_pane"] = pane.get("pane_id")
    if not (link["workspace_id"] and link["root_pane"]):
        raise DispatchError(f"tab/workspace create returned no workspace/pane id: {json.dumps(res)[:300]}")
    save(link, run_dir, marker)

    # 2. Worktree (project helper where it has one), then verify HEAD.
    setup = R.setup_command(task, cfg, host)
    if setup:
        cmd, ms = setup
        step("worktree setup", lambda: herdr(cfg, host, orch, "pane", "run", link["root_pane"], cmd))
        step("worktree wait", lambda: herdr(cfg, host, orch, "pane", "wait-output", link["root_pane"], "--regex",
                                            r"__RT_DONE_0__ rc=[0-9]+", "--timeout", str(ms), timeout=ms // 1000 + 60))
        text = step("worktree read", lambda: herdr(cfg, host, orch, "pane", "read", link["root_pane"], "--source",
                                                   "recent-unwrapped", "--lines", "120", want_json=False))
        m = re.findall(r"__RT_DONE_0__ rc=([0-9]+)", text)
        heads = [ln.strip() for ln in text.splitlines() if re.fullmatch(r"[0-9a-f]{40}", ln.strip())]
        rc = m[-1] if m else None
        link["setup_rc"], link["head_before"] = rc, (heads[-1] if heads else None)
        if rc != "0" or link["head_before"] != task["project_commit"]:
            link["state"] = "failed at worktree verify"
            link["steps"].append({"step": "worktree verify", "state": "failed",
                                  "error": f"rc={rc}, HEAD={link['head_before']}, expected {task['project_commit']}"})
            save(link, run_dir, marker)
            raise DispatchError(f"worktree verify failed: rc={rc}, HEAD={link['head_before']}, "
                                f"expected {task['project_commit']} (an existing branch ignores --base). "
                                f"Tab {link.get('group')} > {link.get('tab_label')} ({link.get('tab_id')}) left open "
                                f"for inspection.")

    # 3. Worker: start the agent and send exactly one brief, or run the commands.
    if link["mode"] == "agent":
        name = task["worker"]["name"]
        step("agent start", lambda: herdr(cfg, host, orch, "agent", "start", name, "--kind", task["worker"]["kind"],
                                          "--pane", link["root_pane"], "--timeout", "120000",
                                          *R.native_args(task["worker"], cfg), timeout=180))
        link["worker_args"] = R.native_args(task["worker"], cfg)[1:]
        link["agent"] = name
        # Tag the worker pane so status can build the tree from Herdr itself.
        try:
            herdr(cfg, host, orch, "pane", "report-metadata", link["root_pane"], "--source", "agent-routing",
                  "--token", f"task={task['task_id'][:32]}", "--token", "role=worker")
        except DispatchError:
            pass   # cosmetic: the tree still works from the workspace
        brief = d["handoff_note"] + (f"\nRouting link: workspace {link['group']} ({link['workspace_id']}), tab "
                                     f"{link['tab_label']} ({link.get('tab_id')}), pane {link['root_pane']}, agent {name}.\n")
        # One submission, no --wait: the orchestrator listens with --listen.
        step("agent prompt", lambda: herdr(cfg, host, orch, "agent", "prompt", name, brief, timeout=120))
    else:
        for i, c in enumerate(task["commands"], 1):
            run = R.command_run(c["run"], task, cfg, host)
            cdir = c.get("cwd", "<WORKTREE>").replace("<WORKTREE>", link["worktree"], 1)
            run = R.in_dir(cdir, run)
            if c.get("long_running"):
                sp = step(f"split for command {i}", lambda: herdr(cfg, host, orch, "pane", "split", link["root_pane"],
                                                                  "--direction", "right", "--cwd", link["worktree"],
                                                                  "--no-focus"))
                pane = sp.get("result", {}).get("pane", {}).get("pane_id")
                link.setdefault("server_panes", []).append(pane)
                step(f"command {i}", lambda: herdr(cfg, host, orch, "pane", "run", pane, run))
                step(f"command {i} ready", lambda: herdr(cfg, host, orch, "pane", "wait-output", pane, "--regex",
                                                         c["ready_regex"], "--timeout", "600000", timeout=660))
            else:
                ms = task["resources"]["timeout_minutes"] * 60000
                step(f"command {i}", lambda: herdr(cfg, host, orch, "pane", "run", link["root_pane"], run + R.done_marker(i)))
                step(f"command {i} wait", lambda: herdr(cfg, host, orch, "pane", "wait-output", link["root_pane"], "--regex",
                                                        rf"__RT_DONE_{i}__ rc=[0-9]+", "--timeout", str(ms),
                                                        timeout=ms // 1000 + 60))
    link["state"] = "dispatched"
    link["listen"] = link_commands(link, cfg, orch)
    save(link, run_dir, marker)
    return link


def save(link, run_dir, marker):
    with open(os.path.join(run_dir, "link.json"), "w") as f:
        f.write(json.dumps(link, indent=2) + "\n")
    # The open-task marker carries the link so --status can find the worker.
    try:
        with open(marker) as f:
            t = json.load(f)
        t["link"] = {k: link.get(k) for k in ("host", "workspace_id", "tab_id", "group", "tab_label", "root_pane",
                                               "agent", "state")}
        with open(marker, "w") as f:
            f.write(json.dumps(t, indent=2) + "\n")
    except (OSError, ValueError):
        pass


def load_link(task_id):
    path = os.path.join(R.ROOT, "runs", "tasks", task_id, "link.json")
    if not os.path.exists(path):
        raise DispatchError(f"no link for {task_id} (was it dispatched from this machine? {path})")
    return R.load_json(path)


def listen(task_id, cfg, orch, lines, wait_ms):
    """Read (or wait on) a dispatched task's worker from wherever we are."""
    link = load_link(task_id)
    host = link["host"]
    out = {"task_id": task_id, "host": host, "workspace_id": link.get("workspace_id"), "state": link.get("state"),
           "herdr": R.herdr_prefix(cfg, host, orch)}
    if link.get("agent"):
        if wait_ms:
            out["wait"] = herdr(cfg, host, orch, "agent", "wait", link["agent"], "--timeout", str(wait_ms),
                                timeout=wait_ms // 1000 + 60).get("result")
        got = herdr(cfg, host, orch, "agent", "get", link["agent"]).get("result", {})
        out["agent"] = link["agent"]
        out["agent_status"] = (got.get("agent") or got).get("agent_status")
        out["output"] = herdr(cfg, host, orch, "agent", "read", link["agent"], "--source", "recent-unwrapped",
                              "--lines", str(lines), want_json=False)
    else:
        out["output"] = herdr(cfg, host, orch, "pane", "read", link["root_pane"], "--source", "recent-unwrapped",
                              "--lines", str(lines), want_json=False)
    return out


def worker_handoff(task_id, cfg, orch):
    """The worker's last reply plus the facts the parent usually needs (PRs, SHAs, tests,
    open questions), read from its session file on its host: a few hundred tokens
    instead of 200 lines of terminal."""
    link = load_link(task_id)
    host, cwd = link["host"], link.get("worktree")
    try:
        kind = R.load_json(os.path.join(R.ROOT, "runs", "tasks", task_id, "task.json"))["worker"]["kind"]
    except (OSError, ValueError, KeyError):
        kind = "codex"
    got, status = {}, None
    if link.get("agent"):
        try:
            got = herdr(cfg, host, orch, "agent", "get", link["agent"]).get("result", {})
            got = got.get("agent") or got
            status = got.get("agent_status")
        except DispatchError as e:
            status = f"unknown ({str(e)[:80]})"
    session = (got.get("agent_session") or {}).get("value")   # its own transcript, not a neighbour's
    req = json.dumps({"kind": kind, "cwd": cwd, "session": session})
    reach = R.reach_for(cfg, host, orch)
    try:
        if reach["mode"] == "local":
            import agent_profile
            msg = agent_profile.last_message(kind, cwd, session=session)
        else:
            with open(os.path.join(R.ROOT, "scripts", "agent_profile.py"), "rb") as f:
                p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach["ssh_alias"],
                                    R.REMOTE_PY + " --last-message " + shlex.quote(req)],
                                   stdin=f, capture_output=True, text=True, timeout=30)
            msg = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        msg = None
    text = (msg or {}).get("text") or ""
    facts = {
        "prs": sorted(set(re.findall(r"(?:PR\s*#|pull/)(\d{2,6})", text, re.I))),
        "shas": sorted(set(re.findall(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", text)))[:6],
        "tests": [ln.strip() for ln in text.splitlines()
                  if re.search(r"\b(pass(ed)?|fail(ed|ing)?|green|red)\b", ln, re.I)][:8],
        "questions": [ln.strip() for ln in text.splitlines() if ln.strip().endswith("?")][:5],
    }
    out = {"task_id": task_id, "host": host, "agent": link.get("agent"), "herdr": R.herdr_prefix(cfg, host, orch),
           "facts": facts,
           "last_message": text or None, "source": (msg or {}).get("source")}
    if link.get("agent"):
        out["agent_status"] = status
    return out


def host_profiles(cfg, host, orch, agents):
    """Model/effort/fast for each agent, read from session files on its host."""
    items = [{"kind": a.get("agent"), "cwd": a.get("cwd"), "session": (a.get("agent_session") or {}).get("value")}
             for a in agents]
    if not items:
        return []
    reach = R.reach_for(cfg, host, orch)
    try:
        if reach["mode"] == "local":
            import agent_profile
            return agent_profile.profiles(items)
        script = os.path.join(R.ROOT, "scripts", "agent_profile.py")
        with open(script, "rb") as f:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach["ssh_alias"],
                                R.REMOTE_PY + " " + shlex.quote(json.dumps(items))],
                               stdin=f, capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout) if p.returncode == 0 else [None] * len(items)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [None] * len(items)


def probe_script():
    """agent_profile.py + host_load.py + probe.py as one stdin script (their CLI
    blocks dropped), so one SSH call does everything on the far side."""
    parts = ["PROBE_EMBEDDED = True\n"]
    for name in ("agent_profile.py", "host_load.py", "probe.py"):
        with open(os.path.join(R.ROOT, "scripts", name)) as f:
            src = f.read()
        parts.append(src.split('\nif __name__ == "__main__"')[0] + "\n")
    parts.append("print(json.dumps(probe(json.loads(sys.argv[1]), profiles, snapshot)))\n")
    return "".join(parts)


def probe_host(cfg, host, orch, workspaces, trees=None):
    """Agents, pane tokens, profiles, load and the draft-pr worktrees' git state for
    one host in one round trip."""
    req = {"herdr": cfg["hosts"][host]["herdr_bin"], "workspaces": workspaces}
    if trees:
        req["trees"] = trees
    reach = R.reach_for(cfg, host, orch)
    if reach["mode"] == "local":
        import agent_profile, host_load, probe     # in-process: no subprocess for Python work
        return probe.probe(req, agent_profile.profiles, host_load.snapshot)
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach["ssh_alias"],
                            R.REMOTE_PY + " " + shlex.quote(json.dumps(req))],
                           input=probe_script(), capture_output=True, text=True, timeout=45)
        if p.returncode == 0:
            return json.loads(p.stdout)
        err = (p.stderr or p.stdout).strip()[-200:]
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        err = str(e)
    if reach["mode"] == "machine":
        # SSH to the target may not work from here; Herdr's own link still gives the agents.
        agents = herdr(cfg, host, orch, "agent", "list", timeout=30).get("result", {}).get("agents", [])
        return {"agents": agents, "tokens": {}, "profiles": [None] * len(agents), "load": None,
                "note": f"probe over SSH failed ({err}); agents only via Herdr"}
    raise DispatchError(f"probe failed: {err}")


def push_tree(t):
    """{"task", "path", "branch"} for a dispatched task whose grant lets it push
    (project-workflow, e.g. draft-pr), from its link; else None."""
    if t.get("authorization", {}).get("scope") != "project-workflow":
        return None
    try:
        ln = R.load_json(os.path.join(R.ROOT, "runs", "tasks", t["task_id"], "link.json"))
    except (OSError, ValueError):
        return None
    if ln.get("state") != "dispatched" or not ln.get("worktree") or not ln.get("branch"):
        return None
    return {"task": t["task_id"], "path": ln["worktree"], "branch": ln["branch"]}


def live_status(cfg, orch, active):
    """Open tasks with live worker state and children, plus untracked agents, per host.
    Hosts are probed in parallel, one round trip each."""
    from concurrent.futures import ThreadPoolExecutor
    report = {"tasks": [], "untracked": [], "unreachable": [], "loads": {}}
    for _, t, host in active:
        ln = t.get("link") or {}
        report["tasks"].append({"task_id": t["task_id"], "host": host, "heavy": R.is_heavy(t), "summary": t.get("summary"),
                                "workspace_id": ln.get("workspace_id"), "tab_id": ln.get("tab_id"),
                                "group": ln.get("group"), "tab_label": ln.get("tab_label"), "agent": ln.get("agent"),
                                "dispatch": ln.get("state", "recorded, not dispatched"), "agents": []})
    # A task owns its tab (grouped layout) or, for older tasks, its whole workspace.
    index = {(r["host"], "tab:" + r["tab_id"] if r["tab_id"] else r["workspace_id"]): r
             for r in report["tasks"] if r["tab_id"] or r["workspace_id"]}
    hosts = [h for h in sorted(cfg["hosts"]) if orch in cfg["hosts"][h].get("reach", {})]
    spaces = {h: sorted({r["workspace_id"] for r in report["tasks"] if r["host"] == h and r["workspace_id"]})
              for h in hosts}
    trees = {h: [] for h in hosts}
    for _, t, host in active:
        tree = push_tree(t)
        if tree and host in trees:
            trees[host].append(tree)
    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        futures = {h: pool.submit(probe_host, cfg, h, orch, spaces[h], trees[h]) for h in hosts}
    for host in hosts:
        try:
            res = futures[host].result()
        except (DispatchError, Exception) as e:   # one host down must not hide the other
            report["unreachable"].append(f"{host}: {e}")
            continue
        if res.get("error"):
            report["unreachable"].append(f"{host}: {res['error']}")
            continue
        if res.get("note"):
            report["unreachable"].append(f"{host}: {res['note']}")
        report["loads"][host] = res.get("load")
        for r in report["tasks"]:
            if r["host"] == host and (res.get("git") or {}).get(r["task_id"]):
                r["git"] = res["git"][r["task_id"]]
        report.setdefault("heavy", {})[host] = res.get("heavy")
        for tool, u in (res.get("usage") or {}).items():          # newest reading per tool wins
            cur = report.setdefault("usage", {}).get(tool)
            if u and (cur is None or (u.get("at") or 0) > (cur.get("at") or 0)):
                report["usage"][tool] = u
        agents, tokens = res.get("agents", []), res.get("tokens", {})
        profs = res.get("profiles") or [None] * len(agents)
        cwd_count = {}
        for ag in agents:
            cwd_count[ag.get("cwd")] = cwd_count.get(ag.get("cwd"), 0) + 1
        # The worker is found by name: unique per machine, and it follows the agent when
        # someone moves its tab (Herdr renumbers moved tabs, so recorded ids go stale).
        # Children are the other agents in the worker's current tab (whole workspace,
        # for older tasks); only a task with no live worker falls back to recorded ids.
        by_name = {r["agent"]: r for r in report["tasks"] if r["host"] == host and r["agent"]}
        live, has_worker = {}, set()
        for ag in agents:
            r = by_name.get(ag.get("name")) if ag.get("name") else None
            if r is None:
                continue
            has_worker.add(r["task_id"])
            if ag.get("tab_id"):
                live["tab:" + ag["tab_id"]] = r
            if not r["tab_id"] and ag.get("workspace_id"):
                live[ag["workspace_id"]] = r
        for ag, prof in zip(agents, profs):
            prof = prof or {}
            entry = {"pane_id": ag.get("pane_id"), "name": ag.get("name"), "agent": ag.get("agent"),
                     "status": ag.get("agent_status"), "cwd": ag.get("cwd"),
                     "model": prof.get("model"), "effort": prof.get("effort"), "fast": bool(prof.get("fast")),
                     # Agents sharing a directory can't be told apart by session files,
                     # unless herdr gave the agent's session id and its file was found.
                     "profile_shared": cwd_count.get(ag.get("cwd"), 0) > 1 and not prof.get("by_session"),
                     # Set by whoever started this pane: `pane report-metadata <pane> --token parent=<pane>`.
                     "parent": tokens.get(ag.get("pane_id"), {}).get("parent"),
                     # Last write to its session file (0: none found); --reap's idle clock.
                     "active_at": int(prof.get("active_at") or 0),
                     "subagents": prof.get("subagents") or []}
            row, role = (by_name.get(ag.get("name")), "worker") if ag.get("name") in by_name else (None, "child")
            if row is None:
                row = (live.get("tab:" + ag["tab_id"]) if ag.get("tab_id") else None) or live.get(ag.get("workspace_id"))
            if row is None:
                rec = (index.get((host, "tab:" + ag["tab_id"])) if ag.get("tab_id") else None) or \
                    index.get((host, ag.get("workspace_id")))
                row = rec if rec is not None and rec["task_id"] not in has_worker else None
            if row is not None:
                entry["role"] = role
                row["agents"].append(entry)
            else:
                entry["host"] = host
                report["untracked"].append(entry)
    return report


ANSWER_KEYS = {"y", "n", "esc", "enter", "1", "2", "3"}
# Codex's request_user_input arrives while the run keeps going: herdr reports the
# agent blocked, but the screen shows only a collapsed "? 1 question / shift+left to
# answer" line under the queued inputs, and "esc to interrupt" would stop the run.
QUESTION_COLLAPSED_RE = re.compile(r"shift\+left to answer", re.I)
QUESTION_OPEN_RE = re.compile(r"^\s*Question \d+/\d+|unanswered question", re.I | re.M)
APPROVAL_RE = re.compile(r"yes, proceed|don't ask again|allow all edits|do you want to (?:proceed|make this edit|allow)", re.I)
RUNNING_RE = re.compile(r"esc to interrupt", re.I)
DIALOG_LINES = 20     # dialogs sit at the bottom; the worker's own output above may quote anything


def _dialog_area(screen):
    return "\n".join([x for x in screen.splitlines() if x.strip()][-DIALOG_LINES:])


def dialog_kind(screen):
    """'question' (Codex request_user_input, answered with text), 'approval' (a key) or
    'unknown'. The collapsed-question footer wins outright (it only appears in the
    composer); otherwise an approval prompt wins, so text is never typed into one."""
    area = _dialog_area(screen)
    if QUESTION_COLLAPSED_RE.search(area):
        return "question"
    if APPROVAL_RE.search(area):
        return "approval"
    return "question" if QUESTION_OPEN_RE.search(area) else "unknown"


def _blocked_worker(task_id, cfg, orch):
    """(link, pane_id) for a task whose worker is blocked right now, or raise."""
    link = load_link(task_id)
    if not link.get("agent"):
        raise DispatchError(f"{task_id} has no agent to answer")
    got = herdr(cfg, link["host"], orch, "agent", "get", link["agent"]).get("result", {})
    ag = got.get("agent") or got
    if ag.get("agent_status") != "blocked":
        raise DispatchError(f"{link['agent']} is {ag.get('agent_status')}, not blocked: refusing to type into it")
    return link, ag.get("pane_id") or link.get("root_pane")


def _screen(cfg, link, orch):
    return herdr(cfg, link["host"], orch, "agent", "read", link["agent"], "--source", "visible", want_json=False)


def _log_answer(task_id, link, orch, **entry):
    link.setdefault("answers", []).append({**entry, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           "from": orch})
    _log_answer_save(task_id, link)


def _log_answer_save(task_id, link):
    with open(os.path.join(R.ROOT, "runs", "tasks", task_id, "link.json"), "w") as f:
        f.write(json.dumps(link, indent=2) + "\n")


def blocked(cfg, orch, active):
    """Open tasks whose worker is waiting on a dialog, with the visible dialog text."""
    out = []
    for _, t, host in active:
        ln = t.get("link") or {}
        if not ln.get("agent"):
            continue
        got = herdr(cfg, ln["host"], orch, "agent", "get", ln["agent"]).get("result", {})
        if (got.get("agent") or got).get("agent_status") != "blocked":
            continue
        screen = herdr(cfg, ln["host"], orch, "agent", "read", ln["agent"], "--source", "visible", want_json=False)
        lines = [x for x in screen.splitlines() if x.strip()]
        out.append({"task_id": t["task_id"], "host": ln["host"], "agent": ln["agent"],
                    "grant": t["authorization"]["scope"], "kind": dialog_kind(screen),
                    "herdr": R.herdr_prefix(cfg, ln["host"], orch),
                    "running": bool(RUNNING_RE.search(screen)), "dialog": "\n".join(lines[-18:])})
    return out


def answer(task_id, key, cfg, orch):
    """Send one allowed key to a worker that is currently blocked; log it in the link."""
    if key not in ANSWER_KEYS:
        raise DispatchError(f"key {key!r} not allowed; use one of {sorted(ANSWER_KEYS)}")
    link, _ = _blocked_worker(task_id, cfg, orch)
    screen = _screen(cfg, link, orch)
    kind = dialog_kind(screen)
    if kind == "question":       # keys would land in the composer, and esc interrupts the run
        raise DispatchError(f"{link['agent']} is asking a question, not showing an approval: keys would be typed "
                            f"into its prompt (esc interrupts the run). Read it with --question {task_id}, "
                            f"answer with --reply {task_id} \"<text>\"")
    if kind == "unknown" and RUNNING_RE.search(_dialog_area(screen)):
        raise DispatchError(f"{link['agent']} shows no recognisable dialog and 'esc to interrupt' is on screen: "
                            f"keys would reach its prompt (esc stops the run). Read it with --blocked and ask the human")
    herdr(cfg, link["host"], orch, "agent", "send-keys", link["agent"], key)
    _log_answer(task_id, link, orch, key=key)
    return f"answered {task_id} ({link['agent']} on {link['host']}) with {key!r}; logged in its link"


def question(task_id, cfg, orch):
    """Open a blocked worker's collapsed question (shift+left) and return the visible text."""
    link, pane = _blocked_worker(task_id, cfg, orch)
    screen = _screen(cfg, link, orch)
    if dialog_kind(screen) != "question":
        raise DispatchError(f"{link['agent']} shows no question (an approval or an unrecognised dialog): "
                            f"read it with --blocked")
    if QUESTION_COLLAPSED_RE.search(_dialog_area(screen)):      # only while collapsed: shift+left on an open one moves on
        herdr(cfg, link["host"], orch, "pane", "send-keys", pane, "shift+left", want_json=False)
        _log_answer(task_id, link, orch, key="shift+left")
        time.sleep(0.5)
        screen = _screen(cfg, link, orch)
    lines = [x for x in screen.splitlines() if x.strip()]
    return "\n".join(lines[-30:])


def steer(task_id, text, cfg, orch, active):
    """Send one instruction to a worker that is working right now (Codex folds it into the
    running turn); logged in the link. Never retried: a timeout doesn't prove it wasn't sent."""
    text = " ".join(text.split())
    if not text or len(text) > 2000:
        raise DispatchError("steer text must be 1-2000 characters")
    if text.startswith("-"):                    # herdr would read it as an option
        raise DispatchError("steer text must not start with '-'")
    if not any(t.get("task_id") == task_id for _, t, _ in active):
        raise DispatchError(f"{task_id} is not an open task: steer only open tasks (a closed one gets a new task)")
    link = load_link(task_id)
    if not link.get("agent"):
        raise DispatchError(f"{task_id} has no agent to steer")
    got = herdr(cfg, link["host"], orch, "agent", "get", link["agent"]).get("result", {})
    status = (got.get("agent") or got).get("agent_status")
    if status == "blocked":
        raise DispatchError(f"{link['agent']} is blocked on a dialog: answer it with --blocked/--answer, "
                            f"or --question/--reply for a Codex question")
    if status != "working":
        raise DispatchError(f"{link['agent']} is {status}, not working: send a normal prompt (a fix round) instead")
    _log_answer(task_id, link, orch, steer=text[:500], sent="pending")    # log before the send can fail
    herdr(cfg, link["host"], orch, "agent", "prompt", link["agent"], text)
    link["answers"][-1]["sent"] = "yes"
    got = herdr(cfg, link["host"], orch, "agent", "get", link["agent"]).get("result", {})
    after = (got.get("agent") or got).get("agent_status")
    if after not in ("working", "blocked"):     # it stopped between the check and the send: a new turn
        link["answers"][-1]["landed"] = after
        _log_answer_save(task_id, link)
        return (f"warning: {link['agent']} was {after} right after the send, so the steer may have started a new "
                f"turn before its last result was read: read it with --listen {task_id} --handoff; logged in its link")
    _log_answer_save(task_id, link)
    return f"steered {task_id} ({link['agent']} on {link['host']}); logged in its link"


def reply(task_id, text, cfg, orch):
    """Type a one-line answer into a worker's opened question and press enter; logged in the link."""
    text = " ".join(text.split())
    if not text or len(text) > 2000:
        raise DispatchError("reply must be 1-2000 characters of text")
    link, pane = _blocked_worker(task_id, cfg, orch)
    screen = _screen(cfg, link, orch)
    if dialog_kind(screen) != "question":
        raise DispatchError(f"{link['agent']} shows no open question (an approval or an unrecognised dialog): "
                            f"read it with --blocked")
    if QUESTION_COLLAPSED_RE.search(_dialog_area(screen)):
        raise DispatchError(f"the question is still collapsed (text would queue as a follow-up): "
                            f"open it first with --question {task_id}")
    herdr(cfg, link["host"], orch, "pane", "send-text", pane, text, want_json=False)
    _log_answer(task_id, link, orch, reply=text[:500], enter="pending")   # typed: log it before enter can fail
    herdr(cfg, link["host"], orch, "pane", "send-keys", pane, "enter", want_json=False)
    link["answers"][-1]["enter"] = "sent"
    _log_answer_save(task_id, link)
    return f"replied to {task_id} ({link['agent']} on {link['host']}); logged in its link"
