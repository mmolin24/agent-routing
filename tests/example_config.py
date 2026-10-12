#!/usr/bin/env python3
"""Dry-run the shipped mixed-project example without checkouts or live hosts."""
import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import route as R

SHA = "a" * 40
CONFIG = ROOT / "config/hosts.example.json"


def offline(*args, **kwargs):
    raise AssertionError(f"unexpected live call: {args!r}")


def dry_run(cfg, flags, host, path, branch):
    built = []
    original = R.build_task

    def build(args, config):
        task = original(args, config)
        built.append(task)
        return task

    out, err = io.StringIO(), io.StringIO()
    # Exact --commit skips ref resolution. --from mac identifies the example's
    # routing home explicitly; no active dir means no live load probes.
    argv = ["route.py", "--config", str(CONFIG), "--from", "mac", "--commit", SHA, "--json", *flags]
    with patch.object(sys, "argv", argv), patch.object(R, "build_task", build), \
            patch.object(R, "routing_ref", return_value="offline-example"), \
            patch.object(subprocess, "run", offline), patch.object(subprocess, "Popen", offline), \
            patch.object(socket, "create_connection", offline), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = R.main()
    assert rc == 0, (argv, out.getvalue(), err.getvalue())
    decision = json.loads(out.getvalue())
    assert decision["status"] == "routed", decision
    assert decision["selected_host"] == host, decision
    assert decision["execution_authorized"] is False
    assert "preflight" not in decision and "recorded" not in decision
    assert R.worktree_identity(built[0], cfg, host) == (path, branch)
    assert path in decision["handoff_note"]
    assert decision["plan"], decision
    print(f"PASS {built[0]['repository']['name']}: {host}, {path}, {branch}")
    return decision


def main():
    cfg = R.load_config(CONFIG)
    cases = [
        (["--issue", "APP-1", "--requires", "web", "--grant", "draft-pr"],
         "linux", "/home/you/projects/web-app/.worktrees/APP-1", "feature/APP-1"),
        (["--issue", "MOB-2", "--requires", "ios-simulator", "--grant", "verify-only"],
         "mac", "/Users/you/worktrees/mobile/MOB-2", "feature/MOB-2"),
        (["--repo", "infra", "--issue", "OPS-3", "--requires", "opentofu", "--grant", "local-only"],
         "linux", "/home/you/worktrees/infra/OPS-3", "feature/OPS-3"),
    ]
    for case in cases:
        dry_run(cfg, *case)
    # The web gate must actually use the advertised runtime and port slots.
    gate = dry_run(cfg, ["--issue", "APP-4", "--requires", "docker", "--grant", "gate"],
                   "linux", "/home/you/projects/web-app/.worktrees/APP-4", "feature/APP-4")
    assert gate["heavy"] and gate["port_slot"] == 1
    assert gate["env"] == {"API_HOST_PORT": "3010", "POSTGRES_HOST_PORT": "5442",
                           "FRONTEND_HOST_PORT": "8091", "APP_RUNTIME_IMAGE": "web-app-runtime:test-app4-gate"}
    print("4 cases, 0 failures")


if __name__ == "__main__":
    main()
