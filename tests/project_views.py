#!/usr/bin/env python3
"""Offline multi-project views, lookups, events and retirement."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import route as R
import dashboard as D
import events as E
import reap as RP
import watch as W

NOW = 1_800_000_000
SHA = "a" * 40


def pr(number, state="open", title="", base="main"):
    return {"number": number, "state": state, "title": title, "head": SHA, "base": base,
            "url": f"https://example.com/pull/{number}", "checks": {"pass": 1, "fail": 0, "pending": 0}}


def task(key, role="impl"):
    return {"task_id": key.lower().replace("-", "") + "-" + role, "host": "desktop", "summary": key + ": work",
            "agents": [{"role": "worker", "status": "working"}]}


class ProjectViews(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="project-views-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = R.load_json(ROOT / "tests/fixtures/hosts.golden.json")
        self.cfg["projects"]["example-app"]["pr"]["checkout"] = "/checkouts/product"
        self.cfg["projects"]["app"] = {
            "issue_prefix": "MOB", "tracker": "none", "pipeline": ["impl", "review", "merge"],
            "worktree": {"kind": "git", "dir": "tickets/{issue}", "branch": "changes/{issue}"},
            "pr": {"checkout": "/checkouts/app"},
        }
        self.prs = {"/checkouts/product": {}, "/checkouts/app": {}}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"ROOT": str(self.root), "CLOSED_DIR": str(self.root / "runs/closed"),
                            "ISSUES_DIR": str(self.root / "runs/issues"), "QUEUE_DIR": str(self.root / "runs/queue"),
                            "detect_orchestrator": lambda cfg: "desktop"}.items():
            self.stack.enter_context(patch.object(R, name, value))
        self.stack.enter_context(patch.dict("os.environ", {}, clear=True))
        self.stack.enter_context(patch.object(R.time, "time", lambda: NOW))
        self.stack.enter_context(patch.object(W, "c", lambda text, *a, **kw: str(text)))
        self.stack.enter_context(patch.object(RP, "on_home", lambda *args: True))

    def marks(self, key, **steps):
        path = Path(R.ISSUES_DIR) / (key + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"issue": key, "steps": {
            n: {"state": state, "at": NOW - 60, "sha": SHA} for n, state in steps.items()}}))

    def view(self, tasks=(), live=None, queues=None):
        report = {"tasks": list(tasks), "untracked": [], "unreachable": []}
        with patch.object(R, "pr_states", side_effect=lambda checkout: copy.deepcopy(self.prs[checkout])) as lookup, \
                patch.object(R, "linear_states", return_value=live or {}) as tracker:
            rows = R.issues_view(report, queues or {}, str(self.root / "runs/active"), self.cfg)
        report["issues"] = rows
        return report, lookup, tracker

    def closed(self, key, role, project=None, outcome="approved", sha=SHA):
        rec = dict(task(key, role), closed_at=NOW - 60, outcome=outcome, project_commit=sha)
        if project:
            rec["repository"] = {"name": project}
        path = Path(R.CLOSED_DIR) / (rec["task_id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec))
        return rec

    def assert_kept(self, report, key, prev=None):
        """A recently idle worker must survive selection, events and apply's re-check."""
        agent = {"issue": key, "name": key.lower().replace("-", "") + "-impl", "host": "desktop",
                 "status": "idle", "active_at": NOW - 60, "pane_id": "p1"}
        report["untracked"] = [agent]
        self.assertEqual(RP.candidates(report, last={}, now=NOW), [])
        obs, info, bad = E.observe((report, {}, {}))
        self.assertNotIn("reap:" + key, obs)
        for first in (False, True):
            before = {} if first else (prev or {})
            kept = E.carry_forward(before, obs, info, bad)
            self.assertFalse(any(kind == "REAP" for kind, _ in E.events(before, kept, info, first=first)))
        calls = []
        def herdr(cfg, host, orch, *args):
            calls.append(args)
            if args[:2] == ("agent", "list"):
                return {"result": {"agents": [dict(agent, agent_status="idle", tab_id="t1", workspace_id="w1")]}}
            if args[:2] == ("pane", "list"):
                return {"result": {"panes": [{"pane_id": "p1", "tab_id": "t1"}]}}
            if args[:2] == ("workspace", "get"):
                return {"result": {"label": key}}
            return {}
        # Force an earlier proposal into --apply, then change its evidence to
        # this unsafe snapshot. No tab-close boundary may be reached.
        proposal = dict(agent, reason="earlier completion")
        (self.root / "runs").mkdir(exist_ok=True)
        with patch.object(R, "gather", return_value=(report, {}, {})):
            result = RP.apply([proposal], self.cfg, "desktop", herdr)
        self.assertTrue(result[0]["result"].startswith("skipped:"), result)
        self.assertFalse(any(args[:2] == ("tab", "close") for args in calls))

    def test_failed_pr_lookup_never_turns_stale_review_into_reap(self):
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "review"]
        self.marks("MOB-1", review="approved")
        self.prs["/checkouts/app"] = {"changes/MOB-1": dict(pr(1), head="b" * 40)}
        before, _, _ = self.view()
        self.assertEqual(before["issues"][0]["steps"]["review"]["cls"], "stale")
        prev, _, _ = E.observe((before, {}, {}))
        self.prs["/checkouts/app"] = None
        report, _, _ = self.view()
        self.assertFalse(report["issues"][0]["completed"])
        self.assert_kept(report, "MOB-1", prev)
        obs, info, bad = E.observe((report, {}, {}))
        # Even a newly proposed completion from another producer cannot sneak
        # through carry-forward, on either a transition or a first snapshot.
        obs["reap:MOB-1"] = "reap"
        self.assertNotIn("reap:MOB-1", E.carry_forward(prev, obs, info, bad))
        with patch.object(RP, "candidates", return_value=[{"issue": "MOB-1", "host": "desktop", "reason": "done"}]):
            self.assertNotIn("reap:MOB-1", E.observe((report, {}, {}))[0])

    def test_failed_tracker_blocks_completion_and_reap_even_with_recorded_done(self):
        self.cfg["projects"]["app"].update(tracker="linear", pipeline=["impl", "review"])
        self.marks("MOB-1", review="approved", linear="done")
        with patch.object(R, "pr_states", return_value={}), patch.object(R, "linear_states", return_value=None):
            report = {"tasks": [], "untracked": [], "unreachable": []}
            report["issues"] = R.issues_view(report, cfg=self.cfg)
        self.assertFalse(report["issues"][0]["completed"])
        self.assert_kept(report, "MOB-1")
        # An outage vetoes the 24 h fallback too.
        report["untracked"][0]["active_at"] = NOW - 48 * 3600
        report["issues"][0]["updated"] = NOW - 48 * 3600
        self.assertEqual(RP.candidates(report, last={}, now=NOW), [])

    def test_every_verification_result_is_stale_at_a_new_pr_head(self):
        for step in ("qa", "review", "gate", "staging"):
            with self.subTest(step=step):
                self.cfg["projects"]["app"]["pipeline"] = ["impl", step]
                self.marks("MOB-1", **{step: "passed"})
                self.prs["/checkouts/app"] = {"changes/MOB-1": dict(pr(1), head="b" * 40)}
                report, _, _ = self.view()
                row = report["issues"][0]
                self.assertEqual(row["steps"][step]["cls"], "stale")
                self.assertFalse(row["completed"])
                self.assert_kept(report, "MOB-1")

    def test_impl_at_an_older_sha_stays_done(self):
        self.closed("MOB-1", "impl", "app", outcome="done")
        self.prs["/checkouts/app"] = {"changes/MOB-1": dict(pr(1), head="b" * 40)}
        row = self.view()[0]["issues"][0]
        self.assertEqual(row["steps"]["impl"]["sha"], SHA)
        self.assertEqual(row["steps"]["impl"]["cls"], "done")
        self.assertNotIn("re-run", row["steps"]["impl"]["label"])

    def test_merge_and_deploy_at_an_older_sha_stay_done(self):
        for step, state in (("merge", "merged"), ("deploy", "deployed")):
            with self.subTest(step=step):
                self.cfg["projects"]["app"]["pipeline"] = ["impl", step]
                self.marks("MOB-1", **{step: state})
                self.prs["/checkouts/app"] = {"changes/MOB-1": dict(pr(1), head="b" * 40)}
                row = self.view()[0]["issues"][0]
                self.assertEqual(row["steps"][step]["cls"], "done")
                self.assertTrue(row["completed"])

    def test_removed_role_maps_to_impl_without_completing_or_reaping(self):
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa"]
        self.closed("MOB-1", "review", "app")
        report, _, _ = self.view()
        self.assertEqual(set(report["issues"][0]["steps"]), {"impl"})
        self.assertEqual(report["issues"][0]["steps"]["impl"]["cls"], "done")
        self.assertFalse(report["issues"][0]["completed"])
        self.assert_kept(report, "MOB-1")
        for role in ("impl", "verify", "work", "review", "audit", "rev-audit"):
            self.assertEqual(R.task_step("mob1-" + role, ["impl", "qa"]), "impl")

    def test_pipeline_requires_two_steps_starting_with_impl_when_read(self):
        for pipeline in ([], ["impl"], ["review"], ["review", "impl"], ["qa", "ship"]):
            with self.subTest(pipeline=pipeline):
                self.cfg["projects"]["app"]["pipeline"] = pipeline
                config = self.root / "config.json"
                config.write_text(json.dumps(self.cfg))
                for read in (lambda: R.project_profile(self.cfg, "app"),
                             lambda: R.load_config(config),
                             lambda: self.view([task("MOB-1")])):
                    with self.assertRaises(R.Invalid) as caught:
                        read()
                    self.assertIn("projects.app.pipeline", str(caught.exception))
                    self.assertIn("at least two steps and start with impl", str(caught.exception))

    def test_pipeline_steps_are_single_lowercase_words_when_read(self):
        for step in ("qa-work", "qa-review-2", "qa-2", "qa2", "qa_work", "QA", "q", "q" * 21,
                     "", 2, "linear", "none"):
            with self.subTest(step=step):
                self.cfg["projects"]["app"]["pipeline"] = ["impl", step]
                for read in (lambda: R.project_profile(self.cfg, "app"),
                             lambda: R.validate_config(self.cfg),
                             lambda: self.view([task("MOB-1", "qa-review-2")])):
                    with self.assertRaises(R.Invalid) as caught:
                        read()
                    self.assertIn("projects.app.pipeline", str(caught.exception))
                    self.assertIn(repr(step), str(caught.exception))
                    self.assertIn("[a-z]{2,20}", str(caught.exception))
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa", "q" * 20]
        self.assertEqual(R.project_profile(self.cfg, "app")["pipeline"], ["impl", "qa", "q" * 20])

    def test_suffixed_roles_follow_the_last_non_numeric_token(self):
        for role, step in (("qa-work", "impl"), ("qa-review-2", "review"), ("qa-2", "qa")):
            with self.subTest(role=role):
                self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa", "review", "merge"]
                self.closed("MOB-1", role, "app")
                report, _, _ = self.view()
                row = report["issues"][0]
                self.assertEqual(set(row["steps"]), {step})
                self.assertEqual(row["steps"][step]["state"], "approved")
                self.assertEqual(R.task_step("mob1-" + role, row["pipeline"]), step)
                self.assertFalse(row["completed"])
                self.assert_kept(report, "MOB-1")
                Path(R.CLOSED_DIR, task("MOB-1", role)["task_id"] + ".json").unlink()
        # Unknown and removed roles retain main's impl fallback, even with
        # suffixes and Herdr numbering.
        for role in ("qa-retired", "qa-retired-2", "qa-review-2", "qa-2"):
            self.assertEqual(R.task_step("mob1-" + role, ["impl", "merge"]), "impl")

    def test_inferred_tracker_done_does_not_finish_before_merge(self):
        self.cfg["projects"]["app"].update(tracker="linear", tracker_flow={"after": {"review": "done"}})
        self.marks("MOB-1", review="approved")
        report, _, _ = self.view()
        row = report["issues"][0]
        self.assertTrue(row["steps"]["linear"]["inferred"])
        self.assertEqual(row["steps"]["linear"]["state"], "done")
        self.assertFalse(row["completed"])
        self.assertIsNone(RP.finished(row))
        self.assert_kept(report, "MOB-1")
        # Both sources of authoritative state remain valid completion evidence.
        self.marks("MOB-1", linear="done")
        self.assertTrue(self.view()[0]["issues"][0]["completed"])
        self.assertTrue(self.view(live={"MOB-1": "done"})[0]["issues"][0]["completed"])
        row["steps"]["linear"] = {"state": "done"}
        self.assertFalse(R.is_completed(row))
        self.assertIsNone(RP.finished(row))

    def test_custom_verdict_is_invalidated_by_a_live_fix_round(self):
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa"]
        self.marks("MOB-1", qa="passed")
        report, _, _ = self.view([task("MOB-1")])
        self.assertEqual(report["issues"][0]["steps"]["qa"]["cls"], "stale")
        self.assertFalse(report["issues"][0]["completed"])
        self.assert_kept(report, "MOB-1")

    def test_removed_role_maps_to_impl_for_open_queued_or_live_workers(self):
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa"]
        report, _, _ = self.view([task("MOB-1", "review")], queues={"desktop": ["mob2-review"]})
        rows = {r["issue"]: r for r in report["issues"]}
        self.assertTrue(rows["MOB-1"]["steps"]["impl"]["active"])
        self.assertEqual(rows["MOB-2"]["steps"]["impl"]["queued"], 1)
        self.assertTrue(all(not r["completed"] for r in rows.values()))
        self.assert_kept(report, "MOB-1")
        report = {"tasks": [], "untracked": [{"name": "mob1-review", "status": "working"}]}
        with patch.object(R, "pr_states", return_value={}):
            report["issues"] = R.issues_view(report, cfg=self.cfg)
        self.assertTrue(report["issues"][0]["steps"]["impl"]["active"])
        self.assertFalse(report["issues"][0]["completed"])
        self.assert_kept(report, "MOB-1")

    def test_unknown_roles_map_to_impl_and_invalidate_prior_verification(self):
        pipeline = ["impl", "review", "gate", "merge", "deploy", "staging"]
        self.assertEqual(R.project_profile(self.cfg, "example-app")["pipeline"], pipeline)
        for role in ("audit", "rev-audit"):
            with self.subTest(role=role):
                self.assertEqual(R.task_step("app106-" + role, pipeline), "impl")
                self.closed("APP-106", role, "example-app", outcome="done")
                report, _, _ = self.view()
                row = report["issues"][0]
                self.assertEqual(set(row["steps"]), {"impl", "linear"})
                self.assertEqual(row["steps"]["impl"]["cls"], "done")
                self.assertFalse(row["completed"])
                self.assert_kept(report, "APP-106")
                self.marks("APP-106", review="approved", gate="passed", staging="verified")
                for source in ("open", "queued", "live"):
                    with self.subTest(source=source):
                        if source == "live":
                            report = {"tasks": [], "untracked": [{"name": "app106-" + role,
                                      "host": "desktop", "status": "working"}]}
                            with patch.object(R, "pr_states", return_value={}), \
                                    patch.object(R, "linear_states", return_value={}):
                                report["issues"] = R.issues_view(report, cfg=self.cfg)
                        else:
                            report, _, _ = self.view(
                                [task("APP-106", role)] if source == "open" else [],
                                queues={"desktop": ["app106-" + role]} if source == "queued" else {})
                        row = report["issues"][0]
                        impl = row["steps"]["impl"]
                        self.assertEqual(impl["task"], "app106-" + role)
                        if source == "queued":
                            self.assertEqual(impl["cls"], "queued")
                        else:
                            self.assertTrue(impl["active"])
                            self.assertEqual(impl["cls"], "working")
                            for step in ("review", "gate", "staging"):
                                self.assertEqual(row["steps"][step]["cls"], "stale")
                            self.assertFalse(row["completed"])
                            self.assert_kept(report, "APP-106")
                Path(R.CLOSED_DIR, "app106-" + role + ".json").unlink()
                Path(R.ISSUES_DIR, "APP-106.json").unlink()

    def test_subagent_named_step_wins_over_description_text(self):
        for key, desc, step in (
                ("APP-981", "APP-981 review: check merge conflict handling", "review"),
                ("APP-981", "APP-981 review check merge conflict handling", "review"),
                ("APP-981", "APP-981 REVIEW: check merge conflict handling", "review"),
                ("APP-981", "APP-981 audit: check merge conflict handling", None),     # as main: no verification step named
                ("APP-981", "Review merge conflict handling for APP-981", "review"),  # as main: merge is never matched from text
                ("MOB-1", "MOB-1 qa: review the merge behavior", "qa")):
            with self.subTest(desc=desc):
                self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa", "review", "merge"]
                report = {"tasks": [task(key)], "untracked": [{"name": "orchestrator", "host": "desktop",
                          "subagents": [{"description": desc, "running": True}]}]}
                with patch.object(R, "pr_states", return_value={}), \
                        patch.object(R, "linear_states", return_value={}):
                    report["issues"] = R.issues_view(report, cfg=self.cfg)
                row = report["issues"][0]
                self.assertEqual(set(row["steps"]) - {"impl", "linear"}, {step} if step else set())
                if step is None:
                    continue
                self.assertTrue(row["steps"][step]["active"])
                self.assertEqual(row["steps"][step]["task"], desc)

    def test_close_uses_recorded_repository_for_custom_verdict_requirement(self):
        self.cfg["projects"]["example-app"].update(pipeline=["impl", "gate"], tracker_flow={})
        self.cfg["projects"]["other"] = {"tracker": "none", "pipeline": ["impl", "qa"]}
        active = self.root / "runs/active"
        active.mkdir(parents=True)
        record = dict(task("ZZ-1", "qa"), repository={"name": "other"})
        path = active / (record["task_id"] + ".json")
        path.write_text(json.dumps(record))
        config = self.root / "config.json"
        config.write_text(json.dumps(self.cfg))
        with patch.object(sys, "argv", ["route.py", "--config", str(config), "--active-dir", str(active),
                                      "--close", record["task_id"]]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(), 2)
        self.assertTrue(path.exists())
        self.assertFalse(Path(R.CLOSED_DIR, path.name).exists())
        report, _, _ = self.view([task("ZZ-1", "qa")])
        self.assert_kept(report, "ZZ-1")

    def test_old_record_keeps_repository_for_degradation_veto(self):
        self.cfg["projects"]["other"] = {"tracker": "none", "pipeline": ["impl", "review", "merge"],
                                           "pr": {"checkout": "/checkouts/other"}}
        record = self.closed("ZZ-1", "impl", "other", "done")
        record["closed_at"] = NOW - 20 * 24 * 3600
        Path(R.CLOSED_DIR, record["task_id"] + ".json").write_text(json.dumps(record))
        self.prs["/checkouts/other"] = None
        report, _, _ = self.view()
        self.assertEqual(report["issues"], [])
        self.assert_kept(report, "ZZ-1")
        report["untracked"][0]["active_at"] = NOW - 48 * 3600
        self.assertEqual(RP.candidates(report, last={}, now=NOW), [])

    def test_recorded_repository_precedes_prefix_and_default(self):
        self.cfg["projects"]["example-app"].update(pipeline=["impl", "gate"], tracker_flow={})
        self.cfg["projects"]["other"] = {"tracker": "none", "pipeline": ["impl", "review", "merge"]}
        self.closed("ZZ-1", "impl", "other", "done")
        report, _, _ = self.view()
        row = report["issues"][0]
        self.assertEqual(row["project"], "other")
        self.assertFalse(row["completed"])
        self.assert_kept(report, "ZZ-1")
        # Repository identity also wins over a configured (conflicting) prefix.
        self.closed("APP-1", "impl", "other", "done")
        self.assertEqual(next(r for r in self.view()[0]["issues"] if r["issue"] == "APP-1")["project"], "other")
        active = self.root / "runs/active"
        active.mkdir()
        active.joinpath("zz2-impl.json").write_text(json.dumps(dict(task("ZZ-2"), repository={"name": "other"})))
        row = next(r for r in self.view([task("ZZ-2")])[0]["issues"] if r["issue"] == "ZZ-2")
        self.assertEqual(row["project"], "other")
        queued = self.root / "runs/queue/desktop"
        queued.mkdir(parents=True)
        queued.joinpath("1-zz3-impl.json").write_text(json.dumps(dict(task("ZZ-3"), repository={"name": "other"})))
        row = next(r for r in self.view(queues={"desktop": ["zz3-impl"]})[0]["issues"] if r["issue"] == "ZZ-3")
        self.assertEqual(row["project"], "other")

    def test_mark_uses_recorded_repository_before_prefix_and_default(self):
        self.cfg["projects"]["example-app"].update(pipeline=["impl", "gate"], tracker_flow={})
        self.cfg["projects"]["other"] = {"tracker": "none", "pipeline": ["impl", "review", "merge"]}
        active = self.root / "runs/active"
        config = self.root / "config.json"
        config.write_text(json.dumps(self.cfg))
        for source in ("closed", "active", "queued"):
            for key in ("ZZ-1", "APP-1"):
                with self.subTest(source=source, key=key):
                    # The newest closed identity wins; a current identity wins
                    # even when the old closed identity names the default repo.
                    old = self.closed(key, "impl", "example-app", "done")
                    old["closed_at"] = NOW - 120
                    Path(R.CLOSED_DIR, old["task_id"] + ".json").write_text(json.dumps(old))
                    record = dict(task(key, "review"), repository={"name": "other"})
                    if source == "closed":
                        record["closed_at"] = NOW - 60
                        directory = Path(R.CLOSED_DIR)
                    elif source == "active":
                        directory = active
                    else:
                        directory = self.root / "runs/queue/desktop"
                    directory.mkdir(parents=True, exist_ok=True)
                    filename = ("1-" if source == "queued" else "") + record["task_id"] + ".json"
                    path = directory / filename
                    path.write_text(json.dumps(record))
                    err = io.StringIO()
                    with patch.object(sys, "argv", ["route.py", "--config", str(config),
                                                  "--active-dir", str(active), "--mark", key,
                                                  "review", "approved", "--sha", SHA]), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                        self.assertEqual(R.main(), 0, err.getvalue())
                    marked = R.load_json(Path(R.ISSUES_DIR, key + ".json"))["steps"]["review"]
                    self.assertEqual(marked["state"], "approved")
                    self.assertEqual(marked["sha"], SHA)
                    tasks = [task(key, "review")] if source == "active" else []
                    queues = {"desktop": [record["task_id"]]} if source == "queued" else {}
                    row = next(r for r in self.view(tasks, queues=queues)[0]["issues"] if r["issue"] == key)
                    self.assertEqual(row["project"], "other")
                    self.assertIn("review", row["pipeline"])
                    path.unlink()

    def test_generic_pr_branch_matching_preserves_ci_events(self):
        self.closed("APP-1", "impl", outcome="done")
        self.prs["/checkouts/product"] = {"fix/APP-1": dict(pr(5, title="Fix widget"), checks={"pass": 0, "fail": 1, "pending": 0})}
        report, _, _ = self.view()
        row = report["issues"][0]
        self.assertEqual(row["issue"], "APP-1")
        self.assertFalse(row.get("unrouted"))
        obs, info, _ = E.observe((report, {}, {}))
        self.assertEqual(obs["pr:APP-1"], "open")
        self.assertEqual(obs["ci:APP-1"], "fail")
        self.assertTrue(any(kind == "CI_FAIL" for kind, _ in E.events({}, obs, info, first=True)))
        self.assert_kept(report, "APP-1")
        self.assertEqual(R.pr_issue("misc", "Fix widget for APP-1", self.cfg, "example-app"), "APP-1")

    def test_default_pr_observation_keys_survive_adding_a_profile(self):
        app = self.cfg["projects"].pop("app")
        self.prs["/checkouts/product"] = {"docs": pr(9, base="old"), "old": pr(8, "merged")}
        before, _, _ = self.view()
        prev, _, _ = E.observe((before, {}, {}))
        self.assertEqual(prev["base:#9"], "stale")
        self.cfg["projects"]["app"] = app
        report, _, _ = self.view()
        obs, info, _ = E.observe((report, {}, {}))
        self.assertEqual(obs, prev)
        self.assertFalse(any(kind == "STALE_BASE" for kind, _ in E.events(prev, obs, info)))
        self.assert_kept(report, "#9", prev)

    def test_rows_render_their_project_pipeline_and_tracker(self):
        # A legacy Linear mark is ignored by a tracker-none project.
        self.marks("MOB-1", linear="done")
        report, lookup, tracker = self.view([task("APP-1"), task("MOB-1", "review")])
        rows = report["issues"]
        self.assertEqual([r["issue"] for r in rows], ["APP-1", "MOB-1"])
        product, app = rows
        self.assertEqual(app["project"], "app")
        self.assertEqual(app["pipeline"], ["impl", "review", "merge"])
        self.assertNotIn("linear", app["steps"])
        self.assertFalse(app["completed"])
        self.assertEqual(product["steps"]["linear"]["state"], "in-progress")
        self.assertEqual(tracker.call_args_list[0].args, (["APP-1"],))
        self.assertEqual({c.args[0] for c in lookup.call_args_list}, set(self.prs))
        for text in ("\n".join(W.detail_lines(app)), "\n".join(W.issue_lines([app], 0, 200)), D.issue_table([app])):
            self.assertIn("review", text)
            self.assertNotIn("Linear", text)
            self.assertNotIn("gate", text)
            self.assertNotIn("staging", text)
        for text in ("\n".join(W.issue_lines(rows, 0, 200)), D.issue_table(rows)):
            self.assertLess(text.index("APP-1"), text.index("MOB-1"))

    def test_merge_finishes_only_the_pipeline_ending_in_merge(self):
        self.marks("MOB-1", review="approved")
        self.marks("APP-1", review="approved", gate="passed")
        self.prs["/checkouts/app"] = {"changes/MOB-1": pr(1, "merged")}
        self.prs["/checkouts/product"] = {"feature/APP-1": pr(1, "merged")}
        report, _, _ = self.view()
        rows = {r["issue"]: r for r in report["issues"]}
        self.assertTrue(rows["MOB-1"]["completed"])
        self.assertEqual(R.stage(rows["MOB-1"]), 3)
        self.assertFalse(rows["APP-1"]["completed"])
        self.assertEqual(rows["APP-1"]["steps"]["linear"]["state"], "in-review")
        self.assertIn("completed", "\n".join(W.issue_lines([rows["MOB-1"]], 0, 200)))
        self.assertNotIn("Linear", D.issue_table([rows["MOB-1"]]))

    def test_unconfigured_pr_is_empty_without_degradation(self):
        self.cfg["projects"]["bare"] = {"issue_prefix": "BARE", "tracker": "none"}
        report, lookup, tracker = self.view([task("BARE-1")])
        row = report["issues"][0]
        self.assertEqual(row["project"], "bare")
        self.assertEqual(row["pipeline"], ["impl", "review", "gate", "merge"])
        self.assertIsNone(row["pr"])
        self.assertNotIn("degraded", report)
        self.assertEqual(lookup.call_count, 2)
        tracker.assert_not_called()
        with patch.object(R.subprocess, "run", side_effect=AssertionError("no configured checkout")):
            self.assertEqual(R.pr_states(), {})
        self.cfg["default_repo"] = "unprofiled"
        report, _, tracker = self.view([task("ZZ-1")])
        self.assertEqual(report["issues"][0]["project"], "unprofiled")
        self.assertNotIn("degraded", report)
        tracker.assert_not_called()

    def test_pr_lookup_uses_checkout_specific_caches(self):
        calls = []
        def gh(argv, **kw):
            calls.append((kw["cwd"], argv[argv.index("--state") + 1]))
            number = 1 if kw["cwd"] == "/checkouts/product" else 2
            return SimpleNamespace(returncode=0, stdout=json.dumps([{
                "number": number, "headRefName": "same-branch", "state": "OPEN", "headRefOid": SHA}]), stderr="")
        with patch("shutil.which", return_value="/bin/gh"), patch.object(R.subprocess, "run", side_effect=gh):
            self.assertEqual(R.pr_states("/checkouts/product")["same-branch"]["number"], 1)
            self.assertEqual(R.pr_states("/checkouts/app")["same-branch"]["number"], 2)
            for cache in (self.root / "runs").glob(".pr-cache-*.json"):
                os.utime(cache, (NOW, NOW))
            self.assertEqual(R.pr_states("/checkouts/product")["same-branch"]["number"], 1)
        self.assertEqual(calls, [(c, s) for c in self.prs for s in ("open", "all")])

    def test_env_override_remains_a_single_project_lookup(self):
        with patch.dict("os.environ", {"ROUTE_PR_REPO": "/legacy"}), \
                patch("shutil.which", return_value="/bin/gh"), \
                patch.object(R.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="[]")) as gh:
            R.pr_states("/checkouts/app")
            self.assertEqual(gh.call_args.kwargs["cwd"], "/legacy")
            report, lookup, _ = self.view([task("APP-1"), task("MOB-1")])
            self.assertEqual(lookup.call_count, 1)
            self.assertTrue(all(r["pr"] is None for r in report["issues"]))

    def test_branch_and_title_matching_follow_template_and_prefix(self):
        def match(branch, title=""):
            return R.pr_issue(branch, title, self.cfg, "app")
        self.assertEqual(match("changes/MOB-12"), "MOB-12")
        self.assertEqual(match("changes/MOB-12-native"), "MOB-12")
        self.assertEqual(match("misc", "[MOB-12] work"), "MOB-12")
        self.assertEqual(match("feature/MOB-12"), "MOB-12")
        self.assertEqual(match("changes/APP-12", "APP-12: work"), "APP-12")
        self.cfg["projects"]["app"]["worktree"]["branch"] = "changes/{issue}/work"
        self.assertEqual(match("changes/MOB-12-native/work"), "MOB-12")

    def test_stacked_base_lookup_does_not_cross_projects(self):
        self.prs["/checkouts/product"] = {"shared-base": pr(1, "merged", "APP-9: base")}
        self.prs["/checkouts/app"] = {"changes/MOB-1": pr(2, base="shared-base")}
        report, _, _ = self.view([task("MOB-1")])
        self.assertIsNone(report["issues"][0]["pr"]["stale_base"])
        self.prs["/checkouts/app"]["shared-base"] = pr(3, "merged")
        report, _, _ = self.view([task("MOB-1")])
        self.assertEqual(report["issues"][0]["pr"]["stale_base"]["number"], 3)

    def test_unrouted_pr_numbers_are_distinct_across_projects(self):
        self.prs["/checkouts/product"] = {"docs": pr(1)}
        self.prs["/checkouts/app"] = {"docs": pr(1)}
        report, _, _ = self.view()
        self.assertEqual({r["issue"] for r in report["issues"]}, {"#1", "app#1"})
        self.assertEqual({r["project"] for r in report["issues"]}, {"example-app", "app"})

    def test_events_for_both_projects_and_scoped_lookup_failure(self):
        for project, branch in (("product", "feature/APP-1"), ("app", "changes/MOB-1")):
            self.prs["/checkouts/" + project] = {branch: pr(1)}
        before, _, _ = self.view([task("APP-1"), task("MOB-1")])
        prev, _, _ = E.observe((before, {}, {}))
        for prs in self.prs.values():
            next(iter(prs.values()))["state"] = "merged"
        after, _, _ = self.view([task("APP-1"), task("MOB-1")])
        obs, info, bad = E.observe((after, {}, {}))
        batch = E.events(prev, E.carry_forward(prev, obs, info, bad), info)
        self.assertEqual({text for kind, text in batch if kind == "MERGED"}, {"APP-1 #1", "MOB-1 #1"})
        self.prs["/checkouts/product"] = None
        # Even a PR-only row that disappears on an outage retains its state.
        blip, _, _ = self.view([task("MOB-1")])
        obs, info, bad = E.observe((blip, {}, {}))
        kept = E.carry_forward(prev, obs, info, bad)
        self.assertEqual(kept["pr:APP-1"], "open")
        self.assertEqual(kept["pr:MOB-1"], "merged")
        batch = E.events(prev, kept, info)
        self.assertEqual([text for kind, text in batch if kind == "MERGED"], ["MOB-1 #1"])

    def test_multiple_linear_projects_use_their_own_flow_and_keys(self):
        self.cfg["projects"]["app"].update(tracker="linear", tracker_flow={
            "active": "building", "after": {"review": "checking", "merge": "done"},
            "rank": {"building": 0, "checking": 1, "done": 2}})
        self.marks("MOB-1", review="approved")
        report, _, tracker = self.view([task("APP-1")], live={"MOB-1": "building"})
        self.assertEqual({tuple(c.args[0]) for c in tracker.call_args_list}, {("MOB-1",), ("APP-1",)})
        app = next(r for r in report["issues"] if r["project"] == "app")
        self.assertEqual(app["steps"]["linear"]["behind"], "checking")
        self.assertEqual(R.expected_linear(app), "checking")

    def test_tracker_none_finished_issue_can_be_reaped_without_pr(self):
        self.marks("MOB-1", merge="merged")
        report, _, _ = self.view()
        report["untracked"] = [{"issue": "MOB-1", "name": "mob1-impl", "host": "desktop", "status": "idle",
                                "active_at": NOW - 60}]
        cands = RP.candidates(report, last={}, now=NOW)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["reason"], "merge merged")

    def test_custom_last_step_and_live_worktree_role(self):
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "qa", "ship"]
        self.marks("MOB-1", impl="done", qa="approved")
        report, _, _ = self.view([task("MOB-2", "qa")])
        app = next(r for r in report["issues"] if r["issue"] == "MOB-1")
        self.assertEqual(R.stage(app), 2)
        self.assertFalse(app["completed"])
        self.assertNotIn("merge", D.issue_table([app]))
        self.marks("MOB-1", ship="done")
        report, _, _ = self.view()
        self.assertTrue(report["issues"][0]["completed"])
        self.assertEqual(R.worktree_issue("/repos/app/tickets/MOB-3-native", self.cfg), "MOB-3")
        report = {"tasks": [], "untracked": [{"name": "helper", "cwd": "/repos/app/tickets/MOB-3", "status": "working"}]}
        with patch.object(R, "pr_states", return_value={}), patch.object(R, "linear_states", return_value={}):
            rows = R.issues_view(report, cfg=self.cfg)
        self.assertEqual(rows[0]["project"], "app")
        self.assertEqual(rows[0]["steps"]["impl"]["cls"], "working")
        self.assertEqual(R.task_step("mob3-native-qa-2", ["impl", "qa", "ship"]), "qa")
        self.assertFalse(R.is_completed(dict(rows[0], steps={"ship": {"state": "done", "cls": "stale"}})))

    def test_mark_validates_the_issue_project(self):
        cfg_path = self.root / "config.json"
        cfg_path.write_text(json.dumps(self.cfg))
        for key, step, rc in (("MOB-1", "merge", 0), ("MOB-1", "gate", 2), ("MOB-1", "linear", 2),
                              ("APP-1", "linear", 0), ("APP-1", "staging", 0)):
            with self.subTest(key=key, step=step), \
                    patch.object(sys, "argv", ["route.py", "--config", str(cfg_path), "--mark", key, step, "done", "--sha", SHA]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(R.main(), rc)
        self.cfg["projects"]["app"]["pipeline"] = ["impl", "ship"]
        cfg_path.write_text(json.dumps(self.cfg))
        with patch.object(sys, "argv", ["route.py", "--config", str(cfg_path), "--mark", "MOB-1", "ship", "done"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(), 0)

    def test_invalid_view_profiles_fail_early(self):
        for field, value in (("pipeline", []), ("pipeline", ["impl", "impl"]), ("pipeline", ["<script>"]),
                             ("tracker", "unknown"), ("tracker_flow", {"after": {"staging": "done"}})):
            with self.subTest(field=field, value=value):
                cfg = copy.deepcopy(self.cfg)
                cfg["projects"]["app"][field] = value
                with self.assertRaises(R.Invalid):
                    R.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
