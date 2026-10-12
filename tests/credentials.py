"""Staging login contract using fake stores/helpers; never reads real credentials."""
import copy
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "fake-password-$();`never-in-argv`"
EMAIL = "test@example.invalid"
EXAMPLE = json.loads((ROOT / "tests/fixtures/hosts.golden.json").read_text())

# Every external command on PATH records argv and checks its environment. Real
# utilities are then exec'd with those same arguments. The helper deliberately
# echoes its stdin on both output streams, including on failure.
FAKE = r'''
import json, os, signal, sys
from pathlib import Path
name = Path(sys.argv[0]).name
state = Path(os.environ["FAKE_STATE"])
data = json.loads((state / "fixture.json").read_text())
with (state / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"name": name, "argv": sys.argv[1:],
        "secret_in_env": bool(data["secret"]) and any(data["secret"] in v for v in os.environ.values()),
        "credential_vars": [k for k in ("secret", "email", "entry") if k in os.environ]}) + "\n")
if name == "uname":
    print(os.environ["FAKE_SYSTEM"])
elif name in ("security", "pass"):
    if data.get("store_fail"):
        print(data["secret"], file=sys.stderr)
        sys.exit(1)
    if name == "pass":
        print(data["secret"] + "\nlogin: " + data["email"] + "\nlogin: ignored@example.invalid")
    elif "-w" in sys.argv:
        print(data["secret"])
    else:
        print('    "acct"<blob>="' + data["email"] + '"')
elif name in ("pnpm", "npx", "other-helper"):
    stdin = sys.stdin.read()
    (state / "stdin.txt").write_text(stdin)
    (state / "cwd.txt").write_text(os.getcwd())
    print(stdin)
    print(stdin, file=sys.stderr)
    output_flag = "--out" if "--out" in sys.argv else "--state"
    out = Path(sys.argv[sys.argv.index(output_flag) + 1])
    if not data.get("no_session"):
        if data.get("output_symlink"):
            out.symlink_to(data["output_symlink"])
        elif data.get("output_fifo"):
            os.mkfifo(out)
        else:
            out.write_text("" if data.get("empty_session") else "{}\n")
        if data.get("loose_mode"):
            out.chmod(0o644)
    if data.get("signal"):
        os.kill(os.getppid(), getattr(signal, data["signal"]))
    sys.exit(data.get("helper_exit", 0))
else:
    if name == "mkdir" and data.get("mkdir_fail"):
        sys.exit(1)
    if name == "chmod" and (data.get("chmod_fail") or
            (data.get("file_chmod_fail") and sys.argv[1] == "600")):
        sys.exit(1)
    if name == "python3":
        code = sys.argv[2]
        if data.get("clear_fail") and "os.unlink(entry.path)" in code:
            sys.argv[2] = "import os\ndef denied(*a, **k): raise PermissionError()\nos.unlink = denied\n" + code
        if data.get("clear_scan_fail") and "os.unlink(entry.path)" in code:
            sys.argv[2] = "import os\ndef denied(*a, **k): raise PermissionError()\nos.scandir = denied\n" + code
        if data.get("foreign_dir") and "info = os.lstat(directory)" in code:
            sys.argv[2] = code.replace("info.st_uid != os.getuid()", "True")
    os.execv(data["real"][name], [data["real"][name]] + sys.argv[1:])
'''


class StagingLogin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="staging-login-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for name in ("scripts", "config", "bin", "home", "checkout"):
            (self.base / name).mkdir()
        self.script = self.base / "scripts/credentials.sh"
        shutil.copy2(ROOT / "scripts/credentials.sh", self.script)
        shutil.copy2(ROOT / "scripts/host_identity.py", self.base / "scripts/host_identity.py")
        (self.base / "checkout/package.json").write_text("{}")
        self.config = copy.deepcopy(EXAMPLE)
        self.write_config(local=False)
        self.fixture = {"secret": SECRET, "email": EMAIL, "real": {}}
        for name in ("dirname", "python3", "sed", "head", "mkdir", "chmod", "rm",
                     "uname", "security", "pass", "pnpm", "npx", "other-helper"):
            if name not in ("uname", "security", "pass", "pnpm", "npx", "other-helper"):
                self.fixture["real"][name] = shutil.which(name)
            fake = self.base / "bin" / name
            fake.write_text("#!" + sys.executable + "\n" + FAKE)
            fake.chmod(0o755)
        # A clean environment avoids reaching a real credential store or agent.
        self.env = {"HOME": str(self.base / "home"), "PATH": str(self.base / "bin"),
                    "FAKE_STATE": str(self.base), "FAKE_SYSTEM": "Linux"}
        self.calls = []

    def write_config(self, local=True):
        name = "hosts.local.json" if local else "hosts.example.json"
        (self.base / "config" / name).write_text(json.dumps(self.config))

    def run_login(self, *args, code=0, trace=False, allexport=False):
        (self.base / "fixture.json").write_text(json.dumps(self.fixture))
        (self.base / "calls.jsonl").write_text("")
        result = subprocess.run([shutil.which("bash"), *(["-x"] if trace else []), *(["-a"] if allexport else []),
                                 str(self.script), *args], cwd=self.base / "checkout",
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.calls = [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]
        for call in self.calls:
            self.assertNotIn(SECRET, json.dumps(call["argv"]), call["name"])
            self.assertFalse(call["secret_in_env"], call["name"])
            self.assertEqual(call["credential_vars"], [], call["name"])
        return result

    def named(self, name):
        return [call["argv"] for call in self.calls if call["name"] == name]

    def seed_session(self):
        out = self.base / "home/.cache/app-staging/admin.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("old session")
        return out

    def assert_session(self, out, argv, helper="pnpm", worktree=None):
        self.assertEqual(self.named(helper), [argv])
        self.assertEqual((self.base / "stdin.txt").read_text(), EMAIL + "\n" + SECRET + "\n")
        self.assertEqual(os.path.realpath((self.base / "cwd.txt").read_text()),
                         os.path.realpath(worktree or self.base / "checkout"))   # macOS: /var -> /private/var
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(out.parent.stat().st_mode), 0o700)
        self.assertEqual(out.read_text(), "{}\n")

    def test_existing_profile_both_stores_and_all_roles(self):
        expected = {"worktree_dir": ".worktrees/{issue}-staging",
                    "roles": ["admin", "manager", "editor", "viewer"],
                    "keychain_service": "app-staging-{role}", "pass_entry": "app/staging/{role}",
                    "session_dir": "~/.cache/app-staging",
                    "session_cmd": "pnpm --silent session:create --role {role} --out {out}"}
        self.assertEqual(self.config["projects"]["example-app"]["staging"], expected)
        for system in ("Linux", "Darwin"):
            self.env["FAKE_SYSTEM"] = system
            for role in expected["roles"]:
                with self.subTest(system=system, role=role):
                    result = self.run_login(role)
                    out = self.base / "home/.cache/app-staging" / (role + ".json")
                    argv = ["--silent", "session:create", "--role", role, "--out", str(out)]
                    if system == "Darwin":
                        self.assertEqual(self.named("security"), [
                            ["find-generic-password", "-s", "app-staging-" + role],
                            ["find-generic-password", "-s", "app-staging-" + role, "-w"]])
                        self.assertEqual(self.named("pass"), [])
                        self.assert_session(out, argv)
                    else:
                        self.assertEqual(self.named("pass"), [["show", "app/staging/" + role]])
                        self.assertEqual(self.named("security"), [])
                        self.assert_session(out, argv)
                    self.assertEqual(result.stdout, "session: " + str(out) + "\n"
                        "use it as Playwright storageState; it's a credential: don't read or print it; remove with --clear\n")
                    self.assertEqual(result.stderr, "")

    def add_project(self):
        self.config["projects"]["other"] = {"staging": {
            "roles": ["auditor"], "keychain_service": "other-stg:{role}@test",
            "pass_entry": "other/stg/{role}", "session_dir": "~/.cache/other-stg",
            "session_cmd": "{pm} login --state {out} --actor {role}"}}
        for host in self.config["hosts"].values():
            host["repos"]["other"] = {"pnpm_cmd": "other-helper --quiet"}
        self.write_config()

    def test_second_project_and_default_repo(self):
        self.add_project()
        worktree = self.base / "other checkout"
        worktree.mkdir()
        (worktree / "package.json").write_text("{}")
        for system in ("Linux", "Darwin"):
            self.env["FAKE_SYSTEM"] = system
            self.run_login("--repo", "other", "auditor", "--worktree", str(worktree))
            out = self.base / "home/.cache/other-stg/auditor.json"
            self.assert_session(out, ["--quiet", "login", "--state", str(out), "--actor", "auditor"], "other-helper", worktree)
            if system == "Darwin":
                self.assertEqual(self.named("security")[0], ["find-generic-password", "-s", "other-stg:auditor@test"])
            else:
                self.assertEqual(self.named("pass"), [["show", "other/stg/auditor"]])
        self.config["default_repo"] = "other"
        self.write_config()
        self.run_login("auditor")
        self.assertTrue(self.named("other-helper"))

    def test_shlex_and_role_length_boundary(self):
        self.add_project()
        role = "a" + "1" * 31
        staging = self.config["projects"]["other"]["staging"]
        staging["roles"] = [role]
        staging["session_cmd"] = '{pm} "login" --state "{out}" --actor "{role}"'
        self.config["hosts"]["desktop"]["repos"]["other"]["pnpm_cmd"] = '"other-helper" "--quiet"'
        self.write_config()
        self.run_login("--repo", "other", role)
        out = self.base / "home/.cache/other-stg" / (role + ".json")
        self.assert_session(out, ["--quiet", "login", "--state", str(out), "--actor", role], "other-helper")

    def test_legacy_directory_override_clear_and_modes(self):
        override = self.base / "override"
        override.mkdir(mode=0o755)
        self.env["APP_STAGING_SESSION_DIR"] = str(override)
        self.fixture["loose_mode"] = True
        self.run_login("admin")
        out = override / "admin.json"
        self.assert_session(out, ["--silent", "session:create", "--role", "admin", "--out", str(out)])
        (override / "keep.txt").write_text("keep")
        result = self.run_login("--clear")
        self.assertEqual(result.stdout, "cleared: " + str(override) + "\n")
        self.assertFalse(out.exists())
        self.assertTrue((override / "keep.txt").exists())
        self.assertEqual(self.named("pass") + self.named("security") + self.named("pnpm"), [])
        self.env.pop("APP_STAGING_SESSION_DIR")
        self.run_login("admin")
        self.add_project()
        self.run_login("--repo", "other", "auditor")
        self.run_login("--repo", "other", "--clear")
        self.assertFalse((self.base / "home/.cache/other-stg/auditor.json").exists())
        self.assertTrue((self.base / "home/.cache/app-staging/admin.json").exists())

    def test_invalid_roles_and_cli_before_credentials(self):
        for args in (("Admin",), ("unknown",), ("../admin",), ("admin;id",),
                     ("a" * 33,), ("admin\n",), ("--repo",), ("--worktree",), (),
                     ("--clear", "admin"), ("admin", "--worktree"), ("--repo", "unknown", "admin")):
            with self.subTest(args=args):
                self.run_login(*args, code=2)
                self.assertEqual(self.named("pass") + self.named("security") + self.named("pnpm"), [])
        (self.base / "checkout/package.json").unlink()
        self.run_login("admin", code=2)
        self.assertEqual(self.named("pass"), [])

    def test_invalid_templates_before_credentials(self):
        original = copy.deepcopy(self.config)
        bad = {"keychain_service": ["bad;{role}", "bad {role}", "$(id)", "{missing}", ""],
               "pass_entry": ["bad\n{role}", "bad`id`", "bad|{role}", "bad\\x"],
               "session_dir": ["~/.cache/bad dir", "~/.cache/$(id)", "~/.cache/bad\n"],
               "session_cmd": ["{pm} login --actor={role}", "{pm} login --out={out}",
                               "{pm} login; id", "{pm} $(id)", "{pm} {unknown}", "", "\"unterminated"],
               "roles": [["Admin"], ["a" * 33], ["admin", "bad/role"], "admin"]}
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.config = copy.deepcopy(original)
                    self.config["projects"]["example-app"]["staging"][key] = value
                    self.write_config()
                    self.run_login("admin", code=2)
                    self.assertEqual(self.named("pass") + self.named("security") + self.named("pnpm"), [])
        for command in ("pnpm;id", "pnpm $(id)", "", "pnpm --arg=unsafe", "-c pnpm"):
            self.config = copy.deepcopy(original)
            self.config["projects"]["example-app"]["staging"]["session_cmd"] = "{pm} --out {out}"
            self.config["hosts"]["desktop"]["repos"]["example-app"]["pnpm_cmd"] = command
            self.write_config()
            self.run_login("admin", code=2)
            self.assertEqual(self.named("pass"), [])

    def test_failure_codes_and_secret_output_suppression(self):
        for system in ("Linux", "Darwin"):
            self.env["FAKE_SYSTEM"] = system
            out = self.seed_session()
            self.fixture["store_fail"] = True
            self.run_login("admin", code=3)
            self.assertFalse(out.exists())
            self.fixture.pop("store_fail")
            for change in ({"helper_exit": 9}, {"no_session": True}, {"empty_session": True},
                           {"file_chmod_fail": True, "loose_mode": True}, {"email": ""}, {"secret": ""}):
                with self.subTest(system=system, change=change):
                    out = self.seed_session()
                    saved = copy.deepcopy(self.fixture)
                    self.fixture.update(change)
                    result = self.run_login("admin", code=3 if "email" in change or "secret" in change else 4)
                    self.assertFalse((self.base / "home/.cache/app-staging/admin.json").exists())
                    self.assertEqual(result.stdout, "")
                    self.fixture = saved

    def test_tracing_and_inherited_export_flags(self):
        self.env.update(secret="inherited", email="inherited", entry="inherited")
        self.run_login("admin", trace=True)
        # Parsers see stdin, never exported credentials; the helper gets stdin too.
        for call in self.calls:
            if call["name"] in ("pass", "security", "sed", "head", "pnpm"):
                self.assertEqual(call["credential_vars"], [], call["name"])

    def test_allexport_and_exported_shellopts(self):
        for system in ("Linux", "Darwin"):
            for shellopts in (False, True):
                with self.subTest(system=system, shellopts=shellopts):
                    self.env["FAKE_SYSTEM"] = system
                    self.env.update(secret="inherited", email="inherited", entry="inherited")
                    if shellopts:
                        self.env["SHELLOPTS"] = "allexport"
                    self.run_login("admin", allexport=True)
                    self.env.pop("SHELLOPTS", None)

    def test_allexport_refusal_if_disabling_is_ineffective(self):
        # Simulate a shell where the requested option change did not take.
        self.script.write_text(self.script.read_text().replace("set +a\n", ":\n", 1))
        result = self.run_login("admin", allexport=True, code=2)
        self.assertIn("allexport is still on", result.stderr)
        self.assertEqual(self.calls, [])

    def test_session_command_first_word(self):
        staging = self.config["projects"]["example-app"]["staging"]
        for first in ("tee", "cat", "/bin/cat", "-c", "../bin/pnpm", "bin/../../bin/pnpm", "bin/missing"):
            with self.subTest(first=first):
                staging["session_cmd"] = first + " --out {out}"
                self.write_config()
                old = self.seed_session()
                self.run_login("admin", code=2)
                self.assertFalse(old.exists())
                self.assertEqual(self.named("pass"), [])
        # A real executable in the selected worktree is accepted, independently
        # of PATH; a symlink that resolves outside it is rejected.
        bindir = self.base / "checkout/bin"
        bindir.mkdir()
        helper = bindir / "pnpm"
        shutil.copy2(self.base / "bin/pnpm", helper)
        for first in ("bin/pnpm", "./bin/pnpm"):
            staging["session_cmd"] = first + " --out {out} --role {role}"
            self.write_config()
            self.run_login("admin")
            out = self.base / "home/.cache/app-staging/admin.json"
            self.assert_session(out, ["--out", str(out), "--role", "admin"])
        helper.unlink()
        helper.symlink_to(self.base / "bin/pnpm")
        self.run_login("admin", code=2)
        self.assertEqual(self.named("pass"), [])

    def test_pm_uses_host_command_only_when_requested(self):
        staging = self.config["projects"]["example-app"]["staging"]
        staging["session_cmd"] = "{pm} --silent session:create --role {role} --out {out}"
        self.write_config()
        for system, helper, prefix in (("Linux", "pnpm", []), ("Darwin", "npx", ["-y", "pnpm@10.33.2"])):
            self.env["FAKE_SYSTEM"] = system
            self.run_login("admin")
            out = self.base / "home/.cache/app-staging/admin.json"
            self.assert_session(out, prefix + ["--silent", "session:create", "--role", "admin", "--out", str(out)], helper)
        staging["session_cmd"] = EXAMPLE["projects"]["example-app"]["staging"]["session_cmd"]
        self.config["hosts"]["mac"]["repos"]["example-app"]["pnpm_cmd"] = "npx;bad"
        self.write_config()
        self.run_login("admin")
        self.assertTrue(self.named("pnpm"))
        self.assertEqual(self.named("npx"), [])

    def test_directory_validation_for_config_and_both_overrides(self):
        checkout = self.base / "checkout"
        target = self.base / "outside"
        target.mkdir()
        link = self.base / "linked"
        link.symlink_to(target, target_is_directory=True)
        regular = self.base / "regular"
        regular.write_text("keep")
        protected = checkout / "package.json"
        invalid = (".", "..", "sessions", "/", "//", str(self.base / "home"), "~",
                   str(checkout), str(checkout / "sessions"), str(link), str(regular),
                   str(target) + "/../outside", str(target) + "/./sessions",
                   str(target) + "//sessions", "-dir", str(self.base / "space dir"))
        for source in ("config", "STAGING_SESSION_DIR", "APP_STAGING_SESSION_DIR"):
            for value in invalid:
                for args in (("admin",), ("--clear",)):
                    with self.subTest(source=source, value=value, args=args):
                        if source == "config":
                            self.config["projects"]["example-app"]["staging"]["session_dir"] = value
                            self.write_config()
                        else:
                            self.env[source] = value
                        result = self.run_login(*args, code=2)
                        if " " in value:
                            self.assertIn("spaces", result.stderr)
                        self.assertEqual(self.named("pass") + self.named("pnpm"), [])
                        self.assertTrue(protected.exists())
            self.env.pop(source, None)
            self.config = copy.deepcopy(EXAMPLE)
            self.write_config()
        # Detect containment through an ancestor symlink, too.
        alias = self.base / "checkout-alias"
        alias.symlink_to(checkout, target_is_directory=True)
        self.env["STAGING_SESSION_DIR"] = str(alias / "sessions")
        self.run_login("admin", code=2)

    def test_directory_owner(self):
        out = self.seed_session()
        # Synthetic foreign UID, portable even when tests run without chown.
        self.fixture["foreign_dir"] = True
        for args in (("admin",), ("--clear",)):
            result = self.run_login(*args, code=2)
            self.assertIn("owned by the current user", result.stderr)
            self.assertEqual(self.named("pass") + self.named("chmod"), [])
            self.assertTrue(out.exists())

    def test_absolute_override_with_different_worktree(self):
        checkout = self.base / "another-checkout"
        checkout.mkdir()
        (checkout / "package.json").write_text("{}")
        self.env["STAGING_SESSION_DIR"] = str(self.base / "sessions")
        self.run_login("admin", "--worktree", str(checkout))
        out = self.base / "sessions/admin.json"
        self.assert_session(out, ["--silent", "session:create", "--role", "admin", "--out", str(out)], worktree=checkout)

    def test_output_symlink_removed_without_touching_target(self):
        target = self.base / "protected.json"
        target.write_text("keep")
        target.chmod(0o644)
        out = self.seed_session()
        self.fixture["output_symlink"] = str(target)
        self.run_login("admin", code=4)
        self.assertFalse(out.is_symlink())
        self.assertFalse(out.exists())
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_nonregular_output_removed(self):
        out = self.seed_session()
        self.fixture["output_fifo"] = True
        self.run_login("admin", code=4)
        self.assertFalse(out.exists())
        self.assertFalse(any(argv[0] == "600" for argv in self.named("chmod")))

    def test_leading_options_in_templates(self):
        staging = self.config["projects"]["example-app"]["staging"]
        original = copy.deepcopy(staging)
        for key, value in (("pass_entry", "-c"), ("keychain_service", "-service"),
                           ("session_dir", "-sessions"), ("session_cmd", "-c --out {out}")):
            with self.subTest(key=key):
                self.config["projects"]["example-app"]["staging"] = dict(original, **{key: value})
                self.write_config()
                self.run_login("admin", code=2)
                self.assertEqual(self.named("pass") + self.named("security"), [])

    def test_setup_failures_before_credentials(self):
        for failure in ("mkdir_fail", "chmod_fail"):
            with self.subTest(failure=failure):
                old = self.seed_session()
                self.fixture[failure] = True
                result = self.run_login("admin", code=4)
                self.assertIn("cannot prepare session directory", result.stderr)
                self.assertFalse(old.exists())
                self.assertEqual(self.named("pass") + self.named("security") + self.named("pnpm"), [])
                self.fixture.pop(failure)

    def test_signal_cleanup_after_helper_writes(self):
        for signal, code in (("SIGINT", 130), ("SIGTERM", 143)):
            with self.subTest(signal=signal):
                self.fixture["signal"] = signal
                old = self.seed_session()
                self.run_login("admin", code=code)
                self.assertFalse(old.exists())

    def test_clear_only_regular_direct_json_and_partial_failure(self):
        out = self.seed_session()
        hidden = out.parent / ".hidden.json"
        hidden.write_text("session")
        nested = out.parent / "nested"
        nested.mkdir()
        nested_file = nested / "keep.json"
        nested_file.write_text("keep")
        target = self.base / "keep.json"
        target.write_text("keep")
        link = out.parent / "link.json"
        link.symlink_to(target)
        directory = out.parent / "directory.json"
        directory.mkdir()
        result = self.run_login("--clear", code=1)
        self.assertIn("failed to clear", result.stderr)
        self.assertNotIn("cleared:", result.stdout)
        self.assertFalse(out.exists())
        self.assertFalse(hidden.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(directory.is_dir())
        self.assertEqual(target.read_text(), "keep")
        self.assertTrue(nested_file.exists())
        link.unlink()
        directory.rmdir()
        self.seed_session()
        self.fixture["clear_fail"] = True
        self.run_login("--clear", code=1)
        self.assertTrue(out.exists())
        self.assertEqual(self.named("pass") + self.named("pnpm"), [])
        self.fixture.pop("clear_fail")
        self.fixture["clear_scan_fail"] = True
        self.run_login("--clear", code=1)
        self.assertTrue(out.exists())

    def test_old_state_removed_on_profile_and_worktree_failures(self):
        old = self.seed_session()
        self.config["projects"]["example-app"]["staging"]["pass_entry"] = "bad;entry"
        self.write_config()
        self.run_login("admin", code=2)
        self.assertFalse(old.exists())
        self.config = copy.deepcopy(EXAMPLE)
        self.write_config()
        self.seed_session()
        self.run_login("admin", "--worktree", str(self.base / "missing-checkout"), code=2)
        self.assertFalse(old.exists())

    def test_missing_fields_migration_and_clear_minimum(self):
        original = copy.deepcopy(self.config["projects"]["example-app"]["staging"])
        keys = ("roles", "keychain_service", "pass_entry", "session_dir", "session_cmd")
        for missing in ((key,) for key in keys):
            self.config["projects"]["example-app"]["staging"] = {k: v for k, v in original.items() if k not in missing}
            self.write_config()
            result = self.run_login("admin", code=2)
            self.assertIn("missing projects.<repo>.staging keys: " + missing[0], result.stderr)
            self.assertIn("docs/task-contract.md", result.stderr)
            self.assertEqual(self.named("pass"), [])
        self.config["projects"]["example-app"]["staging"] = {"worktree_dir": original["worktree_dir"]}
        self.write_config()
        result = self.run_login("admin", code=2)
        for key in keys:
            self.assertIn(key, result.stderr)
        self.run_login("--clear", code=2)
        # An override cannot silently supply a missing config field.
        self.env["STAGING_SESSION_DIR"] = str(self.base / "sessions")
        self.run_login("--clear", code=2)
        self.env.pop("STAGING_SESSION_DIR")
        self.config["projects"]["example-app"]["staging"] = {"session_dir": original["session_dir"]}
        self.write_config()
        old = self.seed_session()
        self.run_login("--clear")
        self.assertFalse(old.exists())

    def test_local_config_errors_do_not_fall_back(self):
        path = self.base / "config/hosts.local.json"
        path.write_text("{bad-json")
        self.run_login("admin", code=2)
        self.assertEqual(self.named("pass"), [])
        self.config.pop("default_repo")
        self.write_config()
        self.run_login("admin", code=2)
        self.run_login("--repo", "example-app", "admin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
