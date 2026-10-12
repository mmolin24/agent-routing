#!/usr/bin/env python3
"""Dry-run router: validate a task contract, pick a host, print the plan.

It never launches workers, sends prompts, or changes checkouts. The only thing
it can execute is the read-only scripts/preflight.sh, and only when
--run-preflight is passed. A routing decision is a recommendation, not an
authorization to execute: a human reviews every rendered command.

Exit: 0 routed (plan ready for human review), 3 blocked, 2 invalid input.
Python 3.10+ (standard library only). The readers piped to other machines
(agent_profile.py, host_load.py, probe.py, preflight.sh) stay 3.8/bash-3.2
compatible because the far side may fall back to its system interpreter.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit(f"route.py needs Python 3.10+ (this is {sys.version.split()[0]})")

import argparse
import calendar
import functools
import glob
import hashlib
import json
import os
import platform

import re
import shlex
import subprocess
import time
import unicodedata

import host_identity as H

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Without a session bus, repeated probes can spawn persistent D-Bus daemons and
# exhaust resources. Children inherit "disabled:" unless a real bus is set.
if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"
PREFLIGHT = os.path.join(ROOT, "scripts", "preflight.sh")

REQUIRED_FIELDS = {
    "contract_version": int, "task_id": str, "summary": str,
    "authorization": dict, "routing": dict, "repository": dict,
    "project_commit": str, "worktree": dict, "worker": dict,
    "resources": dict, "stop_condition": str,
}
# The project's own docs and skills own the workflow (verification, PR shape,
# workpad, model choice). The contract only carries what routing needs.
OPTIONAL_LISTS = ("commands", "evidence_required")
TAG_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")   # capability tags come from the host config
STRATEGIES = {"pnpm-worktree-issue", "git-worktree", "git-detached", "existing-checkout"}
DEPS_MODES = {"link", "install", "none"}
ENV_MODES = {"link", "none"}
MODES = {"agent", "command"}
# project-workflow: the worker follows the project's own issue workflow up to
# stop_condition (for example-app that can include its draft-PR flow); merge,
# deploy and status transitions stay with the parent/human per the project.
SCOPES = {"read-only", "worktree-write", "project-workflow"}
RUNTIMES = {"none", "compose"}
LOAD_CLASSES = {"light", "heavy"}


def is_heavy(task):
    """Heavy = full suites, readiness gates, Compose stacks: the CPU/memory peaks.
    Docker work (integration tests) is heavy unless explicitly marked light."""
    r = task["resources"]
    if r.get("runtime") == "compose" or r.get("class") == "heavy":
        return True
    return r.get("class") != "light" and "docker" in task["routing"].get("requires", [])
NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
# Native worker CLI flags for model/effort (codex 0.16x, claude 2.1.x help).
NATIVE_MODEL_ARGS = {
    "codex": lambda m, e: (["-m", m] if m else []) + (["-c", f"model_reasoning_effort={e}"] if e else []),
    "claude": lambda m, e: (["--model", m] if m else []) + (["--effort", e] if e else []),
}
# Free-text fields where line breaks are allowed.
PROSE_FIELDS = re.compile(r"^(summary|stop_condition|instructions|resources\.notes|authorization\.(allowed|forbidden)\[\d+\]"
                          r"|evidence_required\[\d+\])$")
# fullmatch everywhere: `$` alone accepts a trailing newline.
SHA_RE = re.compile(r"[0-9a-f]{40}")
AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ISSUE_RE = re.compile(r"[A-Za-z]{2,10}-[0-9]+")
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BRANCH_RE = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)(?!.*\.lock$)[A-Za-z0-9._/-]{1,128}(?<![/.])")
ABS_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+")
REL_PATH_RE = re.compile(r"(?!/)(?!(?:.*/)?\.\.(?:/|$))[A-Za-z0-9._/-]+")
SSH_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
CWD_RE = re.compile(r"<WORKTREE>(?:/(?!(?:.*/)?\.\.(?:/|$))[A-Za-z0-9._/-]+)?")

# TRIPWIRE, not a security boundary: catches honest mistakes in commands[].run.
# Shell text can always evade a regex; the real controls are human review of
# every rendered command and the authorization recorded in the task.
_GIT = r"\bgit(?:\s+(?:-C\s+\S+|-c\s+(?:'[^']*'|\"[^\"]*\"|\S)+|-[pP]|--[a-z-]+(?:=\S+)?))*\s+"
FORBIDDEN_CMD_RE = re.compile(
    _GIT + r"(?:push|pull|reset|clean|checkout|switch|restore|stash|rebase|branch\s+-[dDmM]"
    r"|branch\s+--delete|worktree\s+(?:remove|prune)|update-ref|filter-branch)\b"
    r"|" + _GIT + r"fetch\b[^;&|]*\s[^\s:]*:(?!//)(?!refs/remotes/)"      # fetch into a local branch
    r"|\b(?:tofu|terraform)\b[^;&|]*\s(?:apply|destroy|import|state)\b"
    r"|\bherdr\b[^;&|]*\s(?:server\s+stop|session\s+(?:stop|delete)|(?:pane|tab|workspace)\s+close"
    r"|worktree\s+remove|machine\s+(?:add|remove|enable|disable))\b"
    r"|\bgh\s+(?:pr\s+(?:create|merge)|release\s+create|workflow\s+run)\b"
    r"|\b(?:render|fly|flyctl|vercel|netlify)\s+deploy\b|\bkubectl\s+(?:apply|delete)\b"
    r"|\bdocker\s+compose\b[^;&|]*\sdown\b[^;&|]*\s(?:-v|--volumes)\b"
    r"|\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)*-[a-zA-Z]*f|\brm\s+(?:-[a-zA-Z]*f[a-zA-Z]*\s+)*-[a-zA-Z]*[rR]")
PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


class Invalid(Exception):
    pass


class Raw(str):
    """An argument the router itself emits unquoted (never task-supplied)."""


def load_json(path):
    with open(path) as f:
        return json.load(f)


def clean_git_env():
    """Ignore inherited GIT_* (GIT_DIR etc.) and stop at this repo's parent."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(ROOT)
    return env


@functools.cache
def routing_ref():
    """Identify the routing rules used: git HEAD of THIS repo, else a hash."""
    env = clean_git_env()
    git = ["git", "--no-optional-locks", "-C", ROOT]
    try:
        top = subprocess.run(git + ["rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True, env=env).stdout.strip()
        if os.path.realpath(top) == os.path.realpath(ROOT):
            head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True, env=env).stdout.strip()
            st = subprocess.run(git + ["status", "--porcelain"], capture_output=True,
                                text=True, check=True, env=env).stdout.strip()
            return head + ("+dirty" if st else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    h = hashlib.sha256()
    for rel in ("AGENTS.md", "docs/task-contract.md", "scripts/route.py",
                "scripts/preflight.sh", "config/hosts.example.json"):
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(rel.encode() + b"\0" + f.read())
    return "no-git:sha256:" + h.hexdigest()[:12]


def detect_orchestrator(cfg):
    try:
        return H.detect_host(cfg)
    except ValueError as e:
        raise Invalid(str(e)) from e


def default_linux_host(cfg):
    try:
        return H.default_linux_host(cfg)
    except ValueError as e:
        raise Invalid(str(e)) from e


def _strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def bad_chars(path, s):
    """Control, format (bidi/zero-width) and line/paragraph separator chars."""
    prose = bool(PROSE_FIELDS.match(path))
    allowed = "\t\n\r\u200c\u200d" if prose else "\t"
    return [c for c in s if c not in allowed and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
            and not (prose and 0xE0020 <= ord(c) <= 0xE007F)]


def one_of(val, choices):
    return isinstance(val, str) and val in choices


def project_profile(cfg, repo):
    """Project behavior lives here; host repos only describe local checkouts."""
    if not isinstance(repo, str):
        return {}
    profile = dict((cfg or {}).get("projects", {}).get(repo, {}))
    prefix = profile.get("issue_prefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z]{2,10}", prefix):
            raise Invalid(f"config: projects.{repo}.issue_prefix must match [A-Za-z]{{2,10}}")
        profile["issue_prefix"] = prefix.upper()
    pipeline = profile.get("pipeline", DEFAULT_PIPELINE)
    if not isinstance(pipeline, list) or len(pipeline) < 2:
        raise Invalid(f"config: projects.{repo}.pipeline must have at least two steps and start with impl")
    for step in pipeline:
        if not isinstance(step, str) or not re.fullmatch(r"[a-z]{2,20}", step) or step in ("linear", "none"):
            raise Invalid(f"config: projects.{repo}.pipeline has invalid step {step!r}; "
                          "use a single lowercase word matching [a-z]{2,20} (except linear and none)")
    if len(set(pipeline)) != len(pipeline):
        raise Invalid(f"config: projects.{repo}.pipeline must contain unique step names")
    if pipeline[0] != "impl":
        raise Invalid(f"config: projects.{repo}.pipeline must have at least two steps and start with impl")
    return profile


def missing_helper_profile(repo):
    return Invalid(f"config: {repo} uses the issue worktree helper but has no projects.{repo} profile; "
                   "add one (docs/task-contract.md, Projects)")


def require_helper_profile(cfg, repo):
    wt = project_profile(cfg, repo).get("worktree", {})
    if wt.get("kind") != "helper" or not wt.get("create"):
        raise missing_helper_profile(repo)


def validate_config(cfg):
    """Reject legacy helpers and malformed templates before routing or rendering."""
    projects = cfg.get("projects", {})
    default_linux_host(cfg)
    prefixes = {}
    for host in cfg.get("hosts", {}).values():
        for repo, local in host.get("repos", {}).items():
            if local.get("issue_worktree") == "pnpm-worktree-issue" and repo not in projects:
                raise missing_helper_profile(repo)
    for repo in projects:
        profile = project_profile(cfg, repo)
        prefix = profile.get("issue_prefix")
        if prefix:
            if prefix in prefixes:
                raise Invalid(f"config: duplicate issue_prefix {prefix!r} in projects.{prefixes[prefix]} "
                              f"and projects.{repo} (case-insensitive)")
            prefixes[prefix] = repo
        pipeline = profile.get("pipeline", DEFAULT_PIPELINE)
        if profile.get("tracker", "none") not in ("linear", "none"):
            raise Invalid(f"config: projects.{repo}.tracker must be linear or none")
        flow = profile.get("tracker_flow", {})
        if not isinstance(flow, dict) or not isinstance(flow.get("after", {}), dict) or not isinstance(flow.get("rank", {}), dict):
            raise Invalid(f"config: projects.{repo}.tracker_flow must contain after and rank mappings")
        states = list(flow.get("after", {}).values()) + ([flow["active"]] if "active" in flow else [])
        if (any(step not in pipeline for step in flow.get("after", {})) or
                any(not isinstance(s, str) or not s for s in states) or
                any(not isinstance(s, str) or not isinstance(rank, int) or isinstance(rank, bool)
                    for s, rank in flow.get("rank", {}).items())):
            raise Invalid(f"config: projects.{repo}.tracker_flow has invalid steps, statuses or ranks")
        values = {"pm": profile.get("package_manager", ""), "issue": profile.get("issue_prefix", "XX") + "-1",
                  "commit": "a" * 40, "deps": "link", "env": "link"}
        for section, keys in (("worktree", ("dir", "branch", "create", "check")),
                              ("staging", ("worktree_dir",)),
                              ("brief", ("gate_example", "focused_example"))):
            for key in keys:
                if key in profile.get(section, {}):
                    full_key = f"{section}.{key}"
                    result = render_template(profile[section][key], values, repo=repo, key=full_key)
                    if full_key in ("worktree.dir", "staging.worktree_dir"):
                        validate_template_path(result, repo, full_key)
                        validate_template_location(cfg, repo, result, full_key)


def load_config(path):
    cfg = load_json(path)
    validate_config(cfg)
    return cfg


def package_manager(cfg, repo):
    return project_profile(cfg, repo).get("package_manager")


def worktree_profile(cfg, repo, host=None):
    profile = project_profile(cfg, repo).get("worktree")
    if profile is not None:
        return dict({"kind": "git", "dir": "{issue}", "branch": "feature/{issue}"}, **profile)
    if repo not in cfg.get("projects", {}) and any(
            h.get("repos", {}).get(repo, {}).get("issue_worktree") == "pnpm-worktree-issue"
            for h in cfg.get("hosts", {}).values()):
        raise missing_helper_profile(repo)
    return {"kind": "git", "dir": "{issue}", "branch": "feature/{issue}"}


def pm_command(cfg, repo, host):
    pm = package_manager(cfg, repo)
    local = cfg["hosts"][host]["repos"][repo]
    return local.get("pm_cmd", local.get("pnpm_cmd", "pnpm") if pm == "pnpm" else pm or "")


def template_values(cfg, repo, issue, commit="", deps="link", env="link", host=None):
    return {"pm": pm_command(cfg, repo, host) if host else package_manager(cfg, repo) or "",
            "issue": issue, "commit": commit, "deps": deps, "env": env}


def render_template(template, values, shell=False, *, repo, key):
    """Quote substituted shell arguments; pm is a configured command, not one argument."""
    if shell:
        values = {k: v if k == "pm" else shlex.quote(v) for k, v in values.items()}
    try:
        return template.format(**values)
    except (IndexError, AttributeError, ValueError, KeyError) as e:
        raise Invalid(f"config: projects.{repo}.{key}: invalid template {template!r}: {e}") from e


def validate_template_path(path, repo, key):
    if not REL_PATH_RE.fullmatch(path) or os.path.normpath(path) == "." or ".." in path.split("/"):
        raise Invalid(f"config: projects.{repo}.{key} must render a non-empty relative path "
                      "of [A-Za-z0-9._/-], excluding . and .. segments")
    return path


def template_path(template, values, repo, key):
    return validate_template_path(render_template(template, values, repo=repo, key=key), repo, key)


def validate_template_location(cfg, repo, path, key):
    helper = key == "worktree.dir" and worktree_profile(cfg, repo)["kind"] == "helper"
    for host in cfg.get("hosts", {}).values():
        local = host.get("repos", {}).get(repo, {})
        root = local.get("path")
        if root:
            base = root if helper else local.get("worktree_root", root)
            if os.path.normpath(os.path.join(base, path)) == os.path.normpath(root):
                raise Invalid(f"config: projects.{repo}.{key} resolves to the repo root")


def task_template_values(task, cfg, host):
    wt = task["worktree"]
    return template_values(cfg, task["repository"]["name"], wt.get("issue") or issue_key(task["task_id"]) or "",
                           task["project_commit"], wt.get("deps", "link"), wt.get("env", "link"), host)


def issue_pattern(cfg, repo):
    prefix = project_profile(cfg, repo).get("issue_prefix")
    return re.compile(re.escape(prefix) + r"-[0-9]+") if prefix else ISSUE_RE


def command_run(run, task, cfg, host):
    """Resolve a leading package-manager command the same way in plan and dispatch."""
    repo = task["repository"]["name"]
    pnpm = cfg["hosts"][host]["repos"][repo].get("pnpm_cmd")
    if pnpm and re.match(r"^pnpm(?=\s|$)", run):
        return re.sub(r"^pnpm(?=\s|$)", lambda _: pnpm, run)
    pm = package_manager(cfg, repo)
    if pm:
        return re.sub(r"^" + re.escape(pm) + r"(?=\s)", lambda _: pm_command(cfg, repo, host), run)
    return run


def validate(task, cfg=None):
    """Return a list of contract errors (empty means structurally valid)."""
    if not isinstance(task, dict):
        return ["task must be a JSON object"]
    if cfg is not None:
        validate_config(cfg)
    errors = []
    for k, typ in REQUIRED_FIELDS.items():
        if k not in task:
            errors.append(f"missing field: {k}")
        elif not isinstance(task[k], typ) or (typ is int and isinstance(task[k], bool)):
            errors.append(f"{k} must be {typ.__name__}")
    for k in OPTIONAL_LISTS:
        if k in task and not isinstance(task[k], list):
            errors.append(f"{k} must be a list")
    if errors:
        return errors
    for path, s in _strings(task):
        if bad_chars(path, s):
            errors.append(f"{path} contains control/format characters")

    if not TASK_ID_RE.fullmatch(task["task_id"]):
        errors.append("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if not SHA_RE.fullmatch(task["project_commit"]):
        errors.append("project_commit must be an exact 40-hex commit, not a branch name")

    auth = task["authorization"]
    if not one_of(auth.get("scope"), SCOPES):
        errors.append(f"authorization.scope must be one of {sorted(SCOPES)}")
    if not isinstance(auth.get("granted_by"), str) or not auth.get("granted_by"):
        errors.append("authorization.granted_by is required")
    for key in ("allowed", "forbidden"):
        v = auth.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errors.append(f"authorization.{key} must be a list of strings")

    routing = task["routing"]
    req = routing.get("requires")
    if not isinstance(req, list) or not all(isinstance(t, str) for t in req):
        errors.append("routing.requires must be a list of tags")
    elif not all(TAG_RE.fullmatch(t) for t in req):
        errors.append("routing.requires tags must match [a-z][a-z0-9-]*")
    if routing.get("requested_host") is not None and not isinstance(routing["requested_host"], str):
        errors.append("routing.requested_host must be a string or null")
    if not isinstance(routing.get("allow_behind", False), bool):
        errors.append("routing.allow_behind must be true or false")

    if not isinstance(task["repository"].get("name"), str):
        errors.append("repository.name is required")

    w = task["worker"]
    if not isinstance(w.get("name"), str) or not AGENT_NAME_RE.fullmatch(w["name"]):
        errors.append("worker.name must match [a-z][a-z0-9_-]{0,31}")
    if not isinstance(w.get("kind"), str) or not re.fullmatch(r"[a-z]+", w["kind"]):
        errors.append("worker.kind is required (e.g. codex)")
    for key in ("model", "effort"):
        if w.get(key) is not None and not (isinstance(w[key], str) and NAME_TOKEN_RE.fullmatch(w[key])):
            errors.append(f"worker.{key} must be a simple token or null")

    mode = task.get("mode", "agent")
    if not one_of(mode, MODES):
        errors.append(f"mode must be one of {sorted(MODES)}")

    wt = task["worktree"]
    strategy = wt.get("strategy")
    if not one_of(strategy, STRATEGIES):
        errors.append(f"worktree.strategy must be one of {sorted(STRATEGIES)}")
    elif strategy == "pnpm-worktree-issue":
        if cfg is not None and isinstance(task["repository"].get("name"), str):
            require_helper_profile(cfg, task["repository"]["name"])
        if not isinstance(wt.get("issue"), str) or not issue_pattern(cfg, task["repository"].get("name")).fullmatch(wt["issue"]):
            errors.append("worktree.issue must match the project issue prefix (e.g. APP-758)")
        if not one_of(wt.get("deps", "link"), DEPS_MODES):
            errors.append(f"worktree.deps must be one of {sorted(DEPS_MODES)}")
        if not one_of(wt.get("env", "link"), ENV_MODES):
            errors.append(f"worktree.env must be one of {sorted(ENV_MODES)}")
        if cfg is not None and not errors:
            for host, hc in cfg["hosts"].items():
                if task["repository"]["name"] in hc.get("repos", {}):
                    worktree_dir(task, cfg, host)
    elif strategy in ("git-worktree", "git-detached"):
        if not isinstance(wt.get("path"), str) or not (ABS_PATH_RE.fullmatch(wt["path"])
                                                        or REL_PATH_RE.fullmatch(wt["path"])):
            errors.append("worktree.path must be absolute, or relative to the repo (no ..), of [A-Za-z0-9._/-]")
        if strategy == "git-worktree" and (not isinstance(wt.get("branch"), str) or not BRANCH_RE.fullmatch(wt["branch"])):
            errors.append("worktree.branch must be a valid branch name")
    if strategy != "pnpm-worktree-issue" and "issue" in wt:
        if not isinstance(wt["issue"], str) or not issue_pattern(cfg, task["repository"].get("name")).fullmatch(wt["issue"]):
            errors.append("worktree.issue must match the project issue prefix (e.g. APP-758)")
    if not isinstance(wt.get("require_clean_base", False), bool):
        errors.append("worktree.require_clean_base must be true or false")

    cmds = task.get("commands", [])
    if mode == "command" and not cmds:
        errors.append("commands must not be empty in command mode")
    for i, c in enumerate(cmds):
        if not isinstance(c, dict) or not isinstance(c.get("run"), str) or not c["run"].strip():
            errors.append(f"commands[{i}].run is required")
            continue
        if c["run"].lstrip().startswith("-"):
            errors.append(f"commands[{i}].run must not start with '-'")
        if c["run"].rstrip().endswith("\\"):
            errors.append(f"commands[{i}].run must not end with a backslash")
        cwd = c.get("cwd", "<WORKTREE>")
        if not isinstance(cwd, str) or not CWD_RE.fullmatch(cwd):
            errors.append(f"commands[{i}].cwd must be <WORKTREE> or <WORKTREE>/relative/path")
        for flag in ("mutating", "long_running"):
            if not isinstance(c.get(flag, False if flag == "long_running" else None), bool):
                errors.append(f"commands[{i}].{flag} must be true or false")
        rr = c.get("ready_regex")
        if c.get("long_running") is True and mode == "command" and rr is None:
            errors.append(f"commands[{i}] is long_running in command mode: ready_regex is required")
        if rr is not None:
            try:
                ok = isinstance(rr, str) and rr.strip() and not rr.startswith("-") and re.compile(rr)
            except re.error:
                ok = False
            if not ok:
                errors.append(f"commands[{i}].ready_regex must be a non-empty valid regex not starting with '-'")
        if FORBIDDEN_CMD_RE.search(c["run"]):
            errors.append(f"commands[{i}].run trips the forbidden-command tripwire: {c['run']!r}")

    res = task["resources"]
    t = res.get("timeout_minutes")
    if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 24 * 60:
        errors.append("resources.timeout_minutes must be an integer 1..1440")
    ports = res.get("ports", [])
    if not isinstance(ports, list) or not all(isinstance(p, int) and not isinstance(p, bool) for p in ports):
        errors.append("resources.ports must be a list of integers")
    for key in ("compose_project", "database"):
        if res.get(key) is not None and not isinstance(res[key], str):
            errors.append(f"resources.{key} must be a string or null")
    if not one_of(res.get("class", "light"), LOAD_CLASSES):
        errors.append(f"resources.class must be one of {sorted(LOAD_CLASSES)}")
    if not one_of(res.get("runtime", "none"), RUNTIMES):
        errors.append(f"resources.runtime must be one of {sorted(RUNTIMES)}")
    slot = res.get("port_slot")
    if slot is not None and (not isinstance(slot, int) or isinstance(slot, bool) or slot < 1):
        errors.append("resources.port_slot must be an integer >= 1 (slot 0 is the main checkout)")
    return errors


def policy_blockers(task, cfg):
    """Authorization checks: valid contracts that this scope does not permit."""
    out = []
    scope = task["authorization"]["scope"]
    wt = task["worktree"]
    if scope == "read-only":
        if wt["strategy"] != "existing-checkout":
            out.append("read-only scope cannot create a worktree; use existing-checkout or get worktree-write")
        for c in task.get("commands", []):
            if c["mutating"]:
                out.append(f"read-only scope but command is mutating: {c['run']!r}")
    if wt["strategy"] == "existing-checkout":
        if any(c["mutating"] for c in task.get("commands", [])):
            out.append("mutating commands need an isolated worktree, not the existing checkout")
        if scope != "read-only":
            out.append(f"scope {scope} needs an isolated worktree, not the existing checkout")
    if task["resources"].get("runtime") == "compose" and wt["strategy"] == "existing-checkout":
        out.append("a compose runtime needs its own worktree")
    if (package_manager(cfg, task["repository"]["name"]) == "pnpm"
            and wt["strategy"] == "pnpm-worktree-issue" and wt.get("deps", "link") == "link"):
        for c in task.get("commands", []):
            if re.search(r"\bpnpm\b.*\s(?:install|i|add|remove|update|up)\b", c["run"]):
                out.append(f"deps=link symlinks node_modules into the main checkout; {c['run']!r} would "
                           "write there. Use worktree.deps=install")
    if wt["strategy"] in ("git-worktree", "git-detached"):
        for h in cfg["hosts"].values():
            repo = h.get("repos", {}).get(task["repository"]["name"], {})
            p = wt["path"] if os.path.isabs(wt["path"]) else \
                os.path.join(repo.get("worktree_root", repo.get("path", "/")), wt["path"])
            if repo.get("path") and os.path.normpath(p) == os.path.normpath(repo["path"]):
                out.append("worktree.path is the main checkout itself")
    return out


def choose_host(task, cfg, placement=None):
    """Return (host, rule, blockers). Never silently switches hosts.

    With no explicit request, a task goes to a host that has every required tag.
    When more than one does, `placement` (host -> {"hot", "warn", "open"}) spreads
    the work: hot hosts are skipped, then (for heavy work) hosts with no free heavy
    slot or a queue already waiting, then the lowest open-work score wins (open
    tasks / share, plus a step for a warn-level host); ties go to the default host."""
    routing = task["routing"]
    requested = routing.get("requested_host") or task.get("_queued_for")
    requires = set(routing.get("requires", []))
    hosts = cfg["hosts"]

    def missing_on(h):
        return requires - set(hosts[h]["capabilities"])

    unknown = requires - {t for h in hosts.values() for t in h.get("capabilities", [])}
    if unknown:
        return None, "unknown capability", [f"no host has {sorted(unknown)} (see config capabilities)"]

    if requested:
        if requested not in hosts:
            return None, "explicit host request", [f"unknown requested_host {requested!r}"]
        miss = missing_on(requested)
        blockers = [f"requested host {requested!r} lacks {sorted(miss)}; "
                    "not switching hosts — ask the requester"] if miss else []
        return requested, ("queued for this host" if not routing.get("requested_host")
                           else "explicit host request honored"), blockers

    default = default_linux_host(cfg)
    capable = [h for h in sorted(hosts) if not missing_on(h)]
    if not capable:
        return None, "no capable host", [f"no single host has all of {sorted(requires)}; split the task per host"]
    if len(capable) == 1:
        return capable[0], f"only host with {sorted(requires) or 'these requirements'}", []
    if not placement:
        pick = default if default in capable else capable[0]
        return pick, f"{pick}: default among capable hosts {capable} (no load data)", []
    cool = [h for h in capable if not (placement.get(h) or {}).get("hot")] or capable
    cool = [h for h in cool if not (placement.get(h) or {}).get("full")] or cool

    def score(h):
        pl = placement.get(h) or {}
        return (pl.get("open", 0) / float(hosts[h].get("share", 1.0)) + (0.5 if pl.get("warn") else 0.0),
                0 if h == default else 1)
    pick = min(cool, key=score)
    why = ", ".join(f"{h} {placement.get(h, {}).get('open', 0)} open"
                    f"{' hot' if placement.get(h, {}).get('hot') else ' warn' if placement.get(h, {}).get('warn') else ''}"
                    f"{' heavy-full' if placement.get(h, {}).get('full') else ''}"
                    for h in capable)
    return pick, f"load-shared across capable hosts ({why})", []


def reach_for(cfg, host, orch):
    reach = cfg["hosts"][host]["reach"][orch]
    if reach["mode"] in ("ssh", "machine") and not SSH_ALIAS_RE.fullmatch(reach.get("ssh_alias", "")):
        raise Invalid(f"config: bad ssh_alias for {host} from {orch}")
    return reach


def herdr_cmd(cfg, host, orch, *args, pre=""):
    """Render one herdr command for `host` as run from `orch`.

    Every argument is shell-quoted except __PLACEHOLDERS__ and Raw values the
    router builds itself. `pre` is a router-built shell prefix that runs in the
    same shell as herdr (locally, or on the remote side for ssh).
    """
    parts = []
    for a in args:
        if isinstance(a, Raw) or PLACEHOLDER_RE.fullmatch(a):
            parts.append(a)
        else:
            parts.append(shlex.quote(a))
    arg_str = " ".join(parts)
    # No 3.10-only syntax in this file: the version check above must be able to run.
    reach = reach_for(cfg, host, orch)
    if reach["mode"] == "local":
        return pre + "herdr " + arg_str
    if reach["mode"] == "machine":
        return pre + f"herdr --machine {shlex.quote(reach['selector'])} {arg_str}"
    if reach["mode"] == "ssh":
        # The remote shell re-parses the string, so quote the whole call once.
        return f"ssh -o BatchMode=yes {reach['ssh_alias']} " + shlex.quote(pre + cfg["hosts"][host]["herdr_bin"] + " " + arg_str)
    raise Invalid(f"config: unknown reach {reach!r} for {host}")


def herdr_prefix(cfg, host, orch):
    """What to type before a herdr subcommand to reach `host` from `orch`. herdr's
    --machine takes its own case-sensitive profile label (`Mac`), not the routing host
    name (`mac`), and herdr isn't on the Mac's non-interactive ssh PATH."""
    reach = reach_for(cfg, host, orch)
    if reach["mode"] == "ssh":       # quoted, so $HOME expands on the far side
        return f"ssh -o BatchMode=yes {reach['ssh_alias']} {shlex.quote(cfg['hosts'][host]['herdr_bin'])}"
    return herdr_cmd(cfg, host, orch).rstrip()


def herdr_prefixes(cfg, orch):
    """{host: prefix} for every host reachable from `orch`."""
    out = {}
    for h in cfg["hosts"]:
        try:
            out[h] = herdr_prefix(cfg, h, orch)
        except (KeyError, Invalid):
            pass
    return out


def required_tools(task, cfg, host):
    """Host base tools + tools behind each required capability + the worker CLI."""
    tools = list(cfg["hosts"][host].get("tools", []))
    for tag in task["routing"].get("requires", []):
        tools += cfg.get("capability_tools", {}).get(tag, [])
    if task.get("mode", "agent") == "agent":
        tools.append(task["worker"]["kind"])
    return list(dict.fromkeys(tools))


def herdr_group(task_id):
    """(workspace label, tab label) for a task: one Herdr workspace per issue with a tab
    per task, so app123-impl and app123-gate sit together as APP-123 > impl / gate."""
    key = issue_key(task_id)
    if not key or not re.match(r"^(?:rt-)?[a-z]+-?\d+(?:-|$)", task_id.lower()):
        return task_id, task_id
    rest = re.sub(r"^(?:rt-)?[a-z]+-?\d+-?", "", task_id.lower())
    parts = [p for i, p in enumerate(rest.split("-")) if p and (i == 0 or p != rest.split("-")[i - 1])]
    return key, "-".join(parts) or "work"    # --suffix staging + role staging -> "staging", not "staging-staging"


def preflight_invocation(cfg, host, orch, task, env=None):
    h = cfg["hosts"][host]
    repo = h["repos"][task["repository"]["name"]]
    args = [
        "--repo", repo["path"], "--expect-remote", repo["remote"],
        # In-place work must match exactly; worktree work only needs the commit.
        "--expect-commit" if task["worktree"]["strategy"] == "existing-checkout"
        else "--need-commit", task["project_commit"],
        *(["--behind-ref", "origin/main"] if task_step(task["task_id"]) == "gate" else []),   # a gate on a stale branch stops
        *(["--behind-warn"] if task_step(task["task_id"]) == "gate" and task["routing"].get("allow_behind") else []),
        "--tools", " ".join(required_tools(task, cfg, host)), "--herdr-bin", h["herdr_bin"],
        "--path-prepend", ":".join(h.get("path_prepend", [])),
    ]
    if task["worktree"]["strategy"] != "existing-checkout" and worktree_dir(task, cfg, host):
        args += ["--worktree-at", worktree_dir(task, cfg, host)]
    if task["worktree"].get("require_clean_base") is True:
        args.append("--require-clean")
    if task["authorization"]["scope"] == "project-workflow":   # the worker will commit
        args.append("--need-signing")
    ports = sorted({int(v) for k, v in (env or {}).items() if k.endswith("_PORT")} |
                   set(task["resources"].get("ports", [])))
    if ports:
        args += ["--ports-free", " ".join(str(p) for p in ports)]
    reach = reach_for(cfg, host, orch)
    if reach["mode"] == "local":
        return ["bash", PREFLIGHT] + args, None
    remote = "bash -s -- " + " ".join(shlex.quote(a) for a in args)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach["ssh_alias"], remote], PREFLIGHT


def run_preflight(argv, stdin_path):
    stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    try:
        p = subprocess.run(argv, stdin=stdin, capture_output=True, timeout=60, env=clean_git_env())
    except subprocess.TimeoutExpired:
        return {"preflight_status": "unreachable", "blockers": "preflight timed out"}
    finally:
        if stdin_path:
            stdin.close()
    out = {}
    for line in p.stdout.decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    if p.returncode == 255:
        out = {"preflight_status": "unreachable",
               "blockers": "ssh failed: " + p.stderr.decode(errors="replace").strip()[-300:]}
    elif p.returncode not in (0, 20):
        out["preflight_status"] = "error"
        out.setdefault("blockers", p.stderr.decode(errors="replace").strip()[-300:] or f"exit {p.returncode}")
    return out


def worktree_dir(task, cfg, host):
    """Absolute directory the work runs in, derived from the strategy."""
    wt = task["worktree"]
    repo_cfg = cfg["hosts"][host].get("repos", {}).get(task["repository"]["name"], {})
    repo = repo_cfg.get("path")
    if wt["strategy"] in ("git-worktree", "git-detached") and os.path.isabs(wt["path"]):
        return os.path.normpath(wt["path"])
    if not repo:
        return None
    if wt["strategy"] in ("git-worktree", "git-detached"):
        # Relative paths resolve against the repo's worktree_root (keep it outside
        # the checkout when the repo doesn't ignore it), else the repo itself.
        path = os.path.normpath(os.path.join(repo_cfg.get("worktree_root", repo), wt["path"]))
        if path == os.path.normpath(repo):
            raise Invalid(f"config: {task['repository']['name']} worktree path equals the repo root")
        return path
    if wt["strategy"] == "pnpm-worktree-issue":
        name = task["repository"]["name"]
        require_helper_profile(cfg, name)
        values = task_template_values(task, cfg, host)
        path = template_path(worktree_profile(cfg, name, host)["dir"], values, name, "worktree.dir")
        return os.path.normpath(os.path.join(repo, path))
    return os.path.normpath(repo)


def worktree_identity(task, cfg, host):
    """(path, branch) a task will occupy; None where it occupies nothing new."""
    wt = task["worktree"]
    if wt["strategy"] == "existing-checkout":
        return None, None
    if wt["strategy"] == "git-detached":                # no branch: a detached HEAD at project_commit
        return worktree_dir(task, cfg, host), None
    if wt["strategy"] == "git-worktree":
        branch = wt["branch"]
    else:
        repo = task["repository"]["name"]
        require_helper_profile(cfg, repo)
        values = task_template_values(task, cfg, host)
        branch = render_template(worktree_profile(cfg, repo, host)["branch"], values,
                                 repo=repo, key="worktree.branch")
    return worktree_dir(task, cfg, host), branch


def recorded_identity(task, cfg, host):
    """A recorded task keeps its occupied locations across profile changes."""
    identity = task.get("_occupies")
    if identity is not None:
        return identity["path"], identity["branch"]
    return worktree_identity(task, cfg, host)


def load_active(task, cfg, active_dir):
    """Return (problems, [(path, other_task, other_host)]) for open tasks."""
    problems, out = [], []
    for path in sorted(glob.glob(os.path.join(active_dir, "*.json"))):
        try:
            other = load_json(path)
        except (OSError, ValueError, RecursionError):
            problems.append(f"unreadable active task file {path}")
            continue
        if not isinstance(other, dict) or validate(other, cfg):
            problems.append(f"active task file {path} is not a valid contract; resolve before routing")
            continue
        if other["task_id"].lower() == task["task_id"].lower():
            problems.append(f"task {other['task_id']} is already open ({path}); close it before re-routing")
            continue
        sel = other.get("_occupies", {}).get("host", other.get("selected_host"))
        out.append((path, other, sel if one_of(sel, cfg["hosts"]) else choose_host(other, cfg)[0]))
    return problems, out


def stale_entries(active):
    """Open tasks past twice their timeout, timed from dispatch (else from when they were recorded)."""
    out = []
    for path, other, oh in active:
        age_min = elapsed_min(other)
        if age_min is None:
            age_min = (time.time() - os.path.getmtime(path)) / 60
        if age_min > 2 * other["resources"]["timeout_minutes"]:
            out.append(f"open task {other['task_id']} on {oh} has been open {age_min:.0f} min "
                       f"(timeout {other['resources']['timeout_minutes']}): if its worker has stopped, close it "
                       f"(--close {other['task_id']}); if it's still working, check its progress")
    return out


def runtime_env(task, cfg, host, slot):
    """Per-task env that isolates a project runtime (ports, image tags) on a host."""
    spec = cfg["hosts"][host]["repos"][task["repository"]["name"]].get("port_slots")
    if not spec:
        raise Invalid(f"config: {task['repository']['name']} on {host} has no port_slots for runtime=compose")
    env = {k: str(int(v) + slot * int(spec["step"])) for k, v in spec["base"].items()}
    bad = [k for k, v in env.items() if not 1 <= int(v) <= 65535]
    if bad:
        raise Invalid(f"config: slot {slot} puts {bad} outside 1..65535")
    subst = {"task_id": task["task_id"], "slot": str(slot)}
    for k, v in spec.get("extra_env", {}).items():
        env[k] = re.sub(r"\{(task_id|slot)\}", lambda m: subst[m.group(1)], str(v))
    return env


def report_dir(d, task_id):
    """A task's durable directory for reports and results: /tmp is wiped when the machine
    restarts, so temporary reports can be lost. Absolute on the
    target (from preflight's $HOME), else the $HOME form for the brief."""
    home = (d.get("preflight") or {}).get("home")
    return f"{home or '$HOME'}/.cache/agent-routing/reports/{task_id}"


def slot_ports(env):
    return {int(v) for k, v in env.items() if k.endswith("_PORT")}


def pick_slot(task, cfg, host, active):
    """Lowest slot not used by an open task and whose ports nobody has reserved."""
    spec = cfg["hosts"][host]["repos"][task["repository"]["name"]].get("port_slots")
    if not spec:
        return None, f"{task['repository']['name']} has no port_slots on {host}; runtime=compose can't be isolated there"
    used = {o["resources"].get("port_slot") for _, o, oh in active if oh == host}
    taken = {p for _, o, oh in active if oh == host for p in o["resources"].get("ports", [])}
    want = task["resources"].get("port_slot")
    candidates = [want] if want is not None else range(1, spec.get("max_slot", 0) + 1)
    for n in candidates:
        if n > spec.get("max_slot", 0) or n in used:
            continue
        if slot_ports(runtime_env(task, cfg, host, n)) & taken:
            continue
        return n, None
    if want is not None:
        return None, f"port_slot {want} is in use, reserved or above max_slot {spec.get('max_slot', 0)}"
    return None, f"no free port slot on {host} (max_slot {spec.get('max_slot', 0)})"


def collisions(task, host, cfg, active):
    """Compare against open task contracts (see docs/task-contract.md)."""
    found = []
    mine, wt = task["resources"], task["worktree"]
    for path, other, other_host in active:
        oid, o, owt = other["task_id"], other["resources"], other["worktree"]
        same_repo = task["repository"]["name"] == other["repository"]["name"]
        if other_host != host:
            # A branch is shared through the remote: two hosts must not own one.
            # Only tasks that may push (project-workflow) can collide this way.
            mb = worktree_identity(task, cfg, host)[1]
            both_push = {task["authorization"]["scope"], other["authorization"]["scope"]} == {"project-workflow"}
            if same_repo and mb and other_host and both_push and mb == recorded_identity(other, cfg, other_host)[1]:
                found.append(f"branch {mb} is already owned by {oid} on {other_host}")
            continue
        if other["worker"]["name"] == task["worker"]["name"]:
            found.append(f"worker {task['worker']['name']} already has open task {oid}")
        shared = set(mine.get("ports", [])) & set(o.get("ports", []))
        if shared:
            found.append(f"ports {sorted(shared)} in use by {oid}")
        for key in ("compose_project", "database"):
            a, b = mine.get(key), o.get(key)
            if a and b and a.lower() == b.lower():   # Compose lower-cases project names
                found.append(f"{key} {a} in use by {oid}")
        mp, mb = worktree_identity(task, cfg, host)
        op, ob = recorded_identity(other, cfg, host)
        if mp and mp == op:
            found.append(f"worktree path {mp} in use by {oid}")
        if same_repo and mb and mb == ob:
            found.append(f"branch {mb} in use by {oid}")
    return found


def handoff_note(task, d, cfg, host):
    """The routing part of the worker brief. The project's own AGENTS.md and
    skills own the workflow; this note only says where and with what."""
    repo_cfg = cfg["hosts"][host]["repos"][task["repository"]["name"]]
    auth = task["authorization"]
    profile = project_profile(cfg, task["repository"]["name"])
    brief = profile.get("brief", {})
    pm = package_manager(cfg, task["repository"]["name"])
    values = task_template_values(task, cfg, host)
    lines = [
        f"# Task {task['task_id']}: {task['summary']}",
        "",
        "## Routing (from agent-routing; not a workflow)",
        f"- Host: {host} ({cfg['hosts'][host].get('description', '')})",
        f"- Repository: {repo_cfg['path']} at {task['project_commit']}",
        f"- Work directory: {worktree_dir(task, cfg, host)}",
        *([f"- {pm}: use `{values['pm']}`"] if pm else []),
        f"- Routing ref: {d['routing_ref']}",
    ]
    if d.get("env"):
        lines.append(f"- Isolated runtime (port slot {d['port_slot']}); these are already exported in your shell:")
        lines += [f"  - `{k}={v}`" for k, v in d["env"].items()]
        for key, note in brief.get("env_notes", {}).items():
            if key in d["env"]:
                lines.append("  - " + note)
    extra_ports = task["resources"].get("ports", [])
    if extra_ports:
        lines.append(f"- Reserved host ports for this task: {', '.join(map(str, extra_ports))}. "
                     "Use these (e.g. for Metro); other ports may belong to another worker.")
    heavy = f"{cfg['hosts'][host].get('routing_repo', '~/projects/agent-routing')}/scripts/heavy"
    # A grant that forbids the gate (e.g. review) may run focused checks only: don't suggest the gate or Compose.
    no_gate = any("readiness gate" in f for f in auth.get("forbidden", []))
    key = "focused_example" if no_gate else "gate_example"
    example = render_template(brief.get(key, "<your full test command>"), values,
                              repo=task["repository"]["name"], key=f"brief.{key}")
    kinds = "full test suites, typechecks, " if no_gate else \
        "Docker or Compose (`docker compose up`, integration tests), full test suites, typechecks, "
    lines += [
        "",
        "## Heavy phases take turns on this machine",
        f"Wrap every heavy command in `{heavy}`, e.g. `{heavy} {example}`. "
        f"Heavy means {kinds}"
        "web builds or exports, and Playwright/browser smokes. It waits for a free heavy slot on this machine "
        f"(at most {cfg['hosts'][host].get('max_heavy') or 2} at once, first come first served), runs the command, "
        "and frees the slot when it ends. Everything else (reading, editing, focused single-file tests, model "
        "work) runs unwrapped and unlimited. If it says it's waiting, wait; don't run the command unwrapped.",
    ]
    if task["routing"].get("allow_behind") and task_step(task["task_id"]) == "gate":
        lines += ["", "## This gate may be behind origin/main",
                  "It was allowed to start behind main. Before anything else, in the worktree run "
                  "`git merge-tree --write-tree --quiet origin/main HEAD`: exit 0 means it merges cleanly, so "
                  "carry on; exit 1 means a conflict, so stop and report it (the branch needs a fix round)."]
    rd = report_dir(d, task["task_id"])
    lines += [
        "",
        "## Reports survive a restart; host faults stop the task",
        f"Write reports, verdicts and anything you must not lose under `$ROUTE_REPORT_DIR` (`{rd}`; "
        f"`mkdir -p` it first), never `/tmp`: `/tmp` is wiped when the machine restarts.",
    ]
    if "microsoft" in str((d.get("preflight") or {}).get("kernel", "")).lower():
        lines.append(
            "This machine is WSL: `/` can show free virtual-disk space even when the backing volume, Windows' C:, is full. "
            "A kernel I/O error, `SIGBUS`, a dockerd crash or containers vanishing mid-run is a **host fault**, not a "
            "test failure: run `df -h /mnt/c` and `dmesg | tail -20` (never `df /` as evidence), then stop and "
            "report `HOST_FAULT` with that output. Don't retry the step.")
    lines += [
        "",
        "## Workflow",
        f"Follow `{repo_cfg['path']}/AGENTS.md` and the project skill that matches this work. They own "
        "the workflow, verification, commit/PR shape, workpad and reporting; this note does not repeat them.",
        "",
        "## Authority and stop point",
        f"- Scope: {auth['scope']} (granted by {auth['granted_by']})",
    ]
    lines += [f"- Allowed: {x}" for x in auth.get("allowed", [])]
    lines += [f"- Forbidden: {x}" for x in auth.get("forbidden", [])]
    lines.append(f"- Stop point: {task['stop_condition']}")
    if task.get("commands"):
        lines += ["", "## Extra commands requested for this task"]
        lines += [f"- `{c['run']}` (in {c.get('cwd', '<WORKTREE>')})" for c in task["commands"]]
    lines += ["", "## If you start another agent in a new Herdr pane",
              f"Name the agent `{task['task_id'].split('-')[0]}-<role>` (e.g. `{task['task_id'].split('-')[0]}-tests`; "
              "lowercase, max 32 chars) and tag its pane so the orchestrator sees it under you: "
              "`herdr pane report-metadata <new-pane-id> --source agent-routing --token parent=$HERDR_PANE_ID`."]
    if task.get("instructions"):
        lines += ["", "## Task instructions (from the orchestrator)", task["instructions"].rstrip()]
    lines += ["", "## Also report",
              "Alongside the project's own handoff: host, work directory, and exact head SHA."]
    return "\n".join(lines) + "\n"


def record(task, d, note, active, cfg):
    """Write runs/tasks/<task_id>/ and <active>/<task_id>.json (git-ignored)."""
    host = d["selected_host"]
    path, branch = worktree_identity(task, cfg, host)
    identity = {"host": host, "path": path, "branch": branch}
    run_dir = os.path.join(ROOT, "runs", "tasks", task["task_id"])
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(active, exist_ok=True)
    marker = os.path.join(active, task["task_id"] + ".json")
    # Exclusive create: two routers racing for the same task can't both record it.
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    old_link = os.path.join(run_dir, "link.json")
    if os.path.exists(old_link):          # an earlier, closed run of this id: its link isn't this run's
        os.replace(old_link, os.path.join(run_dir, "link.prev.json"))
    t = dict(task, selected_host=d["selected_host"], routing_ref=d["routing_ref"])
    if d.get("port_slot"):
        t["resources"] = dict(task["resources"], port_slot=d["port_slot"],
                              ports=sorted(set(task["resources"].get("ports", [])) | slot_ports(d["env"])))
    for name, body in (("task.json", json.dumps(t, indent=2) + "\n"),
                       ("decision.json", json.dumps(d, indent=2) + "\n"),
                       ("handoff.md", note)):
        with open(os.path.join(run_dir, name), "w") as f:
            f.write(body)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(dict(t, _occupies=identity), indent=2) + "\n")
    return os.path.relpath(run_dir, ROOT)


def native_args(worker, cfg=None):
    """Native CLI flags: the config's per-kind worker_args (auto-approval) plus
    model/effort chosen by the project's routing guide. When the task names
    neither, the config's worker_defaults apply, so every machine starts a worker
    the same way instead of falling back to that machine's own CLI default."""
    dflt = (cfg or {}).get("worker_defaults", {}).get(worker["kind"], {})
    m, e = worker.get("model") or dflt.get("model"), worker.get("effort") or dflt.get("effort")
    args = list((cfg or {}).get("worker_args", {}).get(worker["kind"], []))
    if m or e:
        fn = NATIVE_MODEL_ARGS.get(worker["kind"])
        if not fn:
            raise Invalid(f"no native model/effort flags known for worker kind {worker['kind']!r}")
        args += fn(m, e)
    return ["--"] + args if args else []


def brief_prompt(cfg, host, orch, name, timeout_ms):
    """Prompt step: read the brief from a file, refuse to send an empty prompt."""
    pre = 'b="$(cat __BRIEF_FILE__)" && [ -n "$b" ] && '
    cmd = herdr_cmd(cfg, host, orch, "agent", "prompt", name, Raw('"$b"'),
                    "--wait", "--timeout", str(timeout_ms), pre=pre)
    where = ("on the TARGET host (" + host + ")") if reach_for(cfg, host, orch)["mode"] == "ssh" \
        else ("on the ORCHESTRATOR (" + orch + ")")
    return where, cmd


TYPED_MAX = 900   # bytes typed by `pane run`: macOS keeps 1024 (MAX_CANON) until a new tab's shell reads


def typed_blockers(task, cfg, host):
    """Every command `pane run` types must stay under TYPED_MAX bytes: macOS keeps 1024
    bytes of typed input until a new tab's shell reads it and drops the rest, done marker
    included, so the dispatch would wait out its timeout."""
    typed = []
    setup = setup_command(task, cfg, host)
    if setup:
        typed.append(("worktree setup", setup[0]))
    if task.get("mode", "agent") != "agent":
        wdir = worktree_dir(task, cfg, host)
        for i, c in enumerate(task.get("commands", []), 1):
            run = command_run(c["run"], task, cfg, host)
            run = in_dir(c.get("cwd", "<WORKTREE>").replace("<WORKTREE>", wdir, 1), run)
            typed.append((f"command {i}", run + ("" if c.get("long_running") else done_marker(i))))
    return [f"{what} is {len(t.encode())} bytes typed into the pane; keep it under {TYPED_MAX} "
            f"(macOS drops typed input past 1024 bytes): move it into a script" for what, t in typed
            if len(t.encode()) > TYPED_MAX]


def done_marker(n):
    """Numbered sentinel appended to a pane command; wait-output also sees old output."""
    return '; echo "__RT_DONE_%d__ rc=$?"' % n


def in_dir(cdir, run):
    """`run` in `cdir`, typed into a pane: `builtin cd`, so a user's cd alias or
    function (e.g. zoxide's) can't break it."""
    return "builtin cd {} && {}".format(shlex.quote(cdir), run)


def setup_command(task, cfg, host):
    """(shell command, timeout ms) that creates the task's worktree, or None."""
    repo_cfg = cfg["hosts"][host]["repos"][task["repository"]["name"]]
    wt, q = task["worktree"], shlex.quote
    repo = task["repository"]["name"]
    profile = worktree_profile(cfg, repo, host)
    if wt["strategy"] == "pnpm-worktree-issue":
        require_helper_profile(cfg, repo)
        values = task_template_values(task, cfg, host)
        create = render_template(profile["create"], values, shell=True, repo=repo, key="worktree.create")
        path = template_path(profile["dir"], values, repo, "worktree.dir")
        command = create + " && " + in_dir(path, "")
        if profile.get("check"):
            command += render_template(profile["check"], values, shell=True, repo=repo, key="worktree.check") + " && "
        # Helpers may reuse a branch rather than the requested base; always print HEAD.
        return command + "git rev-parse HEAD" + done_marker(0), 600000
    if wt["strategy"] == "git-worktree":
        return "git worktree add -b {b} {p} {c} && builtin cd {p} && git rev-parse HEAD{d}".format(
            b=q(wt["branch"]), p=q(worktree_dir(task, cfg, host)), c=task["project_commit"], d=done_marker(0)), 120000
    if wt["strategy"] == "git-detached":
        # Keep setup logic in scripts/detached-worktree.sh so typed input stays
        # within the buffer limit while a new tab's shell starts.
        h = cfg["hosts"][host]
        p = q(worktree_dir(task, cfg, host))
        args = [p, task["project_commit"]]
        if profile["kind"] == "helper" and package_manager(cfg, repo) == "pnpm":
            args += [q(os.path.normpath(repo_cfg["path"])), pm_command(cfg, repo, host)]
        return "{r}/scripts/detached-worktree.sh {a} && builtin cd {p} && git rev-parse HEAD{d}".format(
            r=h.get("routing_repo", "~/projects/agent-routing"), a=" ".join(args), p=p, d=done_marker(0)), 600000
    return None


def plan(task, cfg, host, orch, env=None):
    h = cfg["hosts"][host]
    repo_cfg = h["repos"][task["repository"]["name"]]
    wt = task["worktree"]
    name = task["worker"]["name"]
    timeout_ms = task["resources"]["timeout_minutes"] * 60 * 1000
    hc = lambda *a: herdr_cmd(cfg, host, orch, *a)  # noqa: E731
    q = shlex.quote
    # wait-output also matches output already in the pane, so every command
    # gets its own numbered sentinel.
    done = lambda n: '; echo "__RT_DONE_%d__ rc=$?"' % n  # noqa: E731
    done_wait = lambda pane, n, ms: hc("pane", "wait-output", pane, "--regex",  # noqa: E731
                                       "__RT_DONE_%d__ rc=[0-9]+" % n, "--timeout", str(ms))

    steps = [
        ("Discover live state on the target (IDs are per-server; never reuse)",
         [hc("agent", "list"), hc("workspace", "list")]),
        (f"Confirm no live agent is already named {name!r} and it has no open task", []),
        (f"Open the task's own tab in the issue's workspace ({herdr_group(task['task_id'])[0]}): if `workspace list` "
         f"shows a workspace with that label, `tab create` in it; else `workspace create` (then `tab rename` its first "
         f"tab). ROOT_PANE = .result.root_pane.pane_id. The env goes on the tab: tabs don't share it",
         [hc("tab", "create", "--workspace", "__ISSUE_WORKSPACE__", "--cwd", repo_cfg["path"],
             "--label", herdr_group(task["task_id"])[1],
             *[x for k, v in (env or {}).items() for x in ("--env", f"{k}={v}")], "--no-focus"),
          hc("workspace", "create", "--cwd", repo_cfg["path"], "--label", herdr_group(task["task_id"])[0],
             *[x for k, v in (env or {}).items() for x in ("--env", f"{k}={v}")], "--no-focus")]),
    ]
    setup = setup_command(task, cfg, host)
    if setup:
        steps.append(("Create/open the worktree (the project's helper where it has one). Continue only if "
                      "rc=0, any whereami checks are ok, and HEAD == project_commit",
                      [hc("pane", "run", "__ROOT_PANE__", setup[0]), done_wait("__ROOT_PANE__", 0, setup[1]),
                       hc("pane", "read", "__ROOT_PANE__", "--source", "recent-unwrapped", "--lines", "80")]))
    else:
        steps.append(("Read-only in the existing checkout: preflight must have shown HEAD == project_commit", []))

    if task.get("mode", "agent") == "agent":
        where, prompt = brief_prompt(cfg, host, orch, name, timeout_ms)
        steps += [
            ("Start the worker in that shell pane (shell must be at its prompt)",
             [hc("agent", "start", name, "--kind", task["worker"]["kind"], "--pane", "__ROOT_PANE__",
                 *native_args(task["worker"], cfg))]),
            (f"Put the brief (runs/tasks/{task['task_id']}/handoff.md from --record, plus the issue reference) at an "
             f"absolute path {where}; BRIEF_FILE must match [A-Za-z0-9._/-]+. Send exactly one prompt",
             [prompt]),
            ("Collect output, then verify independently (idle != success)",
             [hc("agent", "get", name),
              hc("agent", "read", name, "--source", "recent-unwrapped", "--lines", "200")]),
        ]
    else:
        cmds, n = [], 0
        wdir = worktree_dir(task, cfg, host)
        for i, c in enumerate(task["commands"], 1):
            run = command_run(c["run"], task, cfg, host)
            cdir = c.get("cwd", "<WORKTREE>").replace("<WORKTREE>", wdir, 1)
            run = in_dir(cdir, run)
            if c.get("long_running"):
                n += 1
                pane = f"__SERVER_PANE_{n}__"
                cmds += [hc("pane", "split", "__ROOT_PANE__", "--direction", "right",
                            "--cwd", wdir, "--no-focus"),
                         f"# {pane} = .result.pane.pane_id",
                         hc("pane", "run", pane, run),
                         hc("pane", "wait-output", pane, "--regex", c["ready_regex"], "--timeout", "600000")]
            else:
                cmds += [hc("pane", "run", "__ROOT_PANE__", run + done(i)),
                         done_wait("__ROOT_PANE__", i, timeout_ms)]
        steps += [
            ("Run each contract command, waiting for rc between them; long-running ones get their own pane",
             cmds),
            ("Collect output, then verify independently; stop long-running panes with ctrl+c when done",
             [hc("pane", "read", "__ROOT_PANE__", "--source", "recent-unwrapped", "--lines", "200")]),
        ]
    return steps


QUEUE_DIR = os.path.join(ROOT, "runs", "queue")


def queue_files(host):
    """A host's queue, oldest first (filenames start with a nanosecond timestamp)."""
    return sorted(glob.glob(os.path.join(QUEUE_DIR, host, "*.json")))


def queue_id(path):
    return os.path.basename(path).split("-", 1)[1].removesuffix(".json").lower()


def enqueue(task, d, cfg):
    """Park a task in its host's queue (heavy over the cap, or any task on a hot host). Refuse when full."""
    import time
    host = d["selected_host"]
    qdir = os.path.join(QUEUE_DIR, host)
    os.makedirs(qdir, exist_ok=True)
    files = queue_files(host)
    if any(queue_id(f) == task["task_id"].lower() for f in files):
        return None, f"{task['task_id']} is already queued on {host}"
    limit = cfg["hosts"][host].get("max_queue", 10)
    if len(files) >= limit:
        return None, f"{host} queue is full ({len(files)}/{limit}): backpressure, submit later"
    t = dict(task, selected_host=host, routing_ref=d["routing_ref"],
             queued_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    path = os.path.join(qdir, f"{time.time_ns()}-{task['task_id']}.json")
    with open(path, "x") as f:
        f.write(json.dumps(t, indent=2) + "\n")
    return len(files) + 1, None


GRANT_ROLES = {"draft-pr": "impl", "local-only": "impl", "verify-only": "verify", "gate": "gate", "review": "review",
               "staging-review": "staging"}


def build_task(a, cfg):
    """Build a task from --issue flags and a named grant in the config, so
    routine issues need no hand-written JSON. Nothing here is workflow."""
    validate_config(cfg)
    grants = cfg.get("grants", {})
    if not a.grant or a.grant not in grants:
        raise Invalid(f"unknown --grant {a.grant!r}; config grants: {sorted(grants)}")
    g = grants[a.grant]
    if not a.repo and not cfg.get("default_repo"):
        raise Invalid("config default_repo is required when --repo is omitted")
    issue = a.issue.upper()
    repo = a.repo
    if not repo and not issue.isdigit():
        matches = [name for name in cfg.get("projects", {})
                   if project_profile(cfg, name).get("issue_prefix") == issue.rsplit("-", 1)[0]]
        if len(matches) > 1:
            raise Invalid(f"issue prefix matches multiple projects {matches}; use --repo")
        repo = matches[0] if matches else None
    repo = repo or cfg.get("default_repo")
    if not repo:
        raise Invalid("config default_repo is required when --repo or an issue prefix does not select a project")
    if issue.isdigit():
        prefix = project_profile(cfg, repo).get("issue_prefix")
        if not prefix:
            raise Invalid(f"numeric --issue needs project {repo!r}'s issue_prefix")
        issue = f"{prefix}-{issue}"
    if not issue_pattern(cfg, repo).fullmatch(issue):
        raise Invalid(f"issue {issue!r} does not match project {repo!r}'s issue prefix")
    remote = next((h["repos"][repo]["remote"] for h in cfg["hosts"].values() if repo in h.get("repos", {})), None)
    if not remote:
        raise Invalid(f"repo {repo!r} is not configured on any host")
    gate_runtime = project_profile(cfg, repo).get("gate_runtime", "none")
    runtime = "compose" if a.compose else gate_runtime if a.grant == "gate" else "none"
    # Unique, readable Herdr name: <issue>[-<suffix>]-<role>, e.g. app123-impl,
    # app123-native-verify. Views strip the issue prefix under its task.
    role = a.role or GRANT_ROLES.get(a.grant, "work")
    slug = "-".join([issue.lower().replace("-", "")] + ([a.suffix] if a.suffix else []) + [role])[:32]
    task = {
        "contract_version": 1,
        "task_id": slug,
        "summary": a.summary or f"{issue}{' (' + a.suffix + ')' if a.suffix else ''}",
        "authorization": {"scope": g["scope"], "granted_by": a.granted_by or cfg.get("operator", ""),
                          "allowed": g.get("allowed", []), "forbidden": g.get("forbidden", [])},
        "routing": {"requested_host": a.host,     # plus the grant's own placement needs (staging-review: staging-login)
                    "requires": list(dict.fromkeys([t for t in a.requires.split(",") if t] + g.get("requires", []))),
                    **({"allow_behind": True} if getattr(a, "allow_behind", False) else {})},
        "repository": {"name": repo, "remote": remote},
        "project_commit": a.commit if SHA_RE.fullmatch(a.commit) else "0" * 40,
        "worktree": staging_worktree(cfg, repo, issue) if a.grant == "staging-review" else issue_worktree(cfg, repo, issue, a),
        "worker": {"kind": a.kind, "name": slug, "model": a.model, "effort": a.effort},
        # A project's gate runtime determines whether it needs a port slot.
        "resources": {"timeout_minutes": a.timeout, "runtime": runtime,
                      **({"class": "heavy"} if (a.heavy or a.compose or a.grant == "gate") else
                         {"class": "light"} if a.light else {}),
                      "ports": [int(p) for p in a.ports.split(",") if p]},
        "stop_condition": a.stop or g.get("stop_condition", ""),
    }
    if not SHA_RE.fullmatch(a.commit):
        task["_commit_ref"] = a.commit
    if a.brief_file:
        with open(os.path.expanduser(a.brief_file)) as f:
            task["instructions"] = f.read()
    return task


def staging_worktree(cfg, repo, issue):
    """A staging review only runs the project's session helper against deployed
    staging, so it gets its own detached worktree at project_commit (origin/main by
    default) instead of the issue's, whose feature head may predate the helper."""
    profile = project_profile(cfg, repo)
    wt = worktree_profile(cfg, repo)
    template = profile.get("staging", {}).get("worktree_dir")
    if template is None:
        template = os.path.join(os.path.dirname(wt["dir"]), "{issue}-staging")
    path = template_path(template, template_values(cfg, repo, issue), repo, "staging.worktree_dir")
    validate_template_location(cfg, repo, path, "staging.worktree_dir")
    return {"strategy": "git-detached", "path": path}


def issue_worktree(cfg, repo, issue, a):
    """Built tasks retain the contract's historical helper strategy name."""
    profile = worktree_profile(cfg, repo)
    if profile["kind"] == "helper":
        return {"strategy": "pnpm-worktree-issue", "issue": issue, "deps": a.deps, "env": "link"}
    if profile["kind"] != "git":
        raise Invalid(f"unknown worktree kind {profile['kind']!r} for project {repo!r}")
    name = issue + (f"-{a.suffix}" if a.suffix else "")
    values = template_values(cfg, repo, name, a.commit, a.deps)
    path = template_path(profile["dir"], values, repo, "worktree.dir")
    validate_template_location(cfg, repo, path, "worktree.dir")
    return {"strategy": "git-worktree", "path": path,
            "branch": render_template(profile["branch"], values, repo=repo, key="worktree.branch")}


def resolve_ref(cfg, host, orch, task):
    """Resolve a ref like origin/main to a SHA on the target, read-only, no fetch."""
    ref = task.pop("_commit_ref")
    if not BRANCH_RE.fullmatch(ref):
        return f"bad --commit ref {ref!r}"
    repo = cfg["hosts"][host]["repos"][task["repository"]["name"]]["path"]
    cmd = ["git", "--no-optional-locks", "-C", repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"]
    reach = reach_for(cfg, host, orch)
    if reach["mode"] != "local":
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach["ssh_alias"],
               " ".join(shlex.quote(x) for x in cmd)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=clean_git_env())
    except subprocess.TimeoutExpired:
        return f"timed out resolving {ref} on {host}"
    sha = p.stdout.strip()
    if p.returncode != 0 or not SHA_RE.fullmatch(sha):
        return f"cannot resolve {ref} on {host} (fetch first?)"
    task["project_commit"] = sha
    return None


def decide(a, cfg, cfg_path, task):
    orch = a.orch or detect_orchestrator(cfg)
    placement = None
    if a.active_dir and not task["routing"].get("requested_host"):
        req = set(task["routing"].get("requires", []))
        capable = [h for h, hc in cfg["hosts"].items() if req <= set(hc.get("capabilities", []))]
        if len(capable) > 1:
            _, act = load_active(task, cfg, a.active_dir)
            placement = {}
            for h in capable:
                lv = load_levels(host_load(cfg, h, orch), cfg["hosts"][h].get("hot_limits"))
                full = False
                if is_heavy(task):   # a heavy task prefers a free heavy slot over queueing on a full host
                    cap_h = cfg["hosts"][h].get("max_heavy")
                    used = sum(1 for _, o, oh in act if oh == h and is_heavy(o))
                    full = (cap_h is not None and used >= cap_h) or bool(queue_files(h))
                placement[h] = {"hot": bool(lv.get("hot")), "warn": lv.get("overall") == "warn", "full": full,
                                "open": sum(1 for _, _, oh in act if oh == h), "levels": lv}
    host, rule, blockers = choose_host(task, cfg, placement)
    if host and task.get("_commit_ref"):
        ref = task["_commit_ref"]
        err = resolve_ref(cfg, host, orch, task)
        if err:
            blockers = list(blockers) + [err]
        else:
            blockers = list(blockers)
            rule += f"; {ref} resolved on {host} as of its last fetch"
            task["_resolved_ref"] = ref
    d = {
        "task_id": task["task_id"],
        "routing_ref": routing_ref(),
        "config": os.path.relpath(cfg_path, ROOT),
        "orchestrator_host": orch,
        "selected_host": host,
        "rule": rule,
        "execution_authorized": False,
        "authorization_scope": task["authorization"]["scope"],
        "repository": task["repository"]["name"],
        "project_commit": task["project_commit"],
        "blockers": list(blockers) + policy_blockers(task, cfg),
        "warnings": [],
        "queue_reasons": [],
        "heavy": is_heavy(task),
    }
    if not host:
        return d
    d["blockers"] += typed_blockers(task, cfg, host)
    h = cfg["hosts"][host]
    if orch not in h.get("reach", {}):
        d["blockers"].append(f"config has no reach entry for {host} from {orch}")
        return d
    if task["repository"]["name"] not in h["repos"]:
        d["blockers"].append(f"repo {task['repository']['name']} not configured on {host}")
        return d
    if h.get("checkouts_may_be_stale"):
        d["warnings"].append("target checkout may be stale: fetch is allowed, but never "
                             "pull/reset/clean/checkout/switch/restore/stash it; work from project_commit in a worktree")
    active = []
    if a.active_dir:
        problems, active = load_active(task, cfg, a.active_dir)
        d["blockers"] += problems + collisions(task, host, cfg, active)
        d["warnings"] += stale_entries(active)
        # Only heavy tasks are capped: light workers mostly wait on the model.
        # Over the cap, a heavy task waits in the host's FIFO queue (backpressure).
        heavy_cap = h.get("max_heavy")
        heavy = sum(1 for _, o, oh in active if oh == host and is_heavy(o))
        if d["heavy"] and heavy_cap is not None and heavy >= heavy_cap:
            d["queue_reasons"].append(f"{host} has {heavy} heavy tasks open (max_heavy {heavy_cap})")
        # Load-aware backpressure: a hot host takes no new heavy work; it queues instead.
        # Light (model-bound) work still starts: its own heavy phases (typechecks, web
        # builds, smokes, Docker) wait for a heavy slot through scripts/heavy.
        lv = (placement or {}).get(host, {}).get("levels")
        if lv is None:
            d["host_load"] = host_load(cfg, host, orch)
            lv = load_levels(d["host_load"], h.get("hot_limits"))
        d["host_levels"] = lv
        if lv.get("hot") and d["heavy"]:
            d["queue_reasons"].append(f"{host} is hot: {'; '.join(lv['hot'])}")
        elif lv.get("hot"):
            d["warnings"].append(f"{host} is hot ({'; '.join(lv['hot'])}): light work starts anyway; "
                                 "its heavy phases wait for a slot")
        waiting = [q for q in queue_files(host) if queue_id(q) != task["task_id"].lower()]
        if d["heavy"] and waiting and not getattr(a, "draining", False):
            d["queue_reasons"].append(f"{len(waiting)} heavy task(s) already queued on {host} (first in, first out)")
    else:
        d["warnings"].append("no --active-dir: heavy-task cap, port-slot and worktree collisions were NOT checked")
    if task["resources"].get("runtime") == "compose":
        slot, err = pick_slot(task, cfg, host, active)
        if err and err.startswith("no free port slot"):
            d["queue_reasons"].append(err)
        elif err:
            d["blockers"].append(err)
        else:
            d["port_slot"], d["env"] = slot, runtime_env(task, cfg, host, slot)
    elif task["resources"].get("port_slot") is not None:
        d["warnings"].append("resources.port_slot is ignored unless runtime is compose")
    if d["blockers"] or d["queue_reasons"]:
        return d

    argv, stdin_path = preflight_invocation(cfg, host, orch, task, d.get("env"))
    d["preflight_command"] = " ".join(shlex.quote(x) for x in argv) + (
        f" < {os.path.relpath(stdin_path, ROOT)}" if stdin_path else "")
    if a.run_preflight:
        result = run_preflight(argv, stdin_path)
        d["preflight"] = result
        behind = str(result.get("commit_behind_ref", "")).split()
        if task["routing"].get("allow_behind") and task_step(task["task_id"]) != "gate":
            d["warnings"].append("allow_behind only applies to gates; ignored for this task")
        if task["routing"].get("allow_behind") and len(behind) == 2 and behind[1].isdigit() and int(behind[1]) > 0:
            d["warnings"].append(f"{task['project_commit'][:12]} is {behind[1]} commits behind {behind[0]}; starting "
                                 f"anyway (allow_behind): the gate's brief must stop on a merge conflict with main")
        pin = result.get("repo_pnpm_pin")   # read from the repo's own packageManager field
        if (package_manager(cfg, task["repository"]["name"]) == "pnpm"
                and pin and result.get("pnpm_version") and result["pnpm_version"] != pin):
            d["warnings"].append(
                f"bare pnpm is {result['pnpm_version']}, pin is {pin}: worker brief must use "
                f"the host's pnpm_cmd (npx -y pnpm@{pin} ...)")
        if task.get("_resolved_ref") and result.get("repo_last_fetch", "no-fetch-recorded") == "no-fetch-recorded":
            d["warnings"].append(f"{task['_resolved_ref']} was resolved on {host}, which has no fetch on record: "
                                 "it may be stale. Fetch there and route again")
        dirty = [result.get(k, "0") for k in ("repo_dirty_tracked", "repo_untracked")]
        if task["worktree"]["strategy"] == "existing-checkout" and any(x not in ("0", "") for x in dirty):
            d["warnings"].append(f"existing checkout has local changes ({dirty[0]} tracked, {dirty[1]} untracked): "
                                 "results reflect them, not just project_commit")
        if result.get("preflight_status") != "ok":
            d["blockers"].append("preflight: " + result.get("blockers", result.get("preflight_status", "?")))
    else:
        d["warnings"].append("preflight not run (pass --run-preflight); plan is unverified")
    d["plan"] = [{"step": s, "commands": c} for s, c in plan(task, cfg, host, orch, d.get("env"))]
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", nargs="?", help="task contract JSON (or build one with --issue)")
    b = ap.add_argument_group("build a task instead of reading JSON")
    b.add_argument("--issue", help="issue key or number using the default project prefix, e.g. APP-123 or 123")
    b.add_argument("--requires", default="", help="comma-separated capability tags, e.g. linux,docker")
    b.add_argument("--grant", default=None, help="named grant from the config (required with --issue)")
    b.add_argument("--commit", default="origin/main", help="exact SHA, or a ref resolved on the target (default origin/main)")
    b.add_argument("--host", default=None, help="explicit host request")
    b.add_argument("--suffix", default=None, help="task suffix when one issue has several tasks (e.g. web, ios)")
    b.add_argument("--compose", action="store_true", help="needs the project's Compose stack (port slot)")
    b.add_argument("--heavy", action="store_true",
                   help="full suite, readiness gate or other CPU/memory-heavy run (counts against max_heavy)")
    b.add_argument("--light", action="store_true",
                   help="mark docker work light (e.g. focused tests only); docker work is heavy by default")
    b.add_argument("--allow-behind", action="store_true",
                   help="a gate may start behind origin/main (warned, not blocked); its brief must stop on a "
                        "merge conflict with main")
    b.add_argument("--ports", default="", help="other reserved host ports, e.g. 8081")
    b.add_argument("--deps", default="link", choices=sorted(DEPS_MODES))
    b.add_argument("--kind", default="codex")
    b.add_argument("--model", default=None)
    b.add_argument("--effort", default=None)
    b.add_argument("--repo", default=None)
    b.add_argument("--timeout", type=int, default=120, help="minutes")
    b.add_argument("--stop", default=None, help="stop point (default: the grant's)")
    b.add_argument("--summary", default=None, help="issue title, shown on the task row (pass the Linear title)")
    b.add_argument("--role", default=None, help="worker role in its name (default from the grant: impl, verify, gate, review)")
    b.add_argument("--granted-by", default=None)
    b.add_argument("--print-task", action="store_true", help="print the built task JSON and exit")
    b.add_argument("--brief-file", default=None,
                   help="extra task instructions (e.g. a filled ~/orchestrator template) appended to the worker's brief")
    ap.add_argument("--config", default=None,
                    help="hosts config (default: config/hosts.local.json, else the example)")
    ap.add_argument("--from", dest="orch", default=None,
                    help="host the orchestrator runs on (default: detect)")
    ap.add_argument("--run-preflight", action="store_true",
                    help="also run the read-only preflight on the chosen host")
    ap.add_argument("--active-dir", default=None,
                    help="directory of open task contracts to check for collisions")
    ap.add_argument("--json", action="store_true", help="print the decision as JSON only")
    q = ap.add_argument_group("open tasks and the heavy queue (no daemon: the orchestrator drives these)")
    q.add_argument("--close", metavar="TASK_ID", help="close an open task (frees its heavy and port slot)")
    q.add_argument("--next", action="store_true",
                   help="with --dispatch: start the oldest queued heavy task that now fits (per host); "
                        "without it, only show what would start")
    q.add_argument("--queue", action="store_true", help="show open tasks and queues per host")
    q.add_argument("--dequeue", metavar="TASK_ID", help="drop a task from the queue")
    q.add_argument("--outcome", default=None, help="with --close: how the step ended (passed, failed, approved, changes_required, done, abandoned); "
                        "required for gate and review tasks")
    q.add_argument("--mark", nargs=3, metavar=("ISSUE", "STEP", "STATE"),
                   help="record a pipeline step routing can't see, e.g. --mark APP-1 review approved "
                        "(steps come from the issue's project pipeline and tracker)")
    q.add_argument("--note", default=None, help="with --mark: a short note (PR number, reason)")
    q.add_argument("--sha", default=None, metavar="COMMIT",
                   help="with --mark: the commit the result is for (review/gate), so the views can tell "
                        "when the PR head has moved past it")
    q.add_argument("--status", action="store_true",
                   help="open tasks with live worker state and children on both hosts, plus untracked agents")
    q.add_argument("--dashboard", nargs="?", const=os.path.join(ROOT, "runs", "dashboard.html"), metavar="PATH",
                   help="write the routing map (HTML) to PATH (default runs/dashboard.html)")
    q.add_argument("--snapshot", action="store_true",
                   help="print the data behind the views as JSON: {report, queues, capacity} (agents, trees' raw data, load)")
    q.add_argument("--watch", action="store_true",
                   help="live, animated routing view in the terminal (Ctrl+C to quit); data refresh: --every (default 5s)")
    q.add_argument("--feedback", metavar="TEXT",
                   help="report routing friction for the routing maintainer (one line: what happened, what you "
                        "had to work around); add --task for context. Returns at once")
    q.add_argument("--task", metavar="TASK_ID", help="with --feedback: the task it happened on")
    q.add_argument("--feedback-wait", action="store_true",
                   help="(maintainer) wait for new feedback, print it and exit; --events-timeout applies")
    q.add_argument("--feedback-done", metavar="ID", help="(maintainer) mark a feedback item handled; --note says how")
    q.add_argument("--events", action="store_true",
                   help="wait for the next things that need the orchestrator (a worker blocked or stopped, a stale or "
                        "failed step, CI, merges, a hot machine, a queue to drain), print them and exit; run it in the "
                        "background and start it again after handling. Read-only. --every (default 15s), --follow")
    q.add_argument("--follow", action="store_true", help="with --events: stream one line per event and never exit on its own (no timeout; "
                        "failed snapshots back off and retry). Preferred under a line watcher")
    q.add_argument("--events-timeout", type=int, default=45, metavar="MINUTES",
                   help="with --events: exit 3 after this long with nothing to report (default 45; 0 = never)")
    q.add_argument("--expand", action="store_true",
                   help="with --watch: full pipeline rows for merged issues and list every completed one")
    q.add_argument("--every", type=int, default=0, metavar="SECONDS",
                   help="with --dashboard: keep rewriting it every N seconds (the page reloads itself)")
    q.add_argument("--listen", metavar="TASK_ID", help="read a dispatched task's worker output (via its link)")
    q.add_argument("--handoff", action="store_true",
                   help="with --listen: only the worker's last reply (from its session file) and key facts "
                        "(PRs, SHAs, tests, questions): a few hundred tokens instead of raw terminal output")
    q.add_argument("--blocked", action="store_true",
                   help="workers waiting on a decision (approval/question), with the dialog text: bubble-up for the orchestrator")
    q.add_argument("--answer", nargs=2, metavar=("TASK_ID", "KEY"),
                   help="answer a blocked worker's dialog (keys: y n esc enter 1 2 3); logged in its link")
    q.add_argument("--question", metavar="TASK_ID",
                   help="open a blocked worker's question (Codex asks while it keeps running) and print it")
    q.add_argument("--reap", action="store_true",
                   help="list leftover idle/done agents in issue workspaces whose issue is finished or has had no "
                        "open task for 24 h; with --apply, close their tabs (worktrees stay; logged in runs/reaped.jsonl)")
    q.add_argument("--apply", action="store_true", help="with --reap: close the listed agents' tabs")
    q.add_argument("--steer", nargs=2, metavar=("TASK_ID", "TEXT"),
                   help="send one instruction to a working worker (narrow scope, set a stop point; within the grant); logged in its link")
    q.add_argument("--reply", nargs=2, metavar=("TASK_ID", "TEXT"),
                   help="type a one-line answer into a worker's opened question (after --question); logged in its link")
    q.add_argument("--wait-ms", type=int, default=0, help="with --listen: first wait for the worker to settle")
    q.add_argument("--lines", type=int, default=120, help="with --listen: lines of output to read")
    ap.add_argument("--dispatch", action="store_true",
                    help="after routing, START the task through Herdr (workspace, worktree, worker, brief) and "
                         "write its link. Implies --record and --run-preflight. Executes; review grants first.")
    ap.add_argument("--record", action="store_true",
                    help="when routed, write runs/tasks/<task_id>/ (task, decision, handoff.md) and "
                         "<active-dir>/<task_id>.json; --active-dir defaults to runs/active")
    a = ap.parse_args()
    if a.dispatch:
        a.record = a.run_preflight = True
    local_cfg = os.path.join(ROOT, "config", "hosts.local.json")
    try:
        home_cfg = load_config(a.config or (local_cfg if os.path.exists(local_cfg) else os.path.join(ROOT, "config", "hosts.example.json")))
        for flag, value in (("--host", a.host), ("--from", a.orch)):
            if value is not None:
                try:
                    H.configured_host(home_cfg, value, flag)
                except ValueError as e:
                    raise Invalid(str(e)) from e
        msg = check_home(home_cfg, a.orch or detect_orchestrator(home_cfg), a)
    except (OSError, ValueError, Invalid) as e:
        if isinstance(e, Invalid):
            print(f"error: Invalid: {e}", file=sys.stderr)
            return 2
        msg = None
    if msg:
        print(msg, file=sys.stderr)
        return 2
    if a.close or a.next or a.queue or a.dequeue or a.status or a.listen or a.blocked or a.answer or a.question or a.reply or a.steer or a.reap or a.dashboard or a.watch or a.snapshot or a.mark or a.events or a.feedback or a.feedback_wait or a.feedback_done:
        return queue_ops(a)
    if a.record:
        a.active_dir = a.active_dir or os.path.join(ROOT, "runs", "active")
        os.makedirs(a.active_dir, exist_ok=True)

    if a.active_dir and not os.path.isdir(a.active_dir):
        print(f"error: --active-dir {a.active_dir} is not a directory", file=sys.stderr)
        return 2
    local_cfg = os.path.join(ROOT, "config", "hosts.local.json")
    cfg_path = a.config or (local_cfg if os.path.exists(local_cfg)
                            else os.path.join(ROOT, "config", "hosts.example.json"))
    if bool(a.task) == bool(a.issue):
        print("error: give either a task file or --issue", file=sys.stderr)
        return 2
    try:
        cfg = load_config(cfg_path)
        task = build_task(a, cfg) if a.issue else load_json(a.task)
        errors = validate(task, cfg)
    except (OSError, ValueError, RecursionError, Invalid, KeyError, TypeError) as e:
        print(f"error: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 2
    if a.print_task:
        print(json.dumps(task, indent=2))
        return 2 if errors else 0
    if errors:
        print(json.dumps({"task": a.task or a.issue, "status": "invalid", "errors": errors}, indent=2))
        return 2
    try:
        d = decide(a, cfg, cfg_path, task)
    except (Invalid, KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
        print(f"error: invalid config or task: {e!r}", file=sys.stderr)
        return 2

    d["status"] = "blocked" if d["blockers"] else "queued" if d["queue_reasons"] else "routed"
    if d["status"] == "queued" and a.record:
        pos, err = enqueue(task, d, cfg)
        if err:
            d["blockers"].append(err)
            d["status"] = "blocked"
        else:
            d["queue_position"] = pos
    try:
        if d["status"] == "routed":
            d["handoff_note"] = handoff_note(task, d, cfg, d["selected_host"])
            if a.record:
                d["recorded"] = record(task, d, d["handoff_note"], a.active_dir, cfg)
            if a.dispatch:
                return run_dispatch(a, task, d, cfg)
    except FileExistsError:
        print(f"error: {task['task_id']} is already recorded as open in {a.active_dir}", file=sys.stderr)
        return 3
    except (Invalid, KeyError, TypeError, OSError) as e:
        print(f"error: {e!r}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(d, indent=2))
    else:
        print_human(d)
    return 3 if d["blockers"] else 4 if d["status"] == "queued" else 0


def run_dispatch(a, task, d, cfg):
    """Start a routed, recorded task and print its link. Exit 0 started, 3 failed."""
    import dispatch as D
    run_dir = os.path.join(ROOT, d["recorded"])
    marker = os.path.join(a.active_dir, task["task_id"] + ".json")
    orch = d["orchestrator_host"]
    try:
        link = D.dispatch(task, d, cfg, orch, run_dir, marker)
    except (D.DispatchError, Invalid) as e:
        print(f"DISPATCH FAILED: {e}", file=sys.stderr)
        print(f"link (partial): {d['recorded']}/link.json  — the task stays open; inspect, then --close it",
              file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(link, indent=2))
    else:
        print(f"dispatched:    {task['task_id']} on {link['host']}  {link.get('group')} > {link.get('tab_label')} "
              f"({link['workspace_id']}/{link.get('tab_id')})  "
              f"pane {link['root_pane']}  agent {link.get('agent') or '-'}")
        print(f"worktree:      {link['worktree']}  branch {link['branch']}  HEAD {link.get('head_before') or '-'}")
        print(f"listen:        route.py --listen {task['task_id']} [--wait-ms 600000]   status: route.py --status")
        print(f"herdr:         {link['herdr']}")
        for k, v in link["listen"].items():
            print(f"  {k:9} $ {v}")
    return 0


HOT_DEFAULTS = {"load_per_core": 1.0, "min_mem_avail_pct": 10, "max_docker_pct": 90}
# Remote readers prefer a supported user-installed Python before the system interpreter.
# Non-interactive SSH shells may omit installation directories from PATH.
REMOTE_PY = ('P=; for c in "$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; '
             'do command -v "$c" >/dev/null 2>&1 && P=$c && break; done; "$P" -')


def host_load(cfg, host, orch):
    """Live load of `host` (scripts/host_load.py run locally or piped over SSH)."""
    try:
        reach = reach_for(cfg, host, orch)
        if reach["mode"] == "local":
            import host_load as HL
            return HL.snapshot()
        with open(os.path.join(ROOT, "scripts", "host_load.py"), "rb") as f:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach["ssh_alias"], REMOTE_PY],
                               stdin=f, capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, KeyError, Invalid, subprocess.TimeoutExpired):
        return None


def load_levels(ld, limits=None):
    """Grade a load snapshot: {"cpu"|"mem"|"swap"|"docker": ok/warn/hot, "overall": ..., "hot": [reasons]}."""
    lim = dict(HOT_DEFAULTS, **(limits or {}))
    lv, hot = {}, []
    if not ld:
        return {"overall": "unknown", "hot": []}
    if ld.get("load1") is not None and ld.get("cores"):
        r = ld["load1"] / ld["cores"]
        lv["cpu"] = "hot" if r >= lim["load_per_core"] else "warn" if r >= 0.7 * lim["load_per_core"] else "ok"
        if lv["cpu"] == "hot":
            hot.append(f"load {ld['load1']:.1f} on {ld['cores']} cores")
    if ld.get("mem_total") and ld.get("mem_avail") is not None:
        pct = 100 * ld["mem_avail"] / ld["mem_total"]
        lv["mem"] = "hot" if pct < lim["min_mem_avail_pct"] else "warn" if pct < 2 * lim["min_mem_avail_pct"] else "ok"
        if lv["mem"] == "hot":
            hot.append(f"only {pct:.0f}% memory available")
    if ld.get("pressure") in ("warn", "critical"):     # the kernel's memory pressure level
        p = "hot" if ld["pressure"] == "critical" else "warn"
        if p == "hot" and lv.get("mem") != "hot":
            hot.append("memory pressure critical")
        lv["mem"] = max(lv.get("mem", "ok"), p, key={"ok": 0, "warn": 1, "hot": 2}.get)
    if ld.get("swap_total"):
        lv["swap"] = "warn" if ld.get("swap_used", 0) / ld["swap_total"] >= 0.75 else "ok"   # never hot on its own
    if ld.get("docker") == "up" and ld.get("docker_limit"):
        dp = 100 * ld.get("docker_used", 0) / ld["docker_limit"]
        lv["docker"] = "hot" if dp >= lim["max_docker_pct"] else "warn" if dp >= 0.83 * lim["max_docker_pct"] else "ok"
        if lv["docker"] == "hot":
            hot.append(f"Docker at {dp:.0f}% of its memory limit")
    order = {"ok": 0, "warn": 1, "hot": 2}
    lv["overall"] = max(lv.values(), key=lambda x: order[x]) if lv else "unknown"
    lv["hot"] = hot
    return lv


def collector_exe():
    """The Go collector (agent-collect), unless AGENT_COLLECT=0 or it isn't installed."""
    import shutil
    if os.environ.get("AGENT_COLLECT") == "0":
        return None
    local = os.path.expanduser("~/.local/bin/agent-collect")
    return shutil.which("agent-collect") or (local if os.access(local, os.X_OK) else None)


def check_home(cfg, orch, a):
    """Commands that change the open-task list or the queue run only on the
    routing home, so heavy caps and first-in-first-out stay correct."""
    home = cfg.get("routing_home")
    changes = a.record or a.dispatch or a.close or a.next or a.dequeue or a.answer or a.question or a.reply or a.steer or (a.reap and a.apply) or a.mark or a.feedback or a.feedback_done
    if not home or not changes or orch == home:
        return None
    alias = (cfg["hosts"].get(home, {}).get("reach", {}).get(orch) or {}).get("ssh_alias", home)
    argv, skip = [], False
    for x in sys.argv[1:]:                      # drop --from: on the home it's detected
        if skip:
            skip = False
        elif x == "--from":
            skip = True
        elif not x.startswith("--from="):
            argv.append(x)
    return (f"error: the routing home is {home}: the open-task list and queue live there. Run this on {home}, e.g.\n"
            f"  ssh {alias} " + shlex.quote("cd ~/projects/agent-routing && python3 scripts/route.py " + " ".join(shlex.quote(x) for x in argv)) + "\n"
            f"Read-only commands (--status, --watch, a dry run) work from any machine.")


def gather(cfg, orch, active_dir):
    """Live snapshot for the views: (report, queues, capacity). Away from the
    routing home it's taken ON the home, where the records live; otherwise
    built by the Go collector when installed, else by the Python readers."""
    home = cfg.get("routing_home")
    if home and orch != home and os.environ.get("ROUTE_SNAPSHOT_LOCAL") != "1":
        reach = cfg["hosts"].get(home, {}).get("reach", {}).get(orch) or {}
        repo = cfg["hosts"].get(home, {}).get("routing_repo", "~/projects/agent-routing")
        cmd = (f"cd {repo} && P=; for c in \"$HOME/.local/bin/python3\" /opt/homebrew/bin/python3 python3; do "
               f"command -v \"$c\" >/dev/null 2>&1 && P=$c && break; done; \"$P\" scripts/route.py --snapshot")
        try:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", reach.get("ssh_alias", home), cmd],
                               capture_output=True, text=True, timeout=90)
            if p.returncode == 0:
                d = json.loads(p.stdout)
                return d["report"], d["queues"], d["capacity"]
            note = (p.stderr or p.stdout).strip()[-160:]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            note = str(e)
        report, queues, cap = gather_local(cfg, orch, active_dir)
        report.setdefault("unreachable", []).append(
            f"routing home {home} unreachable ({note}); showing this machine's records, which may be empty")
        return report, queues, cap
    return gather_local(cfg, orch, active_dir)


CLOSED_DIR = os.path.join(ROOT, "runs", "closed")
ISSUES_DIR = os.path.join(ROOT, "runs", "issues")
DEFAULT_PIPELINE = ["impl", "review", "gate", "merge"]
ROLE_STEP = {"impl": "impl", "verify": "impl", "work": "impl"}
RECENT_S = 14 * 24 * 3600          # closed steps and marks stay visible this long


def issue_project(cfg, key):
    """Resolve view rows by prefix, falling back to the configured default."""
    prefix = (key or "").upper().split("-", 1)[0]
    matches = [name for name in cfg.get("projects", {})
               if project_profile(cfg, name).get("issue_prefix") == prefix]
    if len(matches) > 1:
        raise Invalid(f"config: ambiguous issue prefix {prefix!r} in projects {matches}")
    return matches[0] if matches else cfg.get("default_repo")


def issue_records(active_dir=None, queue_hosts=()):
    """Read persisted issue identity, including old records and queued tasks."""
    def records(paths):
        out = []
        for path in paths:
            try:
                out.append(load_json(path))
            except (OSError, ValueError):
                continue
        return out

    closed = records(glob.glob(os.path.join(CLOSED_DIR, "*.json")))
    current = records(glob.glob(os.path.join(active_dir or os.path.join(ROOT, "runs", "active"), "*.json")))
    current += records(path for host in queue_hosts for path in queue_files(host))
    return closed, current


def recorded_issue_projects(closed, current):
    """Current tasks override the newest closed record; prefixes are a fallback."""
    projects = {}
    for t in sorted(closed, key=lambda t: t.get("closed_at", 0)) + current:
        repository = t.get("repository") or {}
        project = repository.get("name") if isinstance(repository, dict) else repository
        key = issue_key(t.get("task_id"))
        if key and project:
            projects[key] = project
    return projects


def view_profile(cfg, key, project=None):
    project = project or issue_project(cfg, key)
    profile = project_profile(cfg, project)
    return {"project": project, "pipeline": list(profile.get("pipeline", DEFAULT_PIPELINE)),
            "tracker": profile.get("tracker", "none"), "tracker_flow": profile.get("tracker_flow", {})}


def row_pipeline(r):
    return r.get("pipeline", DEFAULT_PIPELINE)


def outcome_steps(r):
    # Custom verification steps need verdicts too. Lifecycle steps are exempt
    # from fix-round invalidation, verdict requirements and SHA comparison.
    return set(row_pipeline(r)) - {"impl", "merge", "deploy"}


def issue_data_degraded(report, row):
    """Missing project evidence cannot authorize completion or retirement."""
    if any(str(u).startswith(("routing home", "issue view failed")) for u in report.get("unreachable", [])):
        return True
    key = (row or {}).get("issue", "")
    project = ((row or {}).get("project") or report.get("issue_projects", {}).get(key) or
               report.get("project_prefixes", {}).get(key.split("-", 1)[0], report.get("default_project")))
    bad = set(report.get("degraded", []))
    return bool(bad.intersection({"github", "linear", f"github:{project}", f"linear:{project}"}))


def tracker_kind(r):
    # Older snapshots may predate project metadata.
    return r.get("tracker", "linear" if "linear" in r.get("steps", {}) else "none")


def tracker_label(r):
    return tracker_kind(r).capitalize()


# Step classes, shared by every view (cls -> meaning):
#   done ✓ · fail ✗ · need: waiting on a decision (blocked dialog) · wait: worker stopped, read its output
#   working: live worker running · active: recorded as in progress · queued: waiting for a heavy slot
#   stale: result is for an older PR head · unknown: no result recorded, or the host is unreachable
#   skip: n/a · pending: not started
DONE_STATES = {"done", "passed", "approved", "merged", "deployed", "verified", "pass", "green", "ok"}
FAIL_STATES = {"failed", "fail", "changes", "changes_required", "rejected", "red", "abandoned"}
NEED_STATES = {"blocked", "needs-decision", "waiting-on-human"}
SKIP_STATES = {"n/a", "na", "skipped", "none"}
# A step recorded as in progress that hasn't changed for this long gets flagged (seconds).
# One spelling per result, whatever word or case was recorded: "APPROVE", "approve" and
# "approved" all mean approved. --mark stores the canonical word; classify reads any.
STATE_ALIASES = {
    "approve": "approved", "approval": "approved", "lgtm": "approved",
    "pass": "passed", "green": "passed", "ok": "passed", "success": "passed",
    "fail": "failed", "failure": "failed", "red": "failed",
    "changes": "changes_required", "changes-required": "changes_required", "request_changes": "changes_required",
    "decision": "needs-decision", "needs_decision": "needs-decision", "blocked": "blocked",
    "na": "n/a", "skip": "skipped", "skipped": "skipped", "none": "n/a",
    "deploy": "deployed", "merge": "merged", "verify": "verified", "complete": "done", "completed": "done",
}


def canonical_state(state):
    s = str(state or "").strip().lower().replace(" ", "-")
    return STATE_ALIASES.get(s, s)


QUIET_AFTER = {"impl": 4 * 3600, "review": 3600, "gate": 3 * 3600, "merge": 1800,
               "deploy": 2700, "staging": 24 * 3600}
NOTE_SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def is_completed(r):
    """Finished = the last pipeline step done, or the tracker says Done."""
    pipeline = row_pipeline(r)
    st = (r["steps"].get(pipeline[-1]) or {}) if pipeline else {}
    tracker = tracker_state(r)
    done = st["cls"] == "done" if st.get("cls") else canonical_state(st.get("state")) in DONE_STATES
    return done or tracker in ("done", "completed")


def tracker_state(r):
    """Only live or explicitly recorded tracker evidence authorizes completion."""
    st = r.get("steps", {}).get(tracker_kind(r)) or {}
    return st.get("state", "") if not st.get("inferred") and (st.get("live") or st.get("at")) else ""


def issue_key(task_id):
    """app1-impl / rt-app1 / APP-1 -> APP-1."""
    m = re.match(r"^(?:rt-)?([a-z]+)-?(\d+)", (task_id or "").lower())
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


def task_step(task_id, pipeline=None):
    """The pipeline step a task id or worker name belongs to, from its role (app1-gate).
    A trailing number is herdr's suffix for a reused agent name (app1-gate-gate-2):
    strip it before identifying the role to avoid misclassifying a running gate."""
    parts = task_id.split("-")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    if pipeline is None:
        # Routing callers without a view row retain the conventional grant roles.
        role = ROLE_STEP.get(parts[-1], parts[-1])
        return role if role in set(DEFAULT_PIPELINE) | {"deploy", "staging"} else "impl"
    role = ROLE_STEP.get(parts[-1], parts[-1])
    return role if role in pipeline else "impl"


def checks_summary(rollup):
    """GitHub statusCheckRollup -> {"pass": n, "fail": n, "pending": n}."""
    out = {"pass": 0, "fail": 0, "pending": 0}
    for ck in rollup or []:
        v = (ck.get("conclusion") or ck.get("state") or ck.get("status") or "").upper()
        if v in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            out["pass"] += 1
        elif v in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE"):
            out["fail"] += 1
        else:
            out["pending"] += 1
    return out


def pr_states(checkout=None):
    """{branch: {number, state, draft, url, title, head, checks, base, updated_at}} from cached `gh pr list` calls (30 s):
    recent PRs in any state, plus CI check results for the open ones. None when GitHub
    couldn't be read (never cached), so callers can tell "no PRs" from "no answer"."""
    import shutil
    repo = os.environ.get("ROUTE_PR_REPO") or checkout
    if not repo:
        return {}
    repo = os.path.abspath(os.path.expanduser(repo))
    # Caches belong to checkouts, including the legacy env override.
    cache_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    cache = os.path.join(ROOT, "runs", f".pr-cache-v4-{cache_id}.json")
    try:
        if time.time() - os.path.getmtime(cache) < 30:
            return load_json(cache)
    except OSError:
        pass
    gh = shutil.which("gh")
    if not gh:
        return None

    def gh_list(state, fields, limit):
        p = subprocess.run([gh, "pr", "list", "--state", state, "--limit", str(limit), "--json", fields],
                           cwd=repo, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            raise OSError(f"gh pr list failed: {(p.stderr or '').strip()[-160:]}")
        return json.loads(p.stdout)

    try:
        fields = "number,state,isDraft,headRefName,headRefOid,url,title,baseRefName,updatedAt"
        open_prs = gh_list("open", fields + ",statusCheckRollup", 60)   # every open PR, however old
        checks = {pr["number"]: checks_summary(pr.get("statusCheckRollup")) for pr in open_prs}
        prs = gh_list("all", fields, 80) + open_prs
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    out = {}
    for pr in prs:
        b = pr.get("headRefName", "")
        if b not in out or pr["number"] > out[b]["number"]:
            out[b] = {"number": pr["number"], "state": pr["state"].lower(), "draft": pr.get("isDraft"),
                      "url": pr.get("url"), "title": pr.get("title", ""), "head": pr.get("headRefOid", ""),
                      "checks": checks.get(pr["number"]), "base": pr.get("baseRefName", ""),
                      "updated_at": pr.get("updatedAt", "")}
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


def stale_base(pr, prs):
    """For an open PR stacked on another PR's branch: that PR's number and state once it
    merged or closed (the stack needs a restack or a retarget). None otherwise, including
    when the base's PR is older than the cached list, since unknown isn't stale."""
    base = pr.get("base") or ""
    under = prs.get(base) if pr.get("state") == "open" else None
    if under and under.get("state") in ("merged", "closed") and under.get("number") != pr.get("number"):
        return {"branch": base, "number": under["number"], "state": under["state"]}
    return None


def pr_issue(branch, title, cfg, repo):
    """Prefer the project's branch template, then legacy branch/title matching."""
    pattern = issue_pattern(cfg, repo).pattern
    template = worktree_profile(cfg, repo)["branch"]
    branch_pattern = re.escape(template).replace(re.escape("{issue}"), f"({pattern})(?:-[A-Za-z0-9._-]+)?")
    # Git issue worktrees with a task suffix append it to the issue in the branch.
    m = re.fullmatch(branch_pattern, branch or "", re.I)
    m = m or re.search(r"([A-Za-z]{2,10}-\d+)", branch or "") or re.search(
        r"([A-Za-z]{2,10}-\d+)", title or "")
    return m.group(1).upper() if m else None


LINEAR_KEY_FILE = os.path.expanduser("~/.config/agent-routing/linear-api-key")


def linear_states(keys):
    """{issue: state name} straight from Linear, with a personal API key from
    LINEAR_API_KEY or LINEAR_KEY_FILE (outside this repo, never stored here; SSH
    shells may omit startup environment settings, so a file is more reliable). Cached 60 s. {} when
    there's no key; None when Linear couldn't be read (never cached)."""
    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        try:
            with open(LINEAR_KEY_FILE) as f:
                token = f.read().strip()
        except OSError:
            token = None
    keys = [k for k in keys if re.fullmatch(r"[A-Z]{2,10}-\d+", k)]   # '#1' rows aren't Linear issues
    if not token or not keys:
        return {}
    cache = os.path.join(ROOT, "runs", ".linear-cache-v2.json")
    try:
        if time.time() - os.path.getmtime(cache) < 60:
            got = load_json(cache)
            if set(keys) <= set(got.get("asked", [])):     # asked, even if Linear had no such issue
                return got.get("states", {})
    except (OSError, ValueError, AttributeError):
        pass
    import urllib.request
    out = {}
    for team in sorted({k.split("-")[0] for k in keys}):
        nums = [int(k.split("-")[1]) for k in keys if k.startswith(team + "-")]
        q = {"query": "query($t:String!,$n:[Float!]){issues(first:100,filter:{team:{key:{eq:$t}},number:{in:$n}})"
                      "{nodes{identifier state{name}}}}", "variables": {"t": team, "n": nums}}
        req = urllib.request.Request("https://api.linear.app/graphql", data=json.dumps(q).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": token})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                for n in json.load(resp)["data"]["issues"]["nodes"]:
                    out[n["identifier"]] = n["state"]["name"].lower().replace(" ", "-")
        except (OSError, ValueError, KeyError, TypeError):
            return None
    try:
        with open(cache, "w") as f:
            json.dump({"asked": keys, "states": out}, f)
    except OSError:
        pass
    return out


def expected_linear(r):
    """The tracker status the project's flow implies, or None."""
    if tracker_kind(r) != "linear":
        return None
    steps = r["steps"]
    flow = r.get("tracker_flow", {})
    for name in reversed(row_pipeline(r)):
        if (steps.get(name) or {}).get("cls") == "done" and name in flow.get("after", {}):
            return flow["after"][name]
    if any((steps.get(n) or {}).get("state") == "abandoned" for n in row_pipeline(r)):
        return None
    if r.get("pr") or any(n in steps for n in row_pipeline(r)):
        return flow.get("active")
    return None


def same_commit(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def ago_text(seconds):
    return f"{seconds / 60:.0f}m" if seconds < 3600 else f"{seconds / 3600:.0f}h" if seconds < 86400 else f"{seconds / 86400:.0f}d"


def classify(name, st, pr, unreachable, now):
    """Set st["cls"] and st["label"] (see the class list above)."""
    state = canonical_state(st.get("state"))
    if not st.get("active"):
        st["state"] = state or st.get("state")
    head = ((pr or {}).get("head") if name not in ("impl", "merge", "deploy")
            and (pr or {}).get("state") == "open" else None)
    if st.get("active"):
        if st.get("host") in unreachable:
            cls, label = "unknown", f"{st['host']} unreachable"
        elif state == "working":
            cls, label = "working", f"running on {st.get('host')}"
            if st.get("via"):
                label = f"running in {st['via']} on {st.get('host')}: {st.get('task')}"
            elif st.get("resumed"):
                label += f" ({st.get('task')}, fix round)"
        elif state == "blocked":
            cls, label = "need", f"needs a decision on {st.get('host')}"
        elif state in ("idle", "done"):
            cls, label = "wait", f"worker stopped on {st.get('host')}: read its output"
        else:
            cls, label = "unknown", f"{state or 'no status'} on {st.get('host')}"
        if head and st.get("sha") and not same_commit(st["sha"], head):
            label += f" (testing {st['sha'][:8]}, PR head is {head[:8]})"
            cls = "stale" if cls == "wait" else cls
    elif st.get("queued"):
        cls, label = "queued", f"queued #{st['queued']} on {st.get('host')}"
    elif state == "closed":
        cls, label = "unknown", "closed with no result recorded"
    elif state in DONE_STATES:
        cls, label = "done", state
    elif state in FAIL_STATES:
        cls, label = "fail", state
    elif state in NEED_STATES:
        cls, label = "need", "needs a decision"
    elif state in SKIP_STATES:
        cls, label = "skip", "n/a"
    else:
        cls, label = "active", state or "in progress"
        quiet = now - (st.get("at") or now)
        if st.get("at") and quiet > QUIET_AFTER.get(name, 3600):
            st["quiet"] = True
            label += f", no update for {ago_text(quiet)}"
    if cls in ("done", "fail") and head and st.get("sha") and not same_commit(st["sha"], head):
        cls, label = "stale", f"{state} on {st['sha'][:8]}, PR head is now {head[:8]}: re-run"
    if st.get("note") and cls not in ("working", "need", "wait"):
        label += f" · {st['note']}"
    st["cls"], st["label"] = cls, label


def worktree_issue(cwd, cfg):
    for project in cfg.get("projects", {}):
        wt = worktree_profile(cfg, project)
        pattern = re.escape(wt["dir"]).replace(re.escape("{issue}"), f"({issue_pattern(cfg, project).pattern})")
        m = re.search(r"/" + pattern + r"(?:-[A-Za-z0-9._-]+)?(?:/|$)", cwd or "", re.I)
        if m:
            return m.group(1).upper()
    return None


def attach_live_agents(report, issues, by_issue, row, now, cfg):
    """Live work that isn't an open task still belongs on its issue's row:
    - a worker re-prompted after its task closed (a fix round), found by its
      task-style name (app1-impl) or its issue worktree;
    - an orchestrator's in-process subagent (e.g. an Opus review) that names the
      issue or its PR (#1) and is still running.
    A live agent wins over a recorded mark for that step."""
    by_pr = {f"#{pr['number']}": key for key, pr in by_issue.items()
             if sum(p["number"] == pr["number"] for p in by_issue.values()) == 1}
    for u in report.get("untracked", []):
        status = u.get("status")
        name = u.get("name") or ""
        key = (issue_key(name) if re.match(r"^(?:rt-)?[a-z]+-?\d+(?:-|$)", name) else None) or \
            worktree_issue(u.get("cwd"), cfg)
        if key:
            u["issue"] = key                     # the lanes show it as this issue's work, not stray
        if key and status in ("working", "blocked"):
            step = task_step(name, row_pipeline(row(key))) if issue_key(name) else "impl"
            if step in row_pipeline(row(key)) and not row(key)["steps"].get(step, {}).get("active"):
                row(key)["steps"][step] = {"state": status, "host": u.get("host"), "task": name or u.get("pane_id"),
                                           "active": True, "resumed": True}
                row(key)["updated"] = now
        for sa in u.get("subagents") or []:
            if not sa.get("running"):
                continue
            desc = sa.get("description") or ""
            key = next((k for k in re.findall(r"\b[A-Z]{2,10}-\d+\b", desc) if k in issues), None) or \
                next((by_pr[p] for p in re.findall(r"#\d+", desc) if p in by_pr), None)
            step = None
            if key:
                pipeline = row_pipeline(row(key))
                named = re.search(r"\b" + re.escape(key) + r"\s+([^:\s]+)", desc, re.I)
                token = named.group(1).lower() if named else None
                # Else a verification step named in the text, as before (never impl/merge/deploy).
                step = token if token in pipeline else next((st for st in reversed(pipeline)
                                                             if st not in ("impl", "merge", "deploy") and st in desc.lower()), None)
            if key and step and not row(key)["steps"].get(step, {}).get("active"):
                row(key)["steps"][step] = {"state": "working", "host": u.get("host"), "active": True,
                                           "task": desc, "via": u.get("name") or "orchestrator"}
                row(key)["updated"] = now


def issues_view(report, queues=None, active_dir=None, cfg=None):
    """One row per issue in flight, across steps: routed tasks (open, queued, or closed in
    the last 14 days), the PR on GitHub (state, head, CI), steps the orchestrator marked,
    and Linear's own status when LINEAR_API_KEY is set. Every step is classified once
    here (classify), so the views only draw it."""
    cfg = cfg or {}
    now, issues = time.time(), {}
    report["project_prefixes"] = {project_profile(cfg, p)["issue_prefix"]: p for p in cfg.get("projects", {})
                                  if project_profile(cfg, p).get("issue_prefix")}
    report["default_project"] = cfg.get("default_repo")
    unreachable = {u.split(":", 1)[0] for u in report.get("unreachable", [])}

    closed_records, task_records = issue_records(active_dir, queues or {})
    # Resolve repositories before any rows are created (including queued rows or
    # marks). Current tasks override the newest closed record; prefixes are only
    # a fallback when no persisted repository is available.
    project_by_issue = recorded_issue_projects(closed_records, task_records + report.get("tasks", []))
    report["issue_projects"] = project_by_issue

    def row(key, project=None):
        if key not in issues:
            issues[key] = dict(view_profile(cfg, key, project_by_issue.get(key) or project),
                               issue=key, title="", pr=None, steps={}, updated=0)
        return issues[key]

    def commit_of(task_id):
        try:
            return load_json(os.path.join(active_dir or "", task_id + ".json")).get("project_commit")
        except (OSError, ValueError):
            return None

    for t in report.get("tasks", []):
        key = issue_key(t["task_id"])
        if not key:
            continue
        r = row(key)
        worker = next((a for a in t.get("agents", []) if a.get("role") == "worker"), None)
        step = task_step(t["task_id"], row_pipeline(r))
        if step:
            r["steps"][step] = {"state": (worker or {}).get("status") or t.get("dispatch", "open"),
                                               "host": t["host"], "task": t["task_id"], "active": True,
                                               "sha": commit_of(t["task_id"])}
        r["title"] = r["title"] or (t.get("summary") or "")
        r["updated"] = now
    for host, ids in (queues or {}).items():
        for k, task_id in enumerate(ids, 1):
            key = issue_key(task_id)
            step = task_step(task_id, row_pipeline(row(key))) if key else None
            if step and not row(key)["steps"].get(step, {}).get("active"):
                row(key)["steps"][step] = {"state": "queued", "queued": k, "host": host, "task": task_id}
                row(key)["updated"] = max(row(key)["updated"], now - 1)
    for t in closed_records:
        key, closed = issue_key(t.get("task_id")), t.get("closed_at", 0)
        if not key or now - closed > RECENT_S:
            continue
        r = row(key)
        step = task_step(t["task_id"], row_pipeline(r))
        cur = r["steps"].get(step, {})
        if step and not cur.get("active") and not cur.get("queued") and closed >= cur.get("at", 0):
            r["steps"][step] = {"state": t.get("outcome", "closed"), "host": t.get("selected_host"),
                                "task": t["task_id"], "at": closed, "sha": t.get("project_commit")}
        r["title"] = r["title"] or (t.get("summary") or "")
        r["updated"] = max(r["updated"], closed)
    for path in glob.glob(os.path.join(ISSUES_DIR, "*.json")):
        try:
            rec = load_json(path)
        except (OSError, ValueError):
            continue
        marks = rec.get("steps", {})
        latest = max([m.get("at", 0) for m in marks.values()] or [0])
        if now - latest > RECENT_S and rec.get("issue") not in issues:
            continue
        r = row(rec.get("issue"))
        for step, m in marks.items():
            if step not in row_pipeline(r) and (step != tracker_kind(r) or step == "none"):
                continue
            cur = r["steps"].get(step, {})
            # The newest record wins; a live or queued task always wins.
            if not cur.get("active") and not cur.get("queued") and (m.get("at", 0) >= cur.get("at", 0)):
                sha = m.get("sha") or next(iter(NOTE_SHA_RE.findall(m.get("note") or "")), None)
                r["steps"][step] = {"state": m.get("state"), "note": m.get("note"), "at": m.get("at"), "sha": sha}
        r["updated"] = max(r["updated"], latest)
    # Query each project's checkout once. The env override retains the old
    # single-project lookup, assigned to default_repo.
    projects = list(cfg.get("projects", {}))
    if cfg.get("default_repo") and cfg["default_repo"] not in projects:
        projects.append(cfg["default_repo"])
    if os.environ.get("ROUTE_PR_REPO"):
        projects = [cfg.get("default_repo")]
    by_issue, project_prs = {}, {}
    for project in projects:
        checkout = project_profile(cfg, project).get("pr", {}).get("checkout")
        if not checkout and not os.environ.get("ROUTE_PR_REPO"):
            project_prs[project] = {}
            continue
        prs = pr_states(checkout)
        if prs is None:
            report.setdefault("degraded", []).append(f"github:{project}")
            prs = {}
        project_prs[project] = prs
        for branch, pr in prs.items():
            key = pr_issue(branch, pr.get("title"), cfg, project)
            if key and (project_by_issue.get(key) or issue_project(cfg, key)) != project:
                key = None
            if not key:
                if pr["state"] != "open":
                    continue
                # The default project's observation keys must survive adding
                # other PR profiles; qualify only non-default repositories.
                key = f"#{pr['number']}" if project == cfg.get("default_repo") else f"{project}#{pr['number']}"
                row(key, project).update(updated=now - 3, unrouted=True)
            elif key not in issues and pr["state"] == "open":
                row(key, project).update(updated=now - 2, unrouted=True)
            if key not in by_issue or pr["number"] > by_issue[key]["number"]:
                by_issue[key] = pr
    for key, r in issues.items():
        pr = by_issue.get(key)
        if not pr:
            continue
        r["pr"] = pr = dict(pr, stale_base=stale_base(pr, project_prs.get(r["project"], {})))
        r["title"] = r["title"] or pr.get("title") or ""
        pipeline = row_pipeline(r)
        if "merge" in pipeline:
            if pr["state"] == "merged" and "merge" not in r["steps"]:
                r["steps"]["merge"] = {"state": "merged", "note": f"#{pr['number']}"}
            elif pr["state"] == "closed":
                r["steps"]["merge"] = {"state": "abandoned", "note": f"PR #{pr['number']} closed without merging"}
        if "impl" in pipeline and "impl" not in r["steps"]:
            r["steps"]["impl"] = {"state": "done", "note": "PR open" if pr["state"] == "open" else None}
    attach_live_agents(report, issues, by_issue, row, now, cfg)
    for project in sorted({r["project"] for r in issues.values() if tracker_kind(r) == "linear"}):
        keys = sorted(k for k, r in issues.items() if r["project"] == project and tracker_kind(r) == "linear")
        live = linear_states(keys)
        if live is None:
            report.setdefault("degraded", []).append(f"linear:{project}")
            live = {}
        for key in keys:
            r = issues[key]
            lin = r["steps"].get("linear")
            if key in live:
                r["steps"]["linear"] = {"state": live[key], "note": f"live from {tracker_label(r)}", "live": True}
            elif lin and lin.get("at"):
                lin["note"] = f"recorded {ago_text(now - lin['at'])} ago" + (f" · {lin['note']}" if lin.get("note") else "")
    for r in issues.values():
        pr = r.get("pr")
        impl = r["steps"].get("impl", {})
        fixing = impl.get("active") and impl.get("state") == "working"
        for name, st in r["steps"].items():
            if name == "linear":
                continue
            classify(name, st, pr, unreachable, now)
            # A fix round invalidates earlier verification verdicts, including custom steps.
            if fixing and name in outcome_steps(r) and st["cls"] in ("done", "fail"):
                st["cls"], st["label"] = "stale", f"{st['state']} before the current fix round"
        # Linear: live > recorded > inferred from the pipeline; flag a recorded or live
        # status that lags what the pipeline says.
        want, lin = expected_linear(r), r["steps"].get(tracker_kind(r))
        ranks = r["tracker_flow"].get("rank", {})
        if not lin and want:
            r["steps"]["linear"] = {"state": want, "inferred": True, "note": "inferred from the pipeline"}
        elif lin and want and 0 <= ranks.get(lin.get("state"), -1) < ranks.get(want, -1):
            lin["behind"] = want
        r["completed"] = not issue_data_degraded(report, r) and is_completed(r)
        if r["completed"]:
            for st in r["steps"].values():
                st.pop("active", None)
    return sorted(issues.values(), key=lambda r: (stage(r), -r["updated"]))


FEEDBACK_DIR = os.path.join(ROOT, "runs", "feedback")


def feedback_counts():
    """The routing maintainer's inbox at a glance: waiting, and handled in the last 24 h."""
    now, out = time.time(), {"new": 0, "done_24h": 0}
    for p in glob.glob(os.path.join(FEEDBACK_DIR, "*.json")):
        try:
            item = load_json(p)
        except (OSError, ValueError):
            continue
        if item.get("status") == "new":
            out["new"] += 1
        elif item.get("status") == "done" and now - (item.get("done_at") or 0) < 86400:
            out["done_24h"] += 1
    return out


def feedback_ops(a, orch):
    """The routing maintainer's inbox (runs/feedback/*.json on the routing home)."""
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    if a.feedback:
        item = {"id": f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}", "at": time.time(), "from": orch,
                "task": a.task, "text": a.feedback.strip()[:2000], "status": "new"}
        with open(os.path.join(FEEDBACK_DIR, item["id"] + ".json"), "x") as f:
            f.write(json.dumps(item, indent=2) + "\n")
        print(f"feedback:      {item['id']} filed for the routing maintainer")
        return 0
    if a.feedback_done:
        path = os.path.join(FEEDBACK_DIR, a.feedback_done + ".json")
        item = load_json(path)
        item.update(status="done", done_at=time.time(), resolution=a.note)
        with open(path, "w") as f:
            f.write(json.dumps(item, indent=2) + "\n")
        print(f"feedback:      {a.feedback_done} marked done")
        return 0
    deadline = time.time() + (a.events_timeout or 0) * 60
    while True:                                  # --feedback-wait
        new = []
        for p in sorted(glob.glob(os.path.join(FEEDBACK_DIR, "*.json"))):
            try:
                item = load_json(p)
            except (OSError, ValueError):
                continue
            if item.get("status") == "new":
                new.append(item)
        if new:
            for item in new:
                print(f"{item['id']}  from {item.get('from')}" + (f"  task {item['task']}" if item.get("task") else "")
                      + f"\n    {item['text']}")
            return 0
        if a.events_timeout and time.time() > deadline:
            print("TIMEOUT  no new feedback")
            return 3
        time.sleep(a.every or 20)


def stage(r):
    """Index of the first unfinished pipeline step, so issues list in pipeline
    order from its project profile; a finished pipeline sorts after them."""
    for i, n in enumerate(row_pipeline(r)):
        if (r["steps"].get(n) or {}).get("cls") not in ("done", "skip"):
            return i
    return len(row_pipeline(r))


def task_times(active_dir, now=None):
    """{task_id: (minutes since dispatch, timeout_minutes)} for dispatched open tasks.
    The dispatch time is in runs/tasks/<id>/link.json (the active entry's copy may predate it)."""
    out = {}
    for path in glob.glob(os.path.join(active_dir, "*.json")):
        try:
            t = load_json(path)
        except (OSError, ValueError):
            continue
        el = elapsed_min(t, now)
        if el is not None:
            out[t["task_id"]] = (el, int(t["resources"]["timeout_minutes"]))
    return out


def record_ages(active_dir, now=None):
    """{task_id: minutes since its active entry was written} (dispatch rewrites it,
    so for a task never dispatched this is the time since it was recorded)."""
    out = {}
    for path in glob.glob(os.path.join(active_dir, "*.json")):
        try:
            out[os.path.basename(path)[:-5]] = int(((now or time.time()) - os.path.getmtime(path)) // 60)
        except OSError:
            continue
    return out


NO_WORKER_MIN = 5        # an open task this long with no live worker holds its slot for nothing
NO_WORKER_SETUP_MIN = 15  # a dispatch still running (worktree setup may take 10 min) gets this long


def no_worker_age(t):
    """(minutes, why) when task row t should have a worker agent by now, else None.
    Its agents aren't looked at here. Command-mode tasks have no worker agent at all."""
    st, el = t.get("dispatch"), t.get("elapsed_min")
    if st == "dispatched":
        if not t.get("agent"):                         # mode: command
            return None
        return (el, "no worker agent on its host") if el is not None and el >= NO_WORKER_MIN else None
    if str(st or "").startswith("failed"):
        return (el or t.get("age_min") or 0, f"dispatch {st}") if (el or t.get("age_min") or 0) >= NO_WORKER_MIN else None
    if el is not None:                                 # dispatch still running (no state yet)
        return (el, "dispatch has not finished") if el >= NO_WORKER_SETUP_MIN else None
    age = t.get("age_min")
    return (age, "never dispatched (recorded only, or its plan run by hand)") \
        if age is not None and age >= NO_WORKER_MIN else None


def no_worker(report):
    """Open tasks on a reachable host that should have a worker agent and have none:
    [(task, minutes, why)]. Recorded but never dispatched (--next without --dispatch),
    a failed or stuck dispatch, or the worker gone."""
    down = {u.split(":", 1)[0].strip() for u in report.get("unreachable", [])}
    out = []
    for t in report.get("tasks", []):
        nw = no_worker_age(t)
        if t["host"] in down or not nw or t.get("interrupted") or any(ag.get("role") == "worker" for ag in t.get("agents", [])):
            continue
        out.append((t, *nw))
    return out


def utc_hm(epoch):
    return time.strftime("%H:%MZ", time.gmtime(epoch)) if epoch else "?"


def reset_lines(report):
    """One line per host that restarted under open tasks (mark_interrupted)."""
    by = {}
    for t in report.get("tasks", []):
        if t.get("interrupted"):
            by.setdefault((t["host"], t["interrupted"]["boot_at"]), []).append(t)
    return [f"HOST RESET {h} restarted {utc_hm(b)}: {len(ts)} open task(s) dispatched before it lost their worker, "
            f"children and containers: {', '.join(t['task_id'] + ' @ ' + (t['interrupted']['commit'] or '?') for t in ts)}. "
            f"Inspect each worktree (unpushed work, reports), record it, then --close <id> --outcome interrupted; "
            f"re-dispatch only with the human's OK"
            for (h, b), ts in sorted(by.items())]


def no_worker_lines(report):
    return [f"NO WORKER {t['task_id']} on {t['host']}: {why}, {age} min; it holds its "
            f"{'heavy ' if t.get('heavy') else ''}slot with nothing running: inspect (route --listen "
            f"{t['task_id']}), then --close it and route it again with --dispatch"
            for t, age, why in no_worker(report)]


def dispatched_epoch(task):
    """When the task was dispatched (its link's dispatched_at, epoch seconds), or None."""
    try:
        lp = os.path.join(ROOT, "runs", "tasks", task["task_id"], "link.json")
        at = (load_json(lp) if os.path.exists(lp) else task.get("link") or {}).get("dispatched_at")
        return calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")) if at else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def elapsed_min(task, now=None):
    """Minutes since the task was dispatched (its link's dispatched_at), or None."""
    at = dispatched_epoch(task)
    return None if at is None else int(((now or time.time()) - at) // 60)


def mark_interrupted(report, active_dir):
    """Open tasks dispatched before their host last booted: their worker, its children and
    every container stopped when the host restarted.
    Sets r["interrupted"] = {"boot_at", "commit"}. Routing only says so; the orchestrator
    inspects and closes them (AGENTS.md §1.9: never replay, ask)."""
    loads = report.get("loads") or {}
    for path in glob.glob(os.path.join(active_dir, "*.json")):
        try:
            t = load_json(path)
        except (OSError, ValueError):
            continue
        r = next((r for r in report.get("tasks", []) if r["task_id"] == t.get("task_id")), None)
        ld = (loads.get(r["host"]) or {}) if r else {}
        at = dispatched_epoch(t)
        if at is None or not ld.get("boot_id"):
            continue                             # never dispatched, or the host can't be read now
        try:
            then = (load_json(os.path.join(ROOT, "runs", "tasks", t["task_id"], "decision.json")).get("preflight")
                    or {}).get("boot_id")
        except (OSError, ValueError):
            then = None
        if then and then != "unknown":
            gone = then != ld["boot_id"]         # the boot it was dispatched in has ended
        else:                                    # dispatched before preflight reported boot_id:
            gone = bool(ld.get("boot_at")) and at < ld["boot_at"] - 120   # the clock, with slack
        if gone:
            r["interrupted"] = {"boot_at": ld.get("boot_at") or 0, "commit": (t.get("project_commit") or "")[:12]}


def signing_state():
    """This machine's commit signing: "locked" when git signs with gpg and the agent has no
    cached passphrase (it is lost on every restart: worker commits, pushes and merges then
    fail or hang on pinentry), "unlocked", or None when it doesn't apply. Never prompts."""
    try:
        if subprocess.run(["git", "config", "--global", "--get", "commit.gpgsign"], capture_output=True,
                          text=True, timeout=5).stdout.strip() != "true":
            return None
        with open(os.path.expanduser("~/.gnupg/gpg-agent.conf")) as f:
            if re.search(r"^pinentry-program.*pinentry-mac", f.read(), re.M):
                return None                       # a configured Keychain helper may supply it
    except FileNotFoundError:
        pass
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        p = subprocess.run(["gpg", "--batch", "--pinentry-mode", "error", "--clearsign"], input="ok",
                           capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:       # the agent is busy: a passphrase prompt is already waiting
        return "locked"
    except OSError:
        return None
    return "unlocked" if p.returncode == 0 else "locked"


DOCKER_RESTART_WSL = '"/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe" desktop restart'


def host_warnings(report, home):
    """Things only the human can fix after a restart: a locked GPG agent on the routing home,
    Docker down."""
    out = []
    if report.get("signing") == "locked":
        out.append(f"GPG AGENT LOCKED on {home}: signed commits fail (workers' commits and pushes, merges) and "
                   f"preflight blocks draft-pr there. It starts empty after every restart: ask the human to run, in "
                   f"their own terminal: echo test | gpg --clearsign >/dev/null")
    for h, ld in sorted((report.get("loads") or {}).items()):
        if (ld or {}).get("docker") == "down":
            fix = (f"; on WSL Docker Desktop's engine restarts with {DOCKER_RESTART_WSL} (relaunching the app only "
                   f"reopens its dashboard): the human's call" if ld.get("disk_path") == "/mnt/c" else "")
            out.append(f"DOCKER DOWN on {h}: heavy work there will fail{fix}")
    return out


AUTH_FAIL_RE = re.compile(r"Permission denied \(publickey|agent refused|sign_and_send_pubkey|Too many authentication failures")


def unreachable_hint(line):
    """Missing SSH authentication can look like an outage; explain the conditional remedy."""
    if not AUTH_FAIL_RE.search(line) or "SSH AGENT LOCKED" in line:
        return line
    host = line.split(":", 1)[0].strip()
    return (f"{line}  → SSH AGENT LOCKED: authentication failed; if the required key is not loaded, ask the human "
            f"to unlock it in their own terminal (e.g. ssh-add <SSH_KEY_PATH>). "
            f"{host}'s tasks can't be seen meanwhile; they are probably still running and keep their slots")


EVENTS_IDLE_WARN_MIN = 15   # matches events.CONSUMER_IDLE_MIN


def events_idle_min():
    """Minutes since an --events consumer last polled (events.py touches the heartbeat
    every poll), -1 if none ever did. An active consumer keeps worker stops visible."""
    state = os.environ.get("ROUTE_EVENTS_STATE") or os.path.join(ROOT, "runs", ".events-state.json")
    try:
        return int((time.time() - os.path.getmtime(state + ".heartbeat")) // 60)
    except OSError:
        return -1


def events_warning(report):
    """A loud line when tasks are open and nobody is reading --events, else None."""
    idle = report.get("events_idle_min")
    if idle is None or not report.get("tasks") or 0 <= idle < EVENTS_IDLE_WARN_MIN:
        return None
    since = "has never run" if idle < 0 else f"idle {idle} min"
    return (f"EVENTS CONSUMER {since.upper()} with {len(report['tasks'])} open task(s): nobody sees workers stop; "
            f"start: route.py --events --follow (under a line watcher)")


def gather_local(cfg, orch, active_dir):
    """gather() on this machine's own records, plus the issue pipeline view."""
    report, queues, cap = gather_records(cfg, orch, active_dir)
    times, ages = task_times(active_dir), record_ages(active_dir)
    for r in report.get("tasks", []):          # both readers: elapsed against the task's timeout
        if r["task_id"] in times:
            r["elapsed_min"], r["timeout_min"] = times[r["task_id"]]
        r["age_min"] = ages.get(r["task_id"])
    mark_interrupted(report, active_dir)
    report["unreachable"] = [unreachable_hint(u) for u in report.get("unreachable", [])]
    report["feedback"] = feedback_counts()
    if not cfg.get("routing_home") or orch == cfg["routing_home"]:   # the consumer runs on the home
        report["events_idle_min"] = events_idle_min()
        report["signing"] = signing_state()
    try:
        report["issues"] = issues_view(report, queues, active_dir, cfg)
    except Exception as e:   # the view must never break status
        report["issues"] = []
        report.setdefault("unreachable", []).append(f"issue view failed: {e}")
    return report, queues, cap


def gather_records(cfg, orch, active_dir):
    """Live tasks, queues and capacity: the Go collector when installed, else Python."""
    exe = collector_exe()
    if exe:
        req = {"config": cfg, "active_dir": active_dir, "queue_dir": QUEUE_DIR, "orchestrator": orch, "repo_root": ROOT}
        try:
            p = subprocess.run([exe, "snapshot"], input=json.dumps(req), capture_output=True, text=True, timeout=90)
            if p.returncode == 0:
                d = json.loads(p.stdout)
                return d["report"], d["queues"], d["capacity"]
            note = (p.stderr or p.stdout).strip()[-200:]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            note = str(e)
        report, queues, cap = gather_python(cfg, orch, active_dir)
        report.setdefault("unreachable", []).append(f"agent-collect failed ({note}); used the Python readers")
        return report, queues, cap
    return gather_python(cfg, orch, active_dir)


def gather_python(cfg, orch, active_dir):
    """The Python implementation of gather()."""
    import dispatch as D
    _, active = load_active({"task_id": "", "resources": {}}, cfg, active_dir)
    report = D.live_status(cfg, orch, active)
    cap, queues = {}, {}
    for h, hc in cfg["hosts"].items():
        on = [o for _, o, oh in active if oh == h]
        cap[h] = {"heavy": sum(1 for o in on if is_heavy(o)), "light": sum(1 for o in on if not is_heavy(o)),
                  "max_heavy": hc.get("max_heavy"), "max_queue": hc.get("max_queue", 10)}
        queues[h] = [queue_id(p) for p in queue_files(h)]
        if orch in hc.get("reach", {}):
            cap[h]["load"] = report.get("loads", {}).get(h)      # already fetched by the per-host probe
            if (report.get("heavy") or {}).get(h) is not None:
                cap[h]["slots"] = report["heavy"][h]
            cap[h]["levels"] = load_levels(cap[h]["load"], hc.get("hot_limits"))
    report.pop("heavy", None)                  # carried in capacity, as the Go collector does
    return report, queues, cap


def snapshot(cfg, orch, active_dir):
    """The data behind every view (route.py --snapshot): {report, queues, capacity}.
    The Go collector (agent-collect snapshot) produces exactly this shape."""
    report, queues, cap = gather(cfg, orch, active_dir)
    return {"report": report, "queues": queues, "capacity": cap}


def queue_ops(a):
    """--close / --dequeue / --queue / --next. All file operations under runs/."""
    local_cfg = os.path.join(ROOT, "config", "hosts.local.json")
    cfg = load_config(a.config or (local_cfg if os.path.exists(local_cfg)
                                 else os.path.join(ROOT, "config", "hosts.example.json")))
    active_dir = a.active_dir or os.path.join(ROOT, "runs", "active")
    os.makedirs(active_dir, exist_ok=True)
    rc = 0
    orch = a.orch or detect_orchestrator(cfg)
    if a.listen and a.handoff:
        import dispatch as D
        try:
            out = D.worker_handoff(a.listen, cfg, orch)
        except D.DispatchError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        if a.json:
            print(json.dumps(out, indent=2))
            return 0
        f = out["facts"]
        print(f"task {out['task_id']} on {out['host']}  agent {out.get('agent') or '-'}  "
              f"status {out.get('agent_status') or '-'}")
        print(f"herdr: {out['herdr']}")
        print(f"PRs: {', '.join('#' + p for p in f['prs']) or '-'}   SHAs: {', '.join(x[:12] for x in f['shas']) or '-'}")
        for line in f["tests"]:
            print(f"tests: {line[:160]}")
        for line in f["questions"]:
            print(f"question: {line[:160]}")
        print("--- last reply ---")
        print(out.get("last_message") or "(no reply found in the worker's session file; use --listen without --handoff)")
        return 0
    if a.listen:
        import dispatch as D
        try:
            out = D.listen(a.listen, cfg, orch, a.lines, a.wait_ms)
        except D.DispatchError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"task {out['task_id']} on {out['host']}  workspace {out['workspace_id']}  "
                  f"agent {out.get('agent') or '-'}  status {out.get('agent_status') or '-'}")
            print(f"herdr: {out['herdr']}")
            print(out["output"].rstrip())
        return 0
    if a.snapshot:
        print(json.dumps(snapshot(cfg, orch, active_dir)))
        return 0
    if a.reap:
        import reap as RP
        if not RP.on_home(cfg, orch):
            print(f"error: --reap runs on the routing home ({cfg.get('routing_home')}), where the closed tasks live",
                  file=sys.stderr)
            return 3
        report, queues, _ = gather(cfg, orch, active_dir)
        cands = RP.candidates(report, queues)
        if report.get("unreachable"):
            print(f"note: not checked: {'; '.join(report['unreachable'])}")
        if not cands:
            print("nothing to reap")
            return 0
        if not a.apply:
            for c in cands:
                print(f"{c['issue']:9} {c['host']:7} {c['name']:28} {c['status']:5} {c['reason']}")
            print(f"{len(cands)} agent(s); close their tabs with: route.py --reap --apply")
            return 0
        import dispatch as D
        res = RP.apply(cands, cfg, orch, D.herdr, active_dir)
        for e in res:
            print(f"{e['issue']:9} {e['host']:7} {e['name']:28} {e['result']}")
        done = sum(e["result"].startswith("reaped") for e in res)
        print(f"reaped {done} of {len(res)}; logged in runs/reaped.jsonl (worktrees and session files kept)")
        return 0
    if a.feedback or a.feedback_wait or a.feedback_done:
        return feedback_ops(a, orch)
    if a.events:
        import events as E
        return E.run(lambda: gather(cfg, orch, active_dir), a.every or 15, a.follow, a.events_timeout * 60,
                     cfg=cfg, orch=orch)
    if a.watch:
        import watch as W
        W.EXPAND = a.expand
        return W.run(lambda: gather(cfg, orch, active_dir), cfg, a.every or 5)
    if a.dashboard:
        import dispatch as D
        import dashboard as M
        while True:
            report, queues, cap = gather(cfg, orch, active_dir)
            page = M.render(cfg, report, queues, cap, refresh=a.every or None)
            tmp = a.dashboard + ".tmp"
            with open(tmp, "w") as f:
                f.write(page)
            os.replace(tmp, a.dashboard)
            if not a.every:
                print(f"wrote {a.dashboard}")
                if a.dashboard.startswith("/home/"):
                    print("open on Windows: \\\\wsl.localhost\\Ubuntu" + a.dashboard.replace("/", "\\"))
                return 0
            time.sleep(a.every)
    if a.blocked or a.answer or a.question or a.reply or a.steer:
        import dispatch as D
        _, active = load_active({"task_id": "", "resources": {}}, cfg, active_dir)
        try:
            if a.steer:
                print(D.steer(*a.steer, cfg, orch, active))
                import events as E
                E.forget(a.steer[0])      # if it landed as a new turn, its next stop is reported again
                return 0
            if a.question:
                print(D.question(a.question, cfg, orch))
                print(f"reply with: route.py --reply {a.question} \"<text>\"   (never --answer esc: it interrupts the run)")
                return 0
            if a.answer or a.reply:
                tid = (a.answer or a.reply)[0]
                print(D.answer(*a.answer, cfg, orch) if a.answer else D.reply(*a.reply, cfg, orch))
                import events as E
                E.forget(tid)             # its next dialog, if any, is reported on the next --events run
                return 0
            found = D.blocked(cfg, orch, active)
        except D.DispatchError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        if a.json:
            print(json.dumps(found, indent=2))
        elif not found:
            print("no blocked workers")
        for b in found:
            if b["kind"] == "question":
                print(f"QUESTION: {b['task_id']} on {b['host']} (agent {b['agent']}, "
                      f"{'still running' if b['running'] else 'waiting'}) asks a question; answer within its grant "
                      f"({b['grant']}). Read: route.py --question {b['task_id']}, then route.py --reply "
                      f"{b['task_id']} \"<text>\". Never --answer esc here: it interrupts the run")
            else:
                print(f"BLOCKED: {b['task_id']} on {b['host']} (agent {b['agent']}) — decide within its grant "
                      f"({b['grant']}), or ask the human if it's beyond it. Answer: route.py --answer {b['task_id']} <key>")
            print(f"  herdr: {b['herdr']}")
            print("  " + "\n  ".join(b["dialog"].splitlines()))
        return 4 if found else 0
    status_view = None
    if a.status:
        # gather(): away from the routing home this is read on the home itself.
        rep, s_queues, s_cap = gather(cfg, orch, active_dir)
        status_view = (s_queues, s_cap)
        rep["herdr"] = herdr_prefixes(cfg, orch)
        if a.json:
            print(json.dumps(rep, indent=2))
            return 0
        for t in rep["tasks"]:
            loc = f"{t['group']} > {t['tab_label']}" if t.get("group") and t.get("tab_label") else f"ws {t['workspace_id'] or '-'}"
            el, to = t.get("elapsed_min"), t.get("timeout_min")
            run = f"  {el}/{to} min{' OVER' if el >= to else ''}" if el is not None and to else ""
            if t.get("interrupted"):            # its clock stopped when the host went down
                run = f"  INTERRUPTED: {t['host']} restarted {utc_hm(t['interrupted']['boot_at'])}"
            print(f"{t['task_id']:28} {t['host']:8} {'heavy' if t['heavy'] else 'light':5} {loc:22} {t['dispatch']}{run}")
            g = t.get("git") or {}
            if g.get("error"):
                print(f"    git    {g['error']}")
            elif g:
                age = f" (oldest {int((time.time() - g['oldest_unpushed']) // 60)} min)" if g.get("oldest_unpushed") else ""
                print(f"    git    {g['unpushed']} unpushed{age}{'' if g.get('pushed') else ', branch never pushed'}"
                      f" · {g['stash']} stash · {g['dirty']} dirty")
            for ag in t["agents"]:
                pf = f"{ag.get('model') or ''} {ag.get('effort') or ''}{' fast' if ag.get('fast') else ''}".strip()
                print(f"    {ag['role']:6} {ag['name'] or ag['pane_id']:24} {ag['agent']:8} {ag['status'] or '-':8} {pf}")
        for u in rep["untracked"]:
            pf = f"{u.get('model') or ''} {u.get('effort') or ''}{' fast' if u.get('fast') else ''}".strip()
            print(f"untracked:     {u['host']} {u['name'] or u['pane_id']} ({u['agent']}, {u['status']}) {pf}  cwd {u['cwd']}")
        for u in rep["unreachable"]:
            print(f"unreachable:   {u}")
        print("herdr from here: " + "   ".join(f"{h}: {p}" for h, p in rep["herdr"].items()))
        a.queue = True
    if a.mark:
        key = issue_key(a.mark[0]) or a.mark[0].upper()
        step, state = a.mark[1].lower(), canonical_state(a.mark[2])
        projects = recorded_issue_projects(*issue_records(active_dir, cfg.get("hosts", {})))
        profile = view_profile(cfg, key, projects.get(key))
        if step != tracker_kind(profile) and state not in DONE_STATES | FAIL_STATES | NEED_STATES | SKIP_STATES:
            print(f"note: {a.mark[2]!r} isn't a result word the views know; it shows as in progress. Results: "
                  f"{', '.join(sorted(DONE_STATES | FAIL_STATES | SKIP_STATES))}", file=sys.stderr)
        allowed = row_pipeline(profile) + ([tracker_kind(profile)] if tracker_kind(profile) != "none" else [])
        if step not in allowed:
            print(f"error: unknown step {step!r}; use one of {allowed}", file=sys.stderr)
            return 2
        os.makedirs(ISSUES_DIR, exist_ok=True)
        path = os.path.join(ISSUES_DIR, key + ".json")
        rec = load_json(path) if os.path.exists(path) else {"issue": key, "steps": {}}
        if a.sha and not re.fullmatch(r"[0-9a-fA-F]{7,40}", a.sha):
            print(f"error: --sha {a.sha!r} is not a commit SHA", file=sys.stderr)
            return 2
        known = DONE_STATES | FAIL_STATES | NEED_STATES | SKIP_STATES
        if step in outcome_steps(profile) and state in known and not a.sha:
            print(f"note: no --sha given; the views can't tell if this {step} result goes stale", file=sys.stderr)
        rec["steps"][step] = {"state": state, "at": time.time(), "note": a.note, "sha": (a.sha or "").lower() or None}
        with open(path, "w") as f:
            f.write(json.dumps(rec, indent=2) + "\n")
        print(f"marked:        {key} {step} = {state}" + (f" ({a.note})" if a.note else ""))
        return 0
    if a.close:
        hits = [p for p in glob.glob(os.path.join(active_dir, "*.json"))
                if os.path.basename(p)[:-5].lower() == a.close.lower()]
        if not hits:
            print(f"error: no open task {a.close} in {active_dir}", file=sys.stderr)
            return 2
        closing = []
        for p in hits:
            try:
                t = load_json(p)
            except (OSError, ValueError):
                t = {"task_id": a.close}
            closing.append((p, t))
            profile = view_profile(cfg, issue_key(a.close), (t.get("repository") or {}).get("name"))
            step = task_step(a.close.lower(), row_pipeline(profile))
            if step in outcome_steps(profile) and not a.outcome:
                print(f"error: {a.close} is a {step} task: say how it ended with --outcome "
                      f"(e.g. passed, failed, approved, changes_required), so the views never show a guess",
                      file=sys.stderr)
                return 2
        os.makedirs(CLOSED_DIR, exist_ok=True)
        for p, t in closing:
            t["closed_at"] = time.time()
            t["outcome"] = a.outcome or "closed"
            with open(os.path.join(CLOSED_DIR, os.path.basename(p)), "w") as f:
                f.write(json.dumps(t, indent=2) + "\n")
            os.remove(p)
        print(f"closed:        {a.close} ({a.outcome or 'closed'})")
    if a.dequeue:
        hits = [p for h in cfg["hosts"] for p in queue_files(h) if queue_id(p) == a.dequeue.lower()]
        if not hits:
            print(f"error: {a.dequeue} is not queued", file=sys.stderr)
            return 2
        for p in hits:
            os.remove(p)
        print(f"dequeued:      {a.dequeue}")
    if a.next:
        rc = drain(a, cfg, active_dir)
    if a.queue or a.close or a.dequeue:
        dummy = {"task_id": "", "resources": {}}
        _, active = load_active(dummy, cfg, active_dir)
        for h in sorted(cfg["hosts"]):
            if status_view:                      # counts from the same snapshot as the tasks above
                tasks_h = [t for t in rep["tasks"] if t["host"] == h]
                heavy = [t["task_id"] for t in tasks_h if t["heavy"]]
                light = [t["task_id"] for t in tasks_h if not t["heavy"]]
                qs = status_view[0].get(h, [])
            else:
                on = [o for _, o, oh in active if oh == h]
                heavy = [o["task_id"] for o in on if is_heavy(o)]
                light = [o["task_id"] for o in on if not is_heavy(o)]
                qs = [queue_id(p) for p in queue_files(h)]
            print(f"{h:8} heavy {len(heavy)}/{cfg['hosts'][h].get('max_heavy', '-')} {heavy}  light {len(light)}  "
                  f"queued {len(qs)}/{cfg['hosts'][h].get('max_queue', 10)} {qs}")
            if a.status:
                ld = (rep.get("loads") or {}).get(h) if isinstance(rep, dict) else host_load(cfg, h, orch)
                if ld:
                    lv = load_levels(ld, cfg["hosts"][h].get("hot_limits"))
                    gb = lambda b: f"{(b or 0) / 2**30:.1f}"
                    print(f"         load {ld.get('load1')}/{ld.get('cores')} cores ({lv.get('cpu')})  "
                          f"mem {gb(ld.get('mem_avail'))}/{gb(ld.get('mem_total'))} GB free"
                          f"{', pressure ' + ld['pressure'] if ld.get('pressure') not in (None, 'normal') else ''} ({lv.get('mem')})  "
                          f"swap {gb(ld.get('swap_used'))}/{gb(ld.get('swap_total'))} GB ({lv.get('swap')})  "
                          f"docker {ld.get('docker')} {gb(ld.get('docker_used'))}/{gb(ld.get('docker_limit'))} GB "
                          f"({lv.get('docker', '-')})  "
                          + (f"disk {ld['disk_path']} {gb(ld.get('disk_free'))}/{gb(ld.get('disk_total'))} GB free  "
                             if ld.get("disk_path") and ld.get("disk_free") is not None else "")
                          + f"overall {lv['overall'].upper()}")
                    for o in orphan_lines(ld.get("orphans")):
                        print(f"         {o}")
        for w in stale_entries(active):
            print(f"warning:       {w}")
        ew = events_warning(rep) if status_view else None
        if ew:
            print(f"WARNING:       {ew}")
        for nw in no_worker_lines(rep) if status_view else []:
            print(f"WARNING:       {nw}")
        for hw in host_warnings(rep, cfg.get("routing_home") or orch) if status_view else []:
            print(f"WARNING:       {hw}")
        for rw in reset_lines(rep) if status_view else []:
            print(f"WARNING:       {rw}")
        if any(queue_files(h) for h in cfg["hosts"]) and not a.next:
            print("next:          route.py --next --dispatch to start the oldest queued task that fits (without --dispatch: a dry run)")
    return rc


def orphan_lines(orphans):
    """--status lines for dev servers whose starter is gone (read-only: stopping one is
    the human's call, and only once no open task owns its directory)."""
    def age(s):
        return f"{s // 86400} d" if s >= 86400 else f"{s // 3600} h" if s >= 3600 else f"{s // 60} min"
    return [f"ORPHAN {o.get('cmd')} pid {o.get('pid')}, {age(o.get('age_s') or 0)} old, "
            f"port {','.join(map(str, o.get('ports') or [])) or '?'}, {(o.get('rss') or 0) / 2**20:.0f} MB: {o.get('cwd') or '?'}"
            for o in sorted(orphans or [], key=lambda o: -(o.get("age_s") or 0))]


def drain(a, cfg, active_dir):
    """Start the oldest queued task per host that now fits. A head still waiting for a
    slot keeps everything behind it waiting on that host (first in, first out); a
    *blocked* head (stale commit, missing tool) needs a person, not a slot, so the next
    task behind it may start. A load-shared task (no explicit host) queued on a full host
    may start on another capable host with room, in queue order; an explicit host request never moves.
    `--host X` drains X only. Without --dispatch nothing is recorded or dequeued: a task
    recorded without a worker would hold its heavy slot with nothing running.
    --record alone still records,
    for a plan run by hand."""
    go, keep = getattr(a, "dispatch", False), getattr(a, "record", False)
    only = getattr(a, "host", None)
    started, failed, taken = 0, 0, set()
    for h in sorted(cfg["hosts"]):
        if only and h != only:
            continue
        own = queue_files(h)
        movable = [p for g in sorted(cfg["hosts"]) if g != h for p in queue_files(g)
                   if not (load_json(p).get("routing") or {}).get("requested_host")]
        # Queue order across both: the file name starts with the time it was queued.
        for path in sorted(own + movable, key=lambda p: os.path.basename(p)):
            if path in taken or not os.path.exists(path):
                continue
            mine = path in own
            task = load_json(path)
            # Pin it to the host it starts on, so placement can't move it at start time
            # (rule 2: never silently switch hosts). Its brief is written for that host.
            task["_queued_for"] = h
            if not mine:
                task.pop("_resolved_ref", None)   # resolved on the host it queued on, not this one
            a.active_dir, a.record, a.draining = active_dir, True, True
            d = decide(a, cfg, a.config or "config", task)
            if d["selected_host"] != h:
                if mine:
                    print(f"{h}: queued {task['task_id']} would start on {d['selected_host']}, not {h}; left queued "
                          "(a queued task never changes host by itself)")
                continue
            if d["blockers"]:
                if mine:
                    print(f"{h}: queued {task['task_id']} is BLOCKED, left queued (fix it or --dequeue it): "
                          f"{'; '.join(d['blockers'])}")
                continue
            if d["queue_reasons"]:            # h has no room (the same for every task): stop here
                if mine:
                    print(f"{h}: queued {task['task_id']} still waiting: {'; '.join(d['queue_reasons'])}")
                break
            d["status"] = "routed"
            d["handoff_note"] = handoff_note(task, d, cfg, d["selected_host"])
            src = "" if mine else f" (queued on {os.path.basename(os.path.dirname(path))}, load-shared)"
            if not (go or keep):
                print(f"{h}: {task['task_id']}{src} would start now (dry run: nothing recorded, still queued; "
                      f"add --dispatch to start it)")
                taken.add(path)
                break
            try:
                d["recorded"] = record(task, d, d["handoff_note"], active_dir, cfg)
            except FileExistsError:
                print(f"{os.path.basename(os.path.dirname(path))}: queued {task['task_id']} is already open in "
                      f"{active_dir}; left queued (close the open one or --dequeue this)")
                if mine:
                    break
                continue
            os.remove(path)
            taken.add(path)
            if not go:
                print(f"{h}: recorded {task['task_id']}{src} from the queue, NOT started: it holds its slot "
                      f"with no worker until you run its plan")
                print_human(d)
                started += 1
                break
            print(f"{h}: dispatching {task['task_id']}{src} from the queue")
            if run_dispatch(a, task, d, cfg) == 0:
                started += 1
            else:
                failed += 1
            break
    if failed:
        return 3
    if not started:
        print("next:          nothing started" if go or keep else "next:          dry run; nothing started")
    return 0 if started else 4


def print_human(d):
    print(f"task:          {d['task_id']}")
    print(f"routing_ref:   {d['routing_ref']}")
    print(f"orchestrator:  {d['orchestrator_host']}")
    print(f"selected host: {d['selected_host']}  ({d['rule']})")
    print(f"repository:    {d['repository']} @ {d['project_commit']}")
    print(f"authorization: {d['authorization_scope']}  — execution_authorized: NO (recommendation only)")
    print(f"load class:    {'heavy (counts against max_heavy)' if d.get('heavy') else 'light (uncapped)'}")
    for w in d["warnings"]:
        print(f"warning:       {w}")
    if "preflight" in d:
        print("preflight:")
        for k, v in d["preflight"].items():
            print(f"  {k}={v}")
    elif "preflight_command" in d:
        print(f"preflight cmd: {d['preflight_command']}")
    for b in d["blockers"]:
        print(f"BLOCKER:       {b}")
    for r in d.get("queue_reasons", []):
        print(f"waits for:     {r}")
    if d.get("queue_position"):
        print(f"queued:        position {d['queue_position']} on {d['selected_host']} "
              f"(start it later with --next; nothing runs in the background)")
    if d.get("env"):
        print(f"runtime:       port slot {d['port_slot']}: " + " ".join(f"{k}={v}" for k, v in d["env"].items()))
    if d.get("recorded"):
        print(f"recorded:      {d['recorded']}/ (task.json, decision.json, handoff.md) + active marker")
    if d.get("plan"):
        print("dry-run plan (fill __PLACEHOLDERS__ from live JSON responses; review every command;"
              " never paste one unfilled):")
        for i, s in enumerate(d["plan"], 1):
            print(f"  {i}. {s['step']}")
            for c in s["commands"]:
                print(f"       {'' if c.startswith('#') else '$ '}{c}")
    print(f"status:        {d['status'].upper()}")


if __name__ == "__main__":
    sys.exit(main())
