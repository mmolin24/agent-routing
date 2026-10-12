#!/usr/bin/env python3
"""Project profiles: routing and rendering without contacting either host."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import route as R

SHA = "a" * 40


class ProjectProfiles(unittest.TestCase):
    def setUp(self):
        self.cfg = R.load_json(ROOT / "tests/fixtures/hosts.golden.json")
        self.cfg["projects"]["ops"] = {
            "issue_prefix": "OPS",
            "worktree": {"kind": "git", "dir": "tickets/{issue}", "branch": "ops/{issue}"},
        }
        for host in self.cfg["hosts"].values():
            host["repos"]["ops"] = {"path": "/repos/ops", "worktree_root": "/trees/ops",
                                      "remote": "https://example.com/ops.git"}

    def build(self, **overrides):
        values = dict(
            issue="OPS-3", repo=None, grant="local-only", role=None, suffix=None,
            summary=None, granted_by=None, host=None, requires="", commit=SHA,
            deps="link", kind="codex", model=None, effort=None, timeout=120,
            compose=False, heavy=False, light=False, ports="", stop=None,
            brief_file=None)
        values.update(overrides)
        return R.build_task(SimpleNamespace(**values), self.cfg)

    def cli(self, *flags):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "hosts.json"
            config.write_text(json.dumps(self.cfg))
            out = io.StringIO()
            with patch.object(sys, "argv", ["route.py", "--config", str(config),
                                           "--grant", "local-only", "--commit", SHA,
                                           "--print-task", *flags]), contextlib.redirect_stdout(out):
                rc = R.main()
            self.assertEqual(rc, 0, out.getvalue())
            return json.loads(out.getvalue())

    def note(self, task, **overrides):
        decision = {"routing_ref": "test", **overrides}
        return R.handoff_note(task, decision, self.cfg, "desktop")

    def test_second_project_cli_and_git_setup(self):
        task = self.cli("--issue", "OPS-3")
        self.assertEqual(task["repository"]["name"], "ops")
        self.assertEqual(task["worktree"], {"strategy": "git-worktree", "path": "tickets/OPS-3",
                                           "branch": "ops/OPS-3"})
        self.assertEqual(R.validate(task, self.cfg), [])
        self.assertEqual(R.choose_host(task, self.cfg)[0], "desktop")
        self.assertEqual(R.worktree_identity(task, self.cfg, "desktop"),
                         ("/trees/ops/tickets/OPS-3", "ops/OPS-3"))
        self.assertEqual(R.setup_command(task, self.cfg, "desktop"), (
            f'git worktree add -b ops/OPS-3 /trees/ops/tickets/OPS-3 {SHA} && '
            'builtin cd /trees/ops/tickets/OPS-3 && git rev-parse HEAD; echo "__RT_DONE_0__ rc=$?"', 120000))
        self.assertEqual(R.herdr_group(task["task_id"]), ("OPS-3", "impl"))

    def test_no_package_manager_omits_pnpm(self):
        # A stale host command does not imply that the project uses a package manager.
        self.cfg["hosts"]["desktop"]["repos"]["ops"]["pnpm_cmd"] = "old-pnpm"
        note = self.note(self.build())
        self.assertNotIn("pnpm", note)
        self.assertIn("<your full test command>", note)

    def test_numeric_issue_uses_default_project_prefix(self):
        self.cfg["default_repo"] = "ops"
        task = self.cli("--issue", "12")
        self.assertEqual(task["repository"]["name"], "ops")
        self.assertEqual(task["worktree"]["path"], "tickets/OPS-12")
        self.assertEqual(task["task_id"], "ops12-impl")

    def test_explicit_repo_overrides_prefix_selection(self):
        task = self.cli("--issue", "APP-12", "--repo", "example-infra")
        self.assertEqual(task["repository"]["name"], "example-infra")
        with self.assertRaises(R.Invalid):
            self.build(issue="APP-12", repo="ops")

    def test_missing_default_repo_errors(self):
        del self.cfg["default_repo"]
        with self.assertRaisesRegex(R.Invalid, "default_repo"):
            self.build()
        self.assertEqual(self.build(repo="ops")["repository"]["name"], "ops")

    def test_numeric_issue_without_prefix_errors(self):
        del self.cfg["projects"]["ops"]["issue_prefix"]
        with self.assertRaisesRegex(R.Invalid, "issue_prefix"):
            self.build(issue="12", repo="ops")

    def test_unprofiled_repo_generic_fallbacks(self):
        del self.cfg["projects"]["ops"]
        self.cfg["default_repo"] = "ops"
        task = self.build(issue="XY-42", suffix="web")
        self.assertEqual(task["worktree"], {"strategy": "git-worktree", "path": "XY-42-web",
                                           "branch": "feature/XY-42-web"})
        self.assertEqual(R.validate(task, self.cfg), [])
        self.assertNotIn("pnpm", self.note(task))
        self.assertIn("<your full test command>", self.note(task))
        self.assertEqual(R.staging_worktree(self.cfg, "ops", "XY-42"),
                         {"strategy": "git-detached", "path": "XY-42-staging"})
        for issue in ("A-1", "bad", "ABC-", "ABC-2/evil", "X9-42", "X94-2"):
            with self.subTest(issue=issue), self.assertRaises(R.Invalid):
                self.build(issue=issue)

    def assert_missing_helper(self, action, repo="example-app"):
        with self.assertRaises(R.Invalid) as caught:
            action()
        self.assertEqual(str(caught.exception),
                         f"config: {repo} uses the issue worktree helper but has no projects.{repo} profile; "
                         "add one (docs/task-contract.md, Projects)")

    def test_legacy_issue_worktree_without_profile(self):
        del self.cfg["projects"]["example-app"]
        # A legacy declaration on either host invalidates the config, even when
        # the requested host has no such declaration and the issue is numeric.
        for legacy_host in ("desktop", "mac"):
            for host, hc in self.cfg["hosts"].items():
                hc["repos"]["example-app"].pop("issue_worktree", None)
                if host == legacy_host:
                    hc["repos"]["example-app"]["issue_worktree"] = "pnpm-worktree-issue"
            for issue in ("APP-3", "3"):
                for grant in ("local-only", "gate"):
                    with self.subTest(host=legacy_host, issue=issue, grant=grant):
                        self.assert_missing_helper(lambda: self.build(
                            repo="example-app", issue=issue, grant=grant, host="desktop"))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps(self.cfg))
                self.assert_missing_helper(lambda: R.load_config(path))

    def test_explicit_helper_task_and_record_require_profile(self):
        task = self.build(repo="example-app", issue="APP-3")
        del self.cfg["projects"]["example-app"]
        for host in self.cfg["hosts"].values():
            host["repos"]["example-app"].pop("issue_worktree", None)
        for action in (lambda: R.validate(task, self.cfg),
                       lambda: R.worktree_identity(task, self.cfg, "desktop"),
                       lambda: R.setup_command(task, self.cfg, "desktop")):
            self.assert_missing_helper(action)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "old.json").write_text(json.dumps(task))
            self.assert_missing_helper(lambda: R.load_active(self.build(), self.cfg, tmp))
        # Having a project with only a git profile does not supply a helper.
        self.cfg["projects"]["example-app"] = {"worktree": {"kind": "git"}}
        self.assert_missing_helper(lambda: R.validate(task, self.cfg))

    def test_profile_wins_over_legacy_host_kind(self):
        for host in self.cfg["hosts"].values():
            host["repos"]["ops"]["issue_worktree"] = "pnpm-worktree-issue"
        task = self.build()
        self.assertEqual(task["worktree"]["strategy"], "git-worktree")
        self.assertIsNone(R.package_manager(self.cfg, "ops"))

    def helper_profile(self):
        self.cfg["projects"]["ops"].update({
            "package_manager": "pnpm",
            "worktree": {"kind": "helper", "dir": "jobs/{issue}", "branch": "jobs/{issue}",
                         "create": "{pm} task:create {issue} {commit} {deps} {env}",
                         "check": "{pm} task:check {issue}"},
        })
        self.cfg["hosts"]["desktop"]["repos"]["ops"]["pnpm_cmd"] = "npx -y pnpm@10"

    def test_helper_templates_and_issue_validation(self):
        self.helper_profile()
        task = self.build(deps="install")
        self.assertEqual(R.worktree_identity(task, self.cfg, "desktop"),
                         ("/repos/ops/jobs/OPS-3", "jobs/OPS-3"))
        self.assertEqual(R.setup_command(task, self.cfg, "desktop")[0],
                         f'npx -y pnpm@10 task:create OPS-3 {SHA} install link && builtin cd jobs/OPS-3 '
                         '&& npx -y pnpm@10 task:check OPS-3 && git rev-parse HEAD; echo "__RT_DONE_0__ rc=$?"')
        task["worktree"]["issue"] = "APP-3"
        self.assertTrue(any("issue" in e for e in R.validate(task, self.cfg)))
        # No config means structural validation uses the generic pattern.
        self.assertEqual(R.validate(task), [])

    def test_helper_without_check(self):
        self.helper_profile()
        del self.cfg["projects"]["ops"]["worktree"]["check"]
        command = R.setup_command(self.build(), self.cfg, "desktop")[0]
        self.assertIn("builtin cd jobs/OPS-3 && git rev-parse HEAD", command)

    def test_deps_link_guard_is_pnpm_specific(self):
        self.helper_profile()
        task = self.build()
        task["commands"] = [{"run": "pnpm install", "mutating": True}]
        self.assertTrue(any("deps=link" in b for b in R.policy_blockers(task, self.cfg)))
        task["worktree"]["deps"] = "install"
        self.assertEqual(R.policy_blockers(task, self.cfg), [])
        task["worktree"]["deps"] = "link"
        del self.cfg["projects"]["ops"]["package_manager"]
        self.assertEqual(R.policy_blockers(task, self.cfg), [])

    def test_gate_runtime_and_heavy_class(self):
        task = self.build(grant="gate")
        self.assertEqual(task["resources"]["runtime"], "none")
        self.assertTrue(R.is_heavy(task))
        self.cfg["projects"]["ops"]["gate_runtime"] = "compose"
        self.assertEqual(self.build(grant="gate")["resources"]["runtime"], "compose")
        self.assertEqual(self.build(compose=True)["resources"]["runtime"], "compose")

    def test_brief_examples_and_env_notes(self):
        self.helper_profile()
        self.cfg["projects"]["ops"]["brief"] = {
            "gate_example": "{pm} gate", "focused_example": "{pm} lint",
            "env_notes": {"OPS_PORT": "Use $OPS_PORT.", "ABSENT": "Do not show this."},
        }
        note = self.note(self.build(), env={"OPS_PORT": "4321"}, port_slot=1)
        self.assertIn("- pnpm: use `npx -y pnpm@10`", note)
        self.assertIn("scripts/heavy npx -y pnpm@10 gate", note)
        self.assertIn("Use $OPS_PORT.", note)
        self.assertNotIn("Do not show this.", note)
        self.assertNotIn("POSTGRES", note)
        self.assertIn("scripts/heavy npx -y pnpm@10 lint", self.note(self.build(grant="review")))

    def test_staging_profile_and_detached_deps(self):
        self.helper_profile()
        self.cfg["projects"]["ops"]["staging"] = {"worktree_dir": "sessions/{issue}"}
        task = self.build(grant="staging-review")
        self.assertEqual(task["worktree"], {"strategy": "git-detached", "path": "sessions/OPS-3"})
        command = R.setup_command(task, self.cfg, "desktop")[0]
        self.assertIn(f"detached-worktree.sh /trees/ops/sessions/OPS-3 {SHA} /repos/ops npx -y pnpm@10", command)
        # A git project retains plain detached setup even if its brief names pnpm.
        task = self.build(repo="example-infra", issue="APP-3", grant="staging-review")
        command = R.setup_command(task, self.cfg, "desktop")[0]
        self.assertNotIn(f"{SHA} /home/dev/projects/example-infra", command)

    def test_command_manager_shared_by_plan_and_dispatch(self):
        self.helper_profile()
        task = self.build()
        self.assertEqual(R.command_run("pnpm test", task, self.cfg, "desktop"), "npx -y pnpm@10 test")
        task["mode"] = "command"
        task["commands"] = [{"run": "pnpm test", "mutating": False}]
        plan = R.plan(task, self.cfg, "desktop", "desktop")
        self.assertIn("npx -y pnpm@10 test", str(plan))

    def test_profile_paths_rejected_at_load_and_use(self):
        self.helper_profile()
        original = copy.deepcopy(self.cfg)
        for section, key in (("worktree", "dir"), ("staging", "worktree_dir")):
            for path in ("../outside/{issue}", "/tmp/{issue}", "", ".", "././",
                         "jobs/../{issue}", "bad path/{issue}", "jobs/{issue}/.."):
                with self.subTest(section=section, path=path):
                    self.cfg = copy.deepcopy(original)
                    self.cfg["projects"]["ops"].setdefault(section, {})[key] = path
                    with self.assertRaisesRegex(R.Invalid, f"projects.ops.{section}.{key}"):
                        self.build(grant="staging-review" if section == "staging" else "local-only")
                    with tempfile.TemporaryDirectory() as tmp:
                        config = Path(tmp) / "config.json"
                        config.write_text(json.dumps(self.cfg))
                        with self.assertRaisesRegex(R.Invalid, f"projects.ops.{section}.{key}"):
                            R.load_config(config)
        self.cfg = original
        task = self.build()
        # Config-load samples are safe, but host substitutions can render '.'.
        self.cfg["projects"]["ops"]["worktree"]["dir"] = "{pm}"
        self.cfg["hosts"]["desktop"]["repos"]["ops"]["pnpm_cmd"] = "."
        R.validate_config(self.cfg)
        with self.assertRaisesRegex(R.Invalid, "worktree.dir"):
            R.validate(task, self.cfg)
        with self.assertRaisesRegex(R.Invalid, "worktree.dir"):
            R.worktree_dir(task, self.cfg, "desktop")
        with self.assertRaisesRegex(R.Invalid, "worktree.dir"):
            R.setup_command(task, self.cfg, "desktop")

    def test_template_path_cannot_resolve_to_repo_root(self):
        # A relative template can equal the main checkout when worktree_root
        # is the checkout's parent; validate both staging and git templates.
        self.cfg["hosts"]["desktop"]["repos"]["ops"]["worktree_root"] = "/repos"
        for section, key in (("worktree", "dir"), ("staging", "worktree_dir")):
            with self.subTest(section=section):
                profile = self.cfg["projects"]["ops"]
                old = copy.deepcopy(profile)
                profile.setdefault(section, {})[key] = "ops"
                with self.assertRaisesRegex(R.Invalid, "repo root"):
                    R.validate_config(self.cfg)
                self.cfg["projects"]["ops"] = old
        self.cfg["projects"]["ops"]["worktree"]["dir"] = "{issue}"
        self.cfg["hosts"]["desktop"]["repos"]["ops"]["path"] = "/repos/OPS-3"
        with self.assertRaisesRegex(R.Invalid, "repo root"):
            self.build()

    def test_recorded_identity_survives_profile_changes(self):
        self.helper_profile()
        task = self.build(grant="draft-pr")
        with tempfile.TemporaryDirectory() as tmp, patch.object(R, "ROOT", tmp):
            active = Path(tmp) / "active"
            decision = {"selected_host": "desktop", "routing_ref": "test"}
            run_dir = R.record(task, decision, "brief", str(active), self.cfg)
            recorded = R.load_json(active / (task["task_id"] + ".json"))
            self.assertEqual(recorded["_occupies"], {
                "host": "desktop", "path": "/repos/ops/jobs/OPS-3", "branch": "jobs/OPS-3"})
            self.assertNotIn("_occupies", R.load_json(Path(tmp) / run_dir / "task.json"))
            self.cfg["projects"]["ops"]["worktree"].update(dir="new/{issue}", branch="new/{issue}")
            contender = self.build(issue="OPS-4", grant="draft-pr")
            contender["worktree"] = {"strategy": "git-worktree", "path": "/repos/ops/jobs/OPS-3",
                                     "branch": "jobs/OPS-3"}
            problems, records = R.load_active(contender, self.cfg, str(active))
            self.assertEqual(problems, [])
            self.assertEqual(R.collisions(contender, "desktop", self.cfg, records), [
                "worktree path /repos/ops/jobs/OPS-3 in use by ops3-impl",
                "branch jobs/OPS-3 in use by ops3-impl"])
            self.assertEqual(R.collisions(contender, "mac", self.cfg, records), [
                "branch jobs/OPS-3 is already owned by ops3-impl on desktop"])
            # Stored host wins over stale or absent selected_host too.
            recorded["selected_host"] = "mac"
            (active / (task["task_id"] + ".json")).write_text(json.dumps(recorded))
            self.assertEqual(R.load_active(contender, self.cfg, str(active))[1][0][2], "desktop")
            del recorded["_occupies"]
            recorded["selected_host"] = "desktop"
            (active / (task["task_id"] + ".json")).write_text(json.dumps(recorded))
            records = R.load_active(contender, self.cfg, str(active))[1]
            self.assertEqual(R.collisions(contender, "desktop", self.cfg, records), [])

    def test_recorded_identity_for_git_detached_and_existing(self):
        for strategy in ("git-worktree", "git-detached", "existing-checkout"):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as tmp, patch.object(R, "ROOT", tmp):
                task = self.build()
                if strategy == "git-detached":
                    task["worktree"] = {"strategy": strategy, "path": "sessions/OPS-3"}
                elif strategy == "existing-checkout":
                    task["worktree"] = {"strategy": strategy}
                active = Path(tmp) / "active"
                R.record(task, {"selected_host": "desktop", "routing_ref": "test"}, "brief", str(active), self.cfg)
                recorded = R.load_json(active / (task["task_id"] + ".json"))
                self.assertEqual(recorded["_occupies"]["host"], "desktop")
                self.assertEqual((recorded["_occupies"]["path"], recorded["_occupies"]["branch"]),
                                 R.worktree_identity(task, self.cfg, "desktop"))

    def test_prefix_must_be_letters_only(self):
        for prefix in ("X9", "X94", "A", "ABCDEFGHIJK", "OP-S", "", 12):
            with self.subTest(prefix=prefix):
                self.cfg["projects"]["ops"]["issue_prefix"] = prefix
                with self.assertRaisesRegex(R.Invalid, "projects.ops.issue_prefix"):
                    self.build(issue="12", repo="ops")
        for issue in ("X9-42", "X94-2"):
            self.assertIsNone(R.ISSUE_RE.fullmatch(issue))

    def test_lowercase_prefix_normalized_on_read(self):
        self.cfg["projects"]["ops"]["issue_prefix"] = "ops"
        self.cfg["default_repo"] = "ops"
        self.assertEqual(R.project_profile(self.cfg, "ops")["issue_prefix"], "OPS")
        for flags in (("--repo", "ops", "--issue", "OPS-3"), ("--issue", "ops-3"),
                      ("--repo", "ops", "--issue", "3")):
            with self.subTest(flags=flags):
                task = self.cli(*flags)
                self.assertEqual(task["worktree"]["path"], "tickets/OPS-3")
                self.assertEqual(R.issue_key(task["task_id"]), "OPS-3")
                self.assertEqual(R.herdr_group(task["task_id"]), ("OPS-3", "impl"))

    def test_brief_uses_actual_issue_deps_and_env(self):
        self.cfg["projects"]["ops"]["brief"] = {
            "gate_example": "check {issue} --deps {deps} --env {env}",
            "focused_example": "focus {issue} --deps {deps} --env {env}"}
        for task in (self.build(suffix="web"), self.build(grant="staging-review")):
            self.assertIn("check OPS-3 --deps link --env link", self.note(task))
        self.helper_profile()
        for deps in ("none", "install"):
            for env in ("none", "link"):
                with self.subTest(deps=deps, env=env):
                    task = self.build(deps=deps)
                    task["worktree"]["env"] = env
                    self.assertIn(f"check OPS-3 --deps {deps} --env {env}", self.note(task))
                    command = R.setup_command(task, self.cfg, "desktop")[0]
                    self.assertIn(f"{SHA} {deps} {env}", command)
                    task["authorization"] = dict(copy.deepcopy(self.cfg["grants"]["review"]), granted_by="test")
                    self.assertIn(f"focus OPS-3 --deps {deps} --env {env}", self.note(task))

    def test_host_pnpm_command_without_package_manager(self):
        for profiled in (True, False):
            with self.subTest(profiled=profiled):
                if not profiled:
                    del self.cfg["projects"]["ops"]
                self.cfg["hosts"]["desktop"]["repos"]["ops"]["pnpm_cmd"] = "npx -y pnpm@10.33.2"
                task = self.build(repo="ops")
                self.assertIsNone(R.package_manager(self.cfg, "ops"))
                self.assertEqual(R.command_run("pnpm test", task, self.cfg, "desktop"),
                                 "npx -y pnpm@10.33.2 test")
                self.assertEqual(R.command_run("pnpm", task, self.cfg, "desktop"), "npx -y pnpm@10.33.2")
                self.assertEqual(R.command_run("pnpmish test", task, self.cfg, "desktop"), "pnpmish test")
                self.assertEqual(R.command_run("pnpm test", task, self.cfg, "mac"), "pnpm test")
                task.update(mode="command", commands=[{"run": "pnpm test", "mutating": False}])
                self.assertIn("npx -y pnpm@10.33.2 test", str(R.plan(task, self.cfg, "desktop", "desktop")))

    def test_malformed_templates_name_project_and_key(self):
        self.helper_profile()
        original = copy.deepcopy(self.cfg)
        for section, key in (("worktree", "dir"), ("worktree", "branch"), ("worktree", "create"),
                             ("worktree", "check"), ("staging", "worktree_dir"),
                             ("brief", "gate_example"), ("brief", "focused_example")):
            for template in ("tickets/{0}", "{issue.typo}", "{issue", "{missing}"):
                with self.subTest(key=key, template=template):
                    self.cfg = copy.deepcopy(original)
                    self.cfg["projects"]["ops"].setdefault(section, {})[key] = template
                    with self.assertRaisesRegex(R.Invalid, f"projects.ops.{section}.{key}"):
                        self.build()
        for template in ("tickets/{0}", "{issue.typo}", "{issue", "{missing}"):
            with self.subTest(cli_template=template), tempfile.TemporaryDirectory() as tmp:
                self.cfg = copy.deepcopy(original)
                self.cfg["projects"]["ops"]["worktree"]["dir"] = template
                config = Path(tmp) / "config.json"
                config.write_text(json.dumps(self.cfg))
                err = io.StringIO()
                with patch.object(sys, "argv", ["route.py", "--config", str(config), "--issue", "OPS-3",
                                               "--grant", "local-only", "--commit", SHA, "--print-task"]), \
                        contextlib.redirect_stderr(err):
                    self.assertEqual(R.main(), 2)
                self.assertIn("Invalid", err.getvalue())
                self.assertIn("projects.ops.worktree.dir", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
