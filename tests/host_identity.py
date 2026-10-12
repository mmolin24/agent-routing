#!/usr/bin/env python3
"""Offline regression checks for arbitrary configured machine IDs."""
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import route as R
import dashboard as D
import watch as W


def config():
    cfg = R.load_config(ROOT / "config/hosts.example.json")
    linux, mac = cfg["hosts"]["linux"], cfg["hosts"]["mac"]
    cfg["hosts"] = {"build1": linux, "build2": copy.deepcopy(linux), "laptop": mac}
    cfg["routing_home"] = "build2"
    cfg["default_linux_host"] = "build1"
    for host, info in cfg["hosts"].items():
        info["match"] = {"hostname": host}
        info["label"] = {"build1": "First builder", "build2": "Routing home", "laptop": "Mobile lab"}[host]
        info["reach"] = {h: {"mode": "local"} if h == host else {
            "mode": "ssh", "ssh_alias": h} for h in cfg["hosts"]}
    return cfg


class HostIdentity(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_short_hostname_and_env_precedence(self):
        for host in ("build1", "build2"):
            with patch.object(R.H.socket, "gethostname", return_value=host + ".example.test"), \
                    patch.object(R.H.platform, "system", return_value="Linux"):
                self.assertEqual(R.detect_orchestrator(self.cfg), host)
        with patch.object(R.H.socket, "gethostname", return_value="build1"), \
                patch.dict(os.environ, ROUTE_HOST="build2"):
            self.assertEqual(R.detect_orchestrator(self.cfg), "build2")

    def test_bad_env_never_falls_back(self):
        for value in ("desktop", ""):
            with patch.dict(os.environ, ROUTE_HOST=value), self.assertRaisesRegex(
                    R.Invalid, "ROUTE_HOST: unknown host.*build1, build2, laptop"):
                R.detect_orchestrator(self.cfg)

    def test_os_fallback_and_ambiguous_identity(self):
        with patch.object(R.H.socket, "gethostname", return_value="unconfigured"), \
                patch.object(R.H.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(R.Invalid, "ambiguous hosts: build1, build2.*set ROUTE_HOST or hosts.<h>.match.hostname"):
                R.detect_orchestrator(self.cfg)
            del self.cfg["hosts"]["build2"]
            self.assertEqual(R.detect_orchestrator(self.cfg), "build1")
        for system, configured in (("Darwin", "macos"), ("Darwin", "darwin")):
            self.cfg["hosts"]["laptop"]["os"] = configured
            with patch.object(R.H.platform, "system", return_value=system), \
                    patch.object(R.H.socket, "gethostname", return_value="unconfigured"):
                self.assertEqual(R.detect_orchestrator(self.cfg), "laptop")
        with patch.object(R.H.platform, "system", return_value="Windows"), \
                patch.object(R.H.socket, "gethostname", return_value="unconfigured"), \
                self.assertRaisesRegex(R.Invalid, "no matching host.*configured hosts: build1, laptop"):
            R.detect_orchestrator(self.cfg)

    def test_duplicate_hostname_is_ambiguous(self):
        self.cfg["hosts"]["build2"]["match"]["hostname"] = "build1"
        with patch.object(R.H.socket, "gethostname", return_value="build1"), \
                self.assertRaisesRegex(R.Invalid, "ambiguous hosts: build1, build2"):
            R.detect_orchestrator(self.cfg)

    def cli(self, *flags):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts.json"
            path.write_text(json.dumps(self.cfg))
            out, err = io.StringIO(), io.StringIO()
            argv = ["route.py", "--config", str(path), "--issue", "MOB-1",
                    "--grant", "local-only", "--commit", "a" * 40, "--json", *flags]
            with patch.object(sys, "argv", argv), patch.object(R, "routing_ref", return_value="offline"), \
                    patch.object(R.subprocess, "run", side_effect=AssertionError("live call")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = R.main()
            return rc, out.getvalue(), err.getvalue()

    def test_cli_hosts_from_loaded_config(self):
        for host in self.cfg["hosts"]:
            rc, out, err = self.cli("--host", host, "--from", "build2")
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)["selected_host"], host)
            rc, out, err = self.cli("--host", "build1", "--from", host)
            self.assertEqual(rc, 0, err)
        for flag in ("--host", "--from"):
            rc, out, err = self.cli(flag, "desktop")
            self.assertEqual(rc, 2)
            self.assertIn("error: Invalid: " + flag + ": unknown host", err)
            self.assertIn("choose from build1, build2, laptop", err)
        with patch.dict(os.environ, ROUTE_HOST="build2"):
            self.assertEqual(self.cli("--host", "build1")[0], 0)

    def test_linux_default_requires_disambiguation(self):
        del self.cfg["default_linux_host"]
        with self.assertRaisesRegex(R.Invalid, "default_linux_host is required.*build1, build2"):
            R.validate_config(self.cfg)
        del self.cfg["hosts"]["build2"]
        self.assertEqual(R.default_linux_host(self.cfg), "build1")
        task = {"routing": {"requires": ["web"]}}
        self.assertEqual(R.choose_host(task, self.cfg)[0], "build1")

    def test_duplicate_prefix_rejected_on_load(self):
        self.cfg["projects"]["mobile"]["issue_prefix"] = "app"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts.json"
            path.write_text(json.dumps(self.cfg))
            with self.assertRaisesRegex(R.Invalid, "duplicate issue_prefix 'APP'.*web-app.*mobile.*case-insensitive"):
                R.load_config(path)
        rc, out, err = self.cli("--repo", "mobile", "--from", "build1")
        self.assertEqual(rc, 2)
        self.assertIn("duplicate issue_prefix", err)

    def test_three_host_views_use_config_and_home_order(self):
        report = {"tasks": [], "untracked": [], "issues": []}
        queues = {h: [] for h in self.cfg["hosts"]}
        capacity = {h: {"max_heavy": info["max_heavy"], "max_queue": info["max_queue"]}
                    for h, info in self.cfg["hosts"].items()}
        order = ["build2", "build1", "laptop"]
        self.assertEqual(D.host_order(self.cfg, self.cfg["hosts"]), order)
        page = D.render(self.cfg, report, queues, capacity)
        lines = "\n".join(W.frame_lines((report, queues, capacity), self.cfg, 0, 120, 5, 0, None))
        plain = W.ANSI_RE.sub("", lines)
        for h in order:
            label, desc, icon, style = D.host_style(self.cfg, h)
            self.assertIn(label + " lane", page)
            self.assertIn(desc, page)
            self.assertIn(label.upper(), plain)
            self.assertIn(desc, plain)
            self.assertIn(W.c(label.upper(), style, True), lines)
            self.assertEqual(icon, "mac" if h == "laptop" else "desktop")
        self.assertLess(page.index("Routing home lane"), page.index("First builder lane"))
        self.assertLess(plain.index("ROUTING HOME"), plain.index("FIRST BUILDER"))
        self.assertLess(plain.index("FIRST BUILDER"), plain.index("MOBILE LAB"))
        self.cfg["hosts"]["build1"]["os"] = "darwin"
        self.assertEqual(D.host_style(self.cfg, "build1")[2:], ("mac", "lane-mac"))

    def test_heavy_uses_identity_for_capacity(self):
        loader = importlib.machinery.SourceFileLoader("routing_heavy", str(ROOT / "scripts/heavy"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config").mkdir()
            self.cfg["hosts"]["build2"]["max_heavy"] = 7
            (Path(tmp) / "config/hosts.local.json").write_text(json.dumps(self.cfg))
            with patch.object(module, "ROOT", tmp), patch.dict(os.environ, ROUTE_HOST="build2"):
                self.assertEqual(module.slot_count(), 7)
            with patch.object(module, "ROOT", tmp), \
                    patch.object(R.H.socket, "gethostname", return_value="build1"):
                self.assertEqual(module.slot_count(), 3)
            with patch.object(module, "ROOT", tmp), \
                    patch.object(R.H.socket, "gethostname", return_value="unconfigured"), \
                    patch.object(R.H.platform, "system", return_value="Linux"), \
                    self.assertRaisesRegex(ValueError, "set ROUTE_HOST"):
                module.slot_count()


if __name__ == "__main__":
    unittest.main()
