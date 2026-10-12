#!/usr/bin/env python3
"""Offline snapshots of the real routing CLI and its project-dependent helpers.

Run normally to compare, or --update to accept deliberately reviewed changes.
--config defaults to the frozen public fixture; keep private snapshots in
--golden-dir tests/golden.local/routing. Only the standard library is needed.

Live boundaries are replaced, not routing logic: ref resolution, preflight,
load collection, routing-repo identity, host detection, and clocks. ROOT and
all state directories point into a fresh temporary directory per case. No
dispatch is executed; plan() renders its Herdr commands. Subprocess and socket
entry points fail closed so a new live dependency cannot silently reach a host.
Configured project paths are preserved (they are inputs); only this checkout,
temporary paths and the synthetic preflight home are normalized.
"""

import argparse
import contextlib
import copy
import difflib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "scripts"))
import route as R  # noqa: E402

EPOCH = 1791806400  # Fixed UTC clock; never depends on TZ or file creation time.
SHA = "1234567890abcdef1234567890abcdef12345678"
REAL_GMTIME = time.gmtime


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(value), encoding="utf-8")
    os.utime(path, (EPOCH, EPOCH))


def offline(*args, **kwargs):
    raise AssertionError(f"live system call in offline golden harness: {args!r}")


def matrix(cfg):
    """Names are stable and also serve as the coverage inventory in --help/output."""
    cases = []

    def issue(name, flags=(), **fixtures):
        cases.append(dict(name=name, argv=["--issue", "APP-9999", *flags], **fixtures))

    for grant in sorted(cfg["grants"]):
        issue("grant-" + grant, ["--grant", grant])
    variants = {
        "web": ["--requires", "web"],
        "docker": ["--requires", "docker"],
        "compose": ["--requires", "docker", "--compose"],
        "suffix-web-desktop": ["--requires", "web", "--suffix", "web", "--host", "desktop"],
        "explicit-mac": ["--requires", "web", "--host", "mac"],
        "blocked-explicit-desktop": ["--requires", "ios-simulator", "--host", "desktop"],
        "docker-light": ["--requires", "docker", "--light"],
        "web-heavy": ["--requires", "web", "--heavy"],
        "model-effort": ["--model", "gpt-6.1-sol", "--effort", "medium"],
        "claude-model-effort": ["--kind", "claude", "--model", "sonnet", "--effort", "high"],
        "infra-worktree": ["--repo", "example-infra", "--requires", "opentofu"],
        "exact-commit": ["--commit", SHA],
        "deps-install-ports": ["--deps", "install", "--ports", "8181,8282"],
    }
    for name, flags in variants.items():
        issue(name, ["--grant", "local-only", *flags])
    issue("ios-native", ["--grant", "verify-only", "--requires", "ios-simulator",
                         "--suffix", "native", "--host", "mac"])
    issue("android", ["--grant", "verify-only", "--requires", "android-emulator"])
    issue("gate-allow-behind", ["--grant", "gate", "--allow-behind"], behind=3)
    issue("gate-behind-blocked", ["--grant", "gate"], behind=3)
    issue("review-role", ["--grant", "review", "--role", "security",
                          "--commit", "origin/feature/APP-9999"])
    issue("heavy-over-cap", ["--grant", "local-only", "--heavy", "--host", "desktop"], active="full")
    issue("heavy-load-shared", ["--grant", "local-only", "--heavy", "--requires", "web"], active="full")
    issue("compose-slot-two", ["--grant", "local-only", "--compose", "--host", "desktop"], active="slot")
    issue("heavy-queue-fifo", ["--grant", "local-only", "--heavy", "--host", "desktop"], queued=True)
    issue("hot-heavy", ["--grant", "local-only", "--heavy", "--host", "desktop"], hot=True)
    issue("hot-light", ["--grant", "local-only", "--light", "--host", "desktop"], hot=True)
    issue("dry-run-from-mac", ["--grant", "draft-pr", "--requires", "web", "--host", "desktop"], orch="mac", record=False)
    issue("dry-run-no-preflight", ["--grant", "local-only"], preflight=False, record=False)

    for path in sorted((ROOT / "examples" / "tasks").iterdir()):
        if path.is_file():
            cases.append(dict(name="example-" + path.stem, task=R.load_json(path),
                              source="examples/tasks/" + path.name))

    # Task-file inputs beyond the examples, including a workspace with no APP key.
    base = R.load_json(ROOT / "examples/tasks/linux-unit-tests.json")
    task = copy.deepcopy(base)
    task.update(task_id="standalone-check", summary="Standalone command with a server pane")
    task["worker"]["name"] = "standalone-check"
    task["worktree"] = {"strategy": "git-worktree", "path": ".worktrees/standalone",
                        "branch": "checks/standalone"}
    task["commands"].append({"cwd": "<WORKTREE>/frontend", "run": "pnpm dev --port 8181",
                             "mutating": False, "long_running": True, "ready_regex": "Ready on"})
    task["resources"]["ports"] = [8181]
    cases.append(dict(name="json-no-issue-server", task=task))
    task = copy.deepcopy(base)
    task["commands"] = [{"run": "pnpm install", "mutating": True}]
    cases.append(dict(name="json-linked-install-blocked", task=task))
    task = copy.deepcopy(base)
    task["project_commit"] = "origin/main"
    cases.append(dict(name="json-invalid-commit", task=task))
    return cases


def fake_active(cfg, root, kind):
    """Valid contracts: real load_active/collisions/stale_entries read these."""
    count = cfg["hosts"]["desktop"].get("max_heavy", 3) if kind == "full" else 1
    for n in range(count):
        task = copy.deepcopy(R.load_json(ROOT / "examples/tasks/linux-unit-tests.json"))
        task.update(task_id=f"fixture-{n}", selected_host="desktop")
        task["worker"]["name"] = f"fixture-{n}"
        task["worktree"]["issue"] = f"APP-{8000 + n}"
        task["resources"] = {"class": "heavy", "timeout_minutes": 120, "ports": []}
        if kind == "slot":
            task["resources"].update(runtime="compose", port_slot=1)
        write_json(root / "runs/active" / (task["task_id"] + ".json"), task)


def snapshot(case, cfg, config_name):
    with tempfile.TemporaryDirectory(prefix="golden-routing-") as tmp:
        root = Path(tmp)
        active = root / "runs/active"
        active.mkdir(parents=True)
        cfg_path = root / "config" / config_name
        write_json(cfg_path, cfg)
        if case.get("active"):
            fake_active(cfg, root, case["active"])
        if case.get("queued"):
            task = copy.deepcopy(R.load_json(ROOT / "examples/tasks/linux-unit-tests.json"))
            task["task_id"] = "fixture-waiting"
            task["worker"]["name"] = "fixture-waiting"
            task["worktree"]["issue"] = "APP-863"
            task["resources"]["class"] = "heavy"
            write_json(root / "runs/queue/desktop" / f"{EPOCH * 10**9}-fixture-waiting.json", task)

        seen = {"preflight_calls": [], "resolve_calls": []}
        original_build, original_decide = R.build_task, R.decide

        def build(a, config):
            task = original_build(a, config)
            seen["task_before_decide"] = copy.deepcopy(task)
            return task

        def decide(a, config, path, task):
            seen.setdefault("task_before_decide", copy.deepcopy(task))
            result = original_decide(a, config, path, task)
            seen["task_after_decide"] = copy.deepcopy(task)
            # Keep the returned object: main() adds status, handoff and recording.
            seen["decision"] = result
            return result

        def resolve(config, host, orch, task):
            ref = task.pop("_commit_ref")
            seen["resolve_calls"].append({"host": host, "orchestrator": orch, "ref": ref})
            if not R.BRANCH_RE.fullmatch(ref):
                return f"bad --commit ref {ref!r}"
            task["project_commit"] = SHA
            return None

        def preflight(argv, stdin_path):
            seen["preflight_calls"].append({"argv": argv, "stdin_path": stdin_path})
            args = shlex.split(argv[-1])[3:] if stdin_path else argv[2:]
            repo = args[args.index("--repo") + 1]
            host = next(h for h, hc in cfg["hosts"].items()
                        if any(rc["path"] == repo for rc in hc["repos"].values()))
            behind = case.get("behind", 0)
            blocked = behind and "--behind-ref" in args and "--behind-warn" not in args
            result = {"preflight_status": "blocked" if blocked else "ok",
                    "blockers": f"commit is {behind} commits behind origin/main" if blocked else "",
                    "commit_behind_ref": f"origin/main {behind}",
                    "home": f"/golden-home/{host}",
                    "kernel": "Microsoft WSL2" if host == "desktop" else "Darwin",
                    "repo_last_fetch": "2026-10-12T12:00:00Z",
                    "repo_dirty_tracked": "0", "repo_untracked": "0",
                    "pnpm_version": "11.0.8" if host == "mac" else "10.33.2"}
            if any(rc["path"] == repo and rc.get("issue_worktree") == "pnpm-worktree-issue"
                   for rc in cfg["hosts"][host]["repos"].values()):
                result["repo_pnpm_pin"] = "10.33.2"
            return result

        def load(config, host, orch):
            return {"cores": 16, "load1": 32.0 if case.get("hot") and host == "desktop" else 1.0,
                    "mem_total": 24 * 2**30, "mem_avail": 16 * 2**30,
                    "swap_total": 4 * 2**30, "swap_used": 0, "pressure": "normal",
                    "docker": "up", "docker_used": 2 * 2**30, "docker_limit": 14 * 2**30}

        argv = list(case.get("argv", []))
        if "task" in case:
            task_path = root / case.get("source", "inputs/" + case["name"] + ".json")
            write_json(task_path, case["task"])
            argv = [str(task_path)]
        argv += ["--config", str(cfg_path), "--active-dir", str(active), "--json"]
        if case.get("record", True):
            argv.append("--record")
        if case.get("preflight", True):
            argv.append("--run-preflight")
        if case.get("orch"):
            argv += ["--from", case["orch"]]

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            replacements = {"ROOT": str(root), "PREFLIGHT": str(root / "scripts/preflight.sh"),
                            "QUEUE_DIR": str(root / "runs/queue"), "CLOSED_DIR": str(root / "runs/closed"),
                            "ISSUES_DIR": str(root / "runs/issues"), "FEEDBACK_DIR": str(root / "runs/feedback"),
                            "routing_ref": lambda: "<ROUTING_REF>", "detect_orchestrator": lambda cfg: "desktop",
                            "resolve_ref": resolve, "run_preflight": preflight, "host_load": load,
                            "collector_exe": lambda: None, "build_task": build, "decide": decide,
                            "run_dispatch": offline}
            for name, value in replacements.items():
                stack.enter_context(patch.object(R, name, value))
            stack.enter_context(patch.object(sys, "argv", [str(root / "scripts/route.py"), *argv]))
            stack.enter_context(patch.dict(os.environ, {"HOME": "/golden-home", "AGENT_COLLECT": "0"}))
            stack.enter_context(patch.object(time, "time", lambda: EPOCH))
            stack.enter_context(patch.object(time, "time_ns", lambda: EPOCH * 10**9))
            stack.enter_context(patch.object(time, "gmtime", lambda seconds=None: REAL_GMTIME(EPOCH if seconds is None else seconds)))
            for name in ("run", "Popen", "call", "check_call", "check_output"):
                stack.enter_context(patch.object(subprocess, name, offline))
            for name in ("socket", "create_connection", "getaddrinfo", "gethostname"):
                stack.enter_context(patch.object(socket, name, offline))
            stack.enter_context(patch.object(os, "system", offline))
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = R.main()

            d = seen.get("decision")
            if d:
                human = io.StringIO()
                with contextlib.redirect_stdout(human):
                    R.print_human(d)
                seen["human_output"] = human.getvalue()
                task, host = seen["task_after_decide"], d["selected_host"]
                if host and task["repository"]["name"] in cfg["hosts"][host]["repos"]:
                    # Even an early-return decision must freeze the helper behavior.
                    helpers = {"worktree_dir": R.worktree_dir(task, cfg, host),
                               "worktree_identity": R.worktree_identity(task, cfg, host),
                               "setup_command": R.setup_command(task, cfg, host),
                               "herdr_group": R.herdr_group(task["task_id"]),
                               "issue_key": R.issue_key(task["task_id"]),
                               "task_step": R.task_step(task["task_id"])}
                    if "plan" not in d:
                        helpers["plan"] = R.plan(task, cfg, host, d["orchestrator_host"], d.get("env"))
                    if "handoff_note" not in d:
                        helpers["handoff_note"] = R.handoff_note(task, d, cfg, host)
                    if task["resources"].get("runtime") == "compose":
                        spec = cfg["hosts"][host]["repos"][task["repository"]["name"]].get("port_slots")
                        if spec:
                            helpers["runtime_env"] = R.runtime_env(task, cfg, host, d.get("port_slot") or 1)
                    seen["helpers"] = helpers

        artifacts = {}
        for path in sorted((root / "runs").rglob("*")):
            if path.is_file():
                artifacts[str(path.relative_to(root))] = (R.load_json(path) if path.suffix == ".json"
                                                         else path.read_text(encoding="utf-8"))
        seen.update(argv=argv, exit_code=rc, stderr=stderr.getvalue(), artifacts=artifacts)
        # JSON is captured semantically (sorted keys); human output preserves order.
        seen["cli_output"] = json.loads(stdout.getvalue())
        output = dump(seen)
        for path in sorted({str(root), str(ROOT)}, key=len, reverse=True):
            output = output.replace(path, "<ROOT>")
        output = output.replace("/golden-home", "<HOME>")
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="write reviewed baseline snapshots")
    parser.add_argument("--config", type=Path, default=ROOT / "tests/fixtures/hosts.golden.json")
    parser.add_argument("--golden-dir", type=Path, default=ROOT / "tests/golden/routing")
    args = parser.parse_args()
    try:
        cfg = R.load_json(args.config)
        cases = matrix(cfg)
    except (OSError, ValueError, KeyError) as exc:
        print(f"FAIL configuration/matrix: {exc}", file=sys.stderr)
        return 1
    # A temporary copy of private config has the same snapshot identity as the
    # private config itself; its source filename is not routing behavior.
    # Preserve the original example filename in committed snapshots.
    config_name = "hosts.example.json" if args.config.resolve() == (ROOT / "tests/fixtures/hosts.golden.json").resolve() else "hosts.local.json"
    failed = 0
    for case in cases:
        path = args.golden_dir / (case["name"] + ".txt")
        try:
            actual = snapshot(case, cfg, config_name)
            if args.update:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(actual, encoding="utf-8")
                print(f"UPDATE {case['name']}")
                continue
            expected = path.read_text(encoding="utf-8")
            if actual == expected:
                print(f"PASS {case['name']}")
                continue
            print(f"FAIL {case['name']}")
            print("".join(difflib.unified_diff(expected.splitlines(keepends=True), actual.splitlines(keepends=True),
                                             fromfile=str(path), tofile=case["name"] + " (actual)")), end="")
        except Exception as exc:
            print(f"FAIL {case['name']}: {type(exc).__name__}: {exc}")
        failed += 1
    # New or removed cases must be reviewed, too; --update never deletes files.
    extra = sorted(p.name for p in args.golden_dir.glob("*.txt")
                   if p.stem not in {case["name"] for case in cases})
    for name in extra:
        print(f"FAIL obsolete golden: {name} (remove explicitly after reviewing the case list)")
    failed += len(extra)
    print(f"{len(cases)} cases, {failed} failures")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
