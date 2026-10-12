#!/usr/bin/env python3
"""Offline golden snapshots of the views.

python3 tests/golden_views.py --update   # regenerate deliberately reviewed goldens
python3 tests/golden_views.py            # compare, with unified diffs on failure

Only the standard library is needed. Synthetic contracts, closed results, marks,
links and heartbeat files live under a temporary ROOT. The collector's raw
report, PR lookup, Linear lookup, host load and Herdr are replaced; the real
gather enrichment, classification, rendering, event polling and reap selection
run unchanged. No worker is launched and no tab is closed. Unexpected process,
socket, HTTP or sleep calls fail even inside production's broad exception handlers.

Clock, UTC/local formatting, locale, mtimes, paths, terminal width, animation
frame and watch UI state are fixed. HTML is the complete render, not a browser
screenshot (its external font links are preserved without fetching them).
"""

import argparse
import contextlib
import copy
import difflib
import glob
import io
import json
import locale
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "scripts"))
import route as R  # noqa: E402
import dashboard as M  # noqa: E402
import dispatch as D  # noqa: E402
import events as E  # noqa: E402
import reap as RP  # noqa: E402
import watch as W  # noqa: E402

EPOCH = 1791806400  # 2026-10-12T12:00:00Z
HOUR = 3600
SHA = "6f63f93a" * 5
OLD_SHA = "9c842a7d" * 5
WIDTH = 180
REAL_GMTIME, REAL_STRFTIME, REAL_GLOB = time.gmtime, time.strftime, glob.glob


class OfflineCall(BaseException):
    """Cannot be swallowed by views' `except Exception` fallback paths."""


def offline(*args, **kwargs):
    raise OfflineCall(f"live system call in offline views harness: {args!r}")


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path, value, at=EPOCH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(value), encoding="utf-8")
    os.utime(path, (at, at))


def iso(at):
    return REAL_STRFTIME("%Y-%m-%dT%H:%M:%SZ", REAL_GMTIME(at))


def config():
    """Self-contained public inputs; never reads hosts.local.json or real HOME."""
    hosts = {}
    for host, maximum in (("desktop", 3), ("mac", 2)):
        hosts[host] = {
            "max_heavy": maximum, "max_queue": 6, "capabilities": ["web", "docker"],
            "herdr_bin": "$HOME/.local/bin/herdr", "path_prepend": [],
            "repos": {"example-app": {"path": f"/fixture/{host}/example-app"}},
            "reach": {host: {"mode": "local"},
                      "mac" if host == "desktop" else "desktop": {
                          "mode": "machine", "selector": "Desktop WSL" if host == "desktop" else "Mac",
                          "ssh_alias": "fixture-desktop" if host == "desktop" else "fixture-mac"}},
        }
    return {"routing_home": "desktop", "default_linux_host": "desktop", "hosts": hosts,
            "default_repo": "example-app",
            "projects": R.load_json(Path(__file__).resolve().parents[1] / "tests/fixtures/hosts.golden.json")["projects"],
            "known_agents": {"orchestrator": "orchestrator", "routing-maintainer": "routing maintainer"}}


def contract(task_id, host="desktop", heavy=False, summary=None):
    key = R.issue_key(task_id)
    task = {
        "contract_version": 1, "task_id": task_id, "summary": summary or f"{key}: Synthetic view task",
        "authorization": {"scope": "worktree-write", "granted_by": "offline fixture",
                          "allowed": ["edit and test in this worktree"], "forbidden": ["deploy"]},
        "routing": {"requested_host": host, "requires": ["web"]},
        "repository": {"name": "example-app", "remote": "https://github.com/example/example-app.git"},
        "project_commit": SHA, "selected_host": host,
        "worktree": {"strategy": "git-worktree", "path": f".worktrees/{key or task_id}",
                     "branch": f"feature/{key or task_id}"},
        "worker": {"kind": "codex", "name": task_id}, "mode": "agent",
        "resources": {"class": "heavy" if heavy else "light", "timeout_minutes": 120, "ports": []},
        "stop_condition": "Report evidence and stop; offline fixture only.",
    }
    errors = R.validate(task)
    if errors:
        raise AssertionError(f"invalid fixture {task_id}: {errors}")
    return task


def agent(name, host="desktop", status="working", role=None, **fields):
    key = R.issue_key(name)
    return {"name": name, "host": host, "status": status, "agent": "codex", "pane_id": f"p-{name}",
            "cwd": f"/fixture/{host}/example-app/.worktrees/{key or 'scratch'}",
            "model": "gpt-6.1-sol", "effort": "high", "active_at": EPOCH - 2 * HOUR,
            "parent": None, **({"role": role} if role else {}), **fields}


def fixture(root, cfg):
    """Two hosts; routed/hand-run work, complete pipelines and retained old sessions."""
    active = root / "runs/active"
    active.mkdir(parents=True)
    report = {"tasks": [], "untracked": [], "unreachable": [], "loads": {}, "usage": {
        "codex": {"plan": "pro", "at": EPOCH, "five_hour": {"used_percent": 32, "resets_at": EPOCH + HOUR},
                  "week": {"used_percent": 92, "resets_at": EPOCH + 48 * HOUR}, "credits": 12300},
        "claude": {"at": EPOCH - 7200, "five_hour": {"used_percent": 23, "resets_at": EPOCH + 3 * HOUR}},
    }}

    def open_task(task_id, host="desktop", status="working", heavy=False, minutes=45, **extra):
        task = contract(task_id, host, heavy, extra.pop("summary", None))
        link = {"dispatched_at": iso(EPOCH - minutes * 60)}
        task["link"] = link
        write_json(active / f"{task_id}.json", task, EPOCH - minutes * 60)
        write_json(root / "runs/tasks" / task_id / "link.json", link)
        write_json(root / "runs/tasks" / task_id / "decision.json", {"preflight": {"boot_id": f"{host}-boot-1"}})
        row = {"task_id": task_id, "host": host, "summary": task["summary"], "heavy": heavy,
               "dispatch": "dispatched", "agent": task_id, "workspace_id": f"w-{task_id}",
               "group": R.issue_key(task_id) or task_id, "tab_label": R.task_step(task_id),
               "agents": [agent(task_id, host, status, "worker", active_at=EPOCH - 60)] if status else [], **extra}
        report["tasks"].append(row)
        return row

    impl = open_task("app9001-impl", minutes=135, summary='APP-9001: Widget <preview> & "quotes"',
                     git={"unpushed": 3, "oldest_unpushed": EPOCH - 70 * 60, "pushed": True, "dirty": 2, "stash": 1})
    impl["agents"] += [agent("app9001-review-child", role="child", parent="p-app9001-impl", agent="claude",
                              model="claude-opus-5-5", effort="medium", fast=True),
                        agent("app9001-test-child", status="done", role="child", parent="p-app9001-review-child")]
    impl["agents"][1]["subagents"] = [
        {"id": "unit", "description": "Check widget boundaries", "running": True, "depth": 1, "type": "explore"},
        {"id": "edge", "description": "Check nested edge case", "running": False, "depth": 2, "parent": "unit"}]
    open_task("app9002-gate", host="mac", status="blocked", heavy=True)
    open_task("app9011-impl", host="mac", minutes=90)
    open_task("app9012-impl", status=None, heavy=True, minutes=20)
    # Recorded only: no link and no worker, so the real no_worker_age takes its other branch.
    standalone = open_task("standalone-audit", status=None, minutes=25, summary="Audit routing docs")
    standalone.update(dispatch="open", agent=None)
    standalone_task = R.load_json(active / "standalone-audit.json")
    standalone_task.pop("link")
    write_json(active / "standalone-audit.json", standalone_task, EPOCH - 25 * 60)
    (root / "runs/tasks/standalone-audit/link.json").unlink()

    def closed(number, step, outcome, hours=2, sha=SHA):
        task = contract(f"app{number}-{step}")
        task.update(closed_at=EPOCH - hours * HOUR, outcome=outcome, project_commit=sha)
        write_json(root / "runs/closed" / f"{task['task_id']}.json", task)

    def marks(number, **states):
        steps = {name: {"state": state, "at": EPOCH - (i + 1) * 600, "sha": SHA,
                        "note": f"offline {name} evidence"} for i, (name, state) in enumerate(states.items())}
        write_json(root / "runs/issues" / f"APP-{number}.json", {"issue": f"APP-{number}", "steps": steps})

    marks(9001, review="approved")  # invalidated by the current fix round
    for n in (9002, 9003, 9004, 9005, 9006, 9007, 9009, 9010):
        closed(n, "impl", "done", hours=n - 9000)
    closed(9002, "review", "approved")
    for n in (9003, 9004, 9005, 9006, 9007):
        marks(n, review="APPROVE", gate="failed" if n == 9004 else "passed")
    marks(9006, review="approved", gate="passed", deploy="deployed", linear="in-progress")
    marks(9007, review="approved", gate="passed", deploy="deployed", staging="verified")
    closed(9009, "review", "approved", sha=OLD_SHA)  # stale PR head
    closed(9015, "impl", "done", hours=15 * 24)  # outside the issue view's 14-day retention
    marks(9016, linear="done")
    # Quiet deployment mark: merged, but awaiting deployment completion.
    write_json(root / "runs/issues/APP-9005.json", {"issue": "APP-9005", "steps": {
        "review": {"state": "approved", "at": EPOCH - 3 * HOUR, "sha": SHA},
        "gate": {"state": "passed", "at": EPOCH - 2 * HOUR, "sha": SHA},
        "deploy": {"state": "deploying", "at": EPOCH - HOUR, "note": "waiting for rollout"}}})
    # Malformed records must remain harmless; older unknown records don't resurrect rows.
    for directory in ("closed", "issues"):
        path = root / "runs" / directory / "broken.json"
        path.write_text("{broken", encoding="utf-8")
        os.utime(path, (EPOCH, EPOCH))

    queues = {"desktop": ["app9009-gate"], "mac": ["app9013-impl", "app9013-gate"]}
    for host, ids in queues.items():
        for index, task_id in enumerate(ids):
            write_json(root / "runs/queue" / host / f"{EPOCH * 10**9 + index}-{task_id}.json",
                       contract(task_id, host, heavy=True))

    report["untracked"] = [
        agent("orchestrator"),
        agent("routing-maintainer", status="idle"),
        agent("scratch-helper", "mac", profile_shared=True),
        agent("app9005-impl", status="done"), agent("app9006-impl", "mac", "idle"),
        agent("app9007-impl", status="done"), agent("app9007-child", status="done", parent="p-app9007-impl"),
        agent("app9015-impl", status="idle", active_at=EPOCH - 40 * HOUR),
        agent("app9016-impl", "mac", "done"),
        agent("app9001-old-impl", status="idle"),  # kept while an open task exists
        agent("app9009-old-gate", status="done"),  # kept while a queued task exists
        agent("app9017-impl", status="done", active_at=None),
        agent("app9018-impl", status="idle", active_at=EPOCH - HOUR),
    ]
    capacity = {}
    for host, hc in cfg["hosts"].items():
        load = {"cores": 16 if host == "desktop" else 15, "load1": 4.0 if host == "desktop" else 12.0,
                "mem_total": 24 * 2**30, "mem_avail": 16 * 2**30, "swap_total": 4 * 2**30,
                "swap_used": 0, "pressure": "normal", "docker": "up", "docker_used": 2 * 2**30,
                "docker_limit": 14 * 2**30, "containers": 3, "boot_id": f"{host}-boot-1",
                "boot_at": EPOCH - 48 * HOUR, "disk_path": "/mnt/c" if host == "desktop" else "/",
                "disk_free": 80 * 2**30, "disk_total": 512 * 2**30}
        report["loads"][host] = load
        on = [t for t in report["tasks"] if t["host"] == host]
        capacity[host] = {"heavy": sum(t["heavy"] for t in on), "light": sum(not t["heavy"] for t in on),
                          "max_heavy": hc["max_heavy"], "max_queue": hc["max_queue"], "load": load,
                          "levels": R.load_levels(load), "slots": {"holders": [
                              {"task": "app9002-gate", "pid": 4242, "cmd": "pnpm ready:check 4002"}] if host == "mac" else [],
                              "waiting": [{"task": "app9001-impl", "pid": 4243, "cmd": "pnpm typecheck"}] if host == "desktop" else []}}

    prs = {}
    for n in (9001, 9002, 9003, 9004, 9005, 9006, 9007, 9009, 9010):
        prs[f"feature/APP-{n}"] = {
            "number": n - 5000, "state": "merged" if n in (9005, 9006, 9007) else "open",
            "draft": n == 9001, "url": f"https://github.com/example/example-app/pull/{n - 5000}",
            "title": f"APP-{n}: Pipeline fixture", "head": SHA, "base": "feature/APP-9005" if n == 9010 else "main",
            "updated_at": iso(EPOCH - (30 if n == 9001 else 1) * HOUR),
            "checks": R.checks_summary([{"conclusion": "SUCCESS"},
                                       {"conclusion": "FAILURE" if n == 9009 else "SUCCESS"}])}
    report["untracked"][0]["subagents"] = [{"id": "review",
        "description": f"APP-9010 review #{prs['feature/APP-9010']['number']}", "running": True}]
    prs["chore/docs"] = {"number": 4999, "state": "open", "draft": False,
                         "url": "https://github.com/example/example-app/pull/4999", "title": "Refresh docs",
                         "head": SHA, "base": "main", "updated_at": iso(EPOCH),
                         "checks": {"pass": 0, "fail": 0, "pending": 1}}
    linear = {"APP-9002": "in-progress", "APP-9005": "in-review", "APP-9006": "in-progress",
              "APP-9007": "done", "APP-9009": "todo"}
    write_json(root / "runs/feedback/new.json", {"status": "new"})
    write_json(root / "runs/feedback/done.json", {"status": "done", "done_at": EPOCH - HOUR})
    heartbeat = root / "runs/.events-state.json.heartbeat"
    heartbeat.write_text(str(EPOCH - 20 * 60), encoding="utf-8")
    os.utime(heartbeat, (EPOCH - 20 * 60, EPOCH - 20 * 60))
    return {"raw": (report, queues, capacity), "prs": prs, "linear": linear, "signing": "unlocked"}


def changed(state, now):
    """Worker stops, CI recovers, PR merges/head moves, and the Mac reboots.

    The gate pane models a native session restored as done after reboot; its
    earlier session timestamp remains, but mark_interrupted still takes priority.
    """
    state = copy.deepcopy(state)
    report, _, cap = state["raw"]
    for t in report["tasks"]:
        if t["task_id"] in ("app9001-impl", "app9002-gate"):
            t["agents"][0].update(status="done", active_at=now - 35 * 60)
        elif t["host"] == "mac":
            t["agents"] = []
    load = report["loads"]["mac"]
    load.update(boot_id="mac-boot-2", boot_at=now - 5 * 60, docker="down", pressure="critical")
    cap["mac"]["levels"] = R.load_levels(load)
    state["prs"]["feature/APP-9003"]["state"] = "merged"
    state["prs"]["feature/APP-9009"]["checks"] = {"pass": 2, "fail": 0, "pending": 0}
    state["prs"]["feature/APP-9002"]["head"] = OLD_SHA
    state["linear"]["APP-9009"] = "in-progress"
    state["signing"] = "locked"
    return state


@contextlib.contextmanager
def sandbox(root, state, clock):
    """Patch live boundaries only; assertions cannot quietly become degraded views."""
    with contextlib.ExitStack() as stack:
        replacements = {
            "ROOT": str(root), "QUEUE_DIR": str(root / "runs/queue"),
            "CLOSED_DIR": str(root / "runs/closed"), "ISSUES_DIR": str(root / "runs/issues"),
            "FEEDBACK_DIR": str(root / "runs/feedback"), "detect_orchestrator": lambda cfg: "desktop",
            "gather_records": lambda *args: copy.deepcopy(state["raw"]), "gather_python": offline,
            "collector_exe": lambda: None, "host_load": lambda cfg, host, orch: copy.deepcopy(state["raw"][0]["loads"].get(host)),
            "pr_states": lambda checkout: copy.deepcopy(state["prs"]),
            "linear_states": lambda keys: None if state["linear"] is None else {
                k: v for k, v in state["linear"].items() if k in keys},
            "signing_state": lambda: state["signing"], "run_dispatch": offline,
        }
        for name, value in replacements.items():
            stack.enter_context(patch.object(R, name, value))
        stack.enter_context(patch.object(D, "herdr", offline))
        stack.enter_context(patch.object(E, "STATE_FILE", str(root / "runs/.events-state.json")))
        stack.enter_context(patch.dict(os.environ, {"HOME": "/fixture/home", "AGENT_COLLECT": "0",
                                                  "ROUTE_EVENTS_STATE": str(root / "runs/.events-state.json"),
                                                  "ROUTE_SNAPSHOT_LOCAL": "1", "TZ": "UTC"}))
        old_locale = locale.setlocale(locale.LC_TIME)
        locale.setlocale(locale.LC_TIME, "C")
        stack.callback(locale.setlocale, locale.LC_TIME, old_locale)
        stack.enter_context(patch.object(time, "time", lambda: clock[0]))
        stack.enter_context(patch.object(time, "time_ns", lambda: clock[0] * 10**9))
        for name in ("gmtime", "localtime"):
            stack.enter_context(patch.object(time, name, lambda seconds=None: REAL_GMTIME(clock[0] if seconds is None else seconds)))
        stack.enter_context(patch.object(time, "strftime", lambda fmt, tm=None: REAL_STRFTIME(fmt, REAL_GMTIME(clock[0]) if tm is None else tm)))
        stack.enter_context(patch.object(time, "sleep", offline))
        stack.enter_context(patch.object(glob, "glob", lambda *a, **kw: sorted(REAL_GLOB(*a, **kw))))
        stack.enter_context(patch.object(shutil, "get_terminal_size", lambda *a, **kw: os.terminal_size((WIDTH, 60))))
        stack.enter_context(patch.object(W, "c", lambda text, *a, **kw: str(text)))
        stack.enter_context(patch.object(W, "RESET", ""))
        stack.enter_context(patch.object(W, "OPEN", {"APP-9001", "APP-9009"}))
        stack.enter_context(patch.object(W, "SEL", {"key": None}))
        stack.enter_context(patch.object(W, "EXPAND", False))
        stack.enter_context(patch.object(W, "pinned", {"n": W.PINNED}))
        for name in ("run", "Popen", "call", "check_call", "check_output"):
            stack.enter_context(patch.object(subprocess, name, offline))
        for name in ("socket", "socketpair", "create_connection", "getaddrinfo", "gethostname", "gethostbyname", "gethostbyname_ex"):
            stack.enter_context(patch.object(socket, name, offline))
        for name in ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "spawnl", "spawnv", "execv", "execve"):
            if hasattr(os, name):
                stack.enter_context(patch.object(os, name, offline))
        stack.enter_context(patch.object(urllib.request, "urlopen", offline))
        stack.enter_context(patch.object(urllib.request.OpenerDirector, "open", offline))
        yield


def capture(call):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = call()
    if rc != 0 or err.getvalue():
        raise AssertionError(f"entry point failed: rc={rc}, stderr={err.getvalue()!r}, stdout={out.getvalue()!r}")
    return out.getvalue()


def legacy_issue_rows(rows):
    """Keep the existing JSON golden contract; project_views checks added metadata.

    Renderers and events still receive the complete rows, so the old text and
    HTML snapshots exercise the profile-driven pipeline without normalization.
    """
    return [{k: v for k, v in row.items() if k not in ("project", "pipeline", "tracker", "tracker_flow")}
            for row in rows]


def snapshots():
    outputs = {}
    with tempfile.TemporaryDirectory(prefix="golden-views-") as tmp:
        root, cfg, clock = Path(tmp), config(), [EPOCH]
        write_json(root / "config/hosts.example.json", cfg)
        state = fixture(root, cfg)
        for scenario in ("initial", "changed", "degraded"):
            if scenario == "changed":
                clock[0] += HOUR
                state = changed(state, clock[0])
            elif scenario == "degraded":
                clock[0] += 60
                state = copy.deepcopy(state)
                state.update(prs=None, linear=None)
                state["raw"][0]["unreachable"] = ["mac: synthetic SSH timeout"]
            with sandbox(root, state, clock):
                snap_dict = R.snapshot(cfg, "desktop", str(root / "runs/active"))
                snap = (snap_dict["report"], snap_dict["queues"], snap_dict["capacity"])
                if any(u.startswith("issue view failed") for u in snap[0]["unreachable"]):
                    raise AssertionError(snap[0]["unreachable"])
                outputs[f"issues-{scenario}.json"] = dump(legacy_issue_rows(snap[0]["issues"]))
                # --status has no standalone formatter; run its real CLI entry point.
                with patch.object(sys, "argv", ["route.py", "--status", "--config", str(root / "config/hosts.example.json"),
                                               "--active-dir", str(root / "runs/active")]):
                    outputs[f"status-{scenario}.txt"] = capture(R.main)
                outputs[f"watch-{scenario}.txt"] = "\n".join(
                    W.frame_lines(copy.deepcopy(snap), cfg, frame=0, width=WIDTH, every=5, age=0, error=None)) + "\n"
                outputs[f"dashboard-{scenario}.html"] = M.render(cfg, *copy.deepcopy(snap), refresh=5)
                outputs[f"reap-{scenario}.json"] = dump(RP.candidates(snap[0], snap[1]))
                # Two real consecutive polls retain state on disk. A third confirms
                # unavailable PR/tracker data is carried forward, not re-announced.
                outputs[f"events-{scenario}.txt"] = capture(lambda: E.run(lambda: copy.deepcopy(snap), every=5, follow=False, timeout=60))
                # Patching time.time does not change filesystem write timestamps.
                # Pin the real poll's files before the next gather reads their ages.
                for suffix in ("", ".heartbeat"):
                    os.utime(str(root / "runs/.events-state.json") + suffix, (clock[0], clock[0]))
                if scenario == "initial":
                    with patch.object(W, "EXPAND", True):
                        outputs["watch-expanded.txt"] = "\n".join(
                            W.frame_lines(copy.deepcopy(snap), cfg, frame=0, width=WIDTH, every=5, age=0, error=None)) + "\n"
                    without_tracker = copy.deepcopy(state)
                    without_tracker["linear"] = {}
                    with sandbox(root, without_tracker, clock):
                        outputs["issues-no-tracker.json"] = dump(legacy_issue_rows(
                            R.snapshot(cfg, "desktop", str(root / "runs/active"))['report']['issues']))
        # Only ephemeral ROOT is normalized; fixed fixture paths remain visible inputs.
        return {name: text.replace(str(root), "<ROOT>") for name, text in outputs.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="write reviewed baseline snapshots")
    parser.add_argument("--golden-dir", type=Path, default=ROOT / "tests/golden/views")
    args = parser.parse_args()
    try:
        actuals = snapshots()
    except (Exception, OfflineCall) as exc:
        print(f"FAIL snapshot generation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    failures = 0
    for name, actual in actuals.items():
        path = args.golden_dir / name
        try:
            if args.update:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(actual, encoding="utf-8")
                print(f"UPDATE {name}")
                continue
            expected = path.read_text(encoding="utf-8")
            if actual == expected:
                print(f"PASS {name}")
                continue
            print(f"FAIL {name}")
            print("".join(difflib.unified_diff(expected.splitlines(keepends=True), actual.splitlines(keepends=True),
                                             fromfile=str(path), tofile=name + " (actual)")), end="")
        except OSError as exc:
            print(f"FAIL {name}: {exc}")
        failures += 1
    # Like golden_routing, removed cases require explicit review; never delete on --update.
    for path in sorted(args.golden_dir.glob("*")):
        if path.is_file() and path.name not in actuals:
            print(f"FAIL obsolete golden: {path.name} (remove explicitly after reviewing the case list)")
            failures += 1
    print(f"{len(actuals)} snapshots, {failures} failures")
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
