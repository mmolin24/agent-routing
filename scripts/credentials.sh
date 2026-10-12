#!/usr/bin/env bash
# Credentials: prepare a signed-in session for one test role from credentials
# stored on this machine, without the password ever reaching the agent that runs this.
#
#   scripts/credentials.sh [--repo NAME] <role> [--worktree DIR]
#   scripts/credentials.sh [--repo NAME] --clear     # delete this project's sessions
#
# The email and password come from this machine's own store and go to the
# project's session helper on stdin only (never argv, env, files or output):
#   macOS: Keychain item, configured service, account = the email
#          (needs the logged-in GUI session: run it in a Herdr pane, not over ssh)
#   Linux: configured pass entry: line 1 password, a "login: <email>" line
# projects.<repo>.staging supplies the roles, store names, session dir and command.
# projects.<repo>.staging.session_cmd runs with the credentials on stdin;
# the config is trusted code, like every other configured command.
# The project helper signs in and writes a session file (e.g. a Playwright storage
# state). This prints only the session file's path. The file is a credential: mode 600,
# never read or printed, removed with --clear when the review ends. Grant: staging-review.
# Never trace credential assignments or the stdin-only printf, even with bash -x.
set +x
# Disable inherited allexport before ANY assignment, including bash -a/SHELLOPTS.
set +a
case "$-" in
  *a*) echo "error: allexport is still on; refusing to handle credentials" >&2; exit 2 ;;
esac
unset email secret entry
set -euo pipefail
umask 077
usage() { echo "usage: $0 [--repo NAME] <role> [--worktree DIR] | [--repo NAME] --clear" >&2; exit 2; }
repo=""; role=""; clear=0; worktree="$PWD"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) [ "$#" -ge 2 ] && [ -n "$2" ] || usage; repo="$2"; shift 2 ;;
    --worktree) [ "$#" -ge 2 ] && [ -n "$2" ] || usage; worktree="$2"; shift 2 ;;
    --clear) [ "$clear" = 0 ] || usage; clear=1; shift ;;
    -*) usage ;;
    *) [ -z "$role" ] || usage; role="$1"; shift ;;
  esac
done
if [ "$clear" = 1 ]; then [ -z "$role" ] || usage; else [ -n "$role" ] || usage; fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config="$root/config/hosts.local.json"
[ -e "$config" ] || config="$root/config/hosts.example.json"
system="$(uname -s)"
# This reader sees configuration only, never credentials. Emit validated lines for
# Bash 3 (macOS); no eval, shell expansion, or command substitution of templates.
read_settings() {
python3 -c '
import json, os, re, shlex, stat, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(sys.argv[1])), "scripts"))
from host_identity import detect_host
def safe(value, options=False):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/@:-]+", value)
        or (not options and value.startswith("-"))):
        raise ValueError("unsafe staging template result (leading - and spaces are not supported, including in directory overrides)")
    return value
def required(staging, keys):
    missing = [k for k in keys if k not in staging]
    if missing:
        raise ValueError("missing projects.<repo>.staging keys: " + ", ".join(missing) + "; see docs/task-contract.md")
try:
    path, repo, role, system, clear, worktree, mode = sys.argv[1:]
    with open(path) as f:
        cfg = json.load(f)
    repo = repo or cfg["default_repo"]
    staging = cfg["projects"][repo]["staging"]
    keys = ["session_dir"] if clear == "1" else ["roles", "keychain_service", "pass_entry", "session_dir", "session_cmd"]
    # Resolve the directory separately so stale sessions can go before validation
    # of the rest of the profile (which may have changed since the last login).
    if mode == "config" or "session_dir" not in staging:
        required(staging, keys)
    raw_dir = os.environ.get("STAGING_SESSION_DIR", os.environ.get("APP_STAGING_SESSION_DIR", staging["session_dir"]))
    directory = safe(os.path.expanduser(raw_dir))
    resolved = os.path.realpath(directory)
    checkout = os.path.realpath(worktree)
    if (not os.path.isabs(directory) or os.path.normpath(directory) != directory
        or any(part in (".", "..") for part in directory.split("/"))
        or resolved == "/" or resolved == os.path.realpath(os.environ["HOME"])
        or os.path.commonpath([resolved, checkout]) == checkout):
        raise ValueError("session_dir must be absolute and normalized, outside the worktree, and neither / nor HOME")
    if os.path.lexists(directory):
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("session_dir must be a real directory owned by the current user (no symlinks)")
    if mode == "dir":
        print(directory)
        sys.exit(0)
    roles = staging["roles"]
    if (not isinstance(roles, list) or not roles or
        any(not isinstance(r, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", r) for r in roles) or
        not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", role) or role not in roles):
        raise ValueError("invalid staging role")
    service = safe(staging["keychain_service"].format(role=role))
    entry = safe(staging["pass_entry"].format(role=role))
    out = safe(directory + "/" + role + ".json")
    command = shlex.split(staging["session_cmd"])
    if not command or command[0].startswith("-"):
        raise ValueError("empty staging command or first word starts with -")
    first = command[0]
    if first not in ("pnpm", "{pm}"):
        safe(first)
        executable = os.path.realpath(os.path.join(checkout, first))
        if (os.path.isabs(first) or ".." in first.split("/")
            or os.path.commonpath([executable, checkout]) != checkout
            or not os.path.isfile(executable) or not os.access(executable, os.X_OK)):
            raise ValueError("session_cmd first word must be pnpm, {pm}, or an executable relative path inside the worktree (no ..)")
        command[0] = executable
    argv = []
    for index, arg in enumerate(command):
        if index == 0 and first not in ("pnpm", "{pm}"):
            # The validated relative executable may sit in a checkout whose
            # absolute path contains spaces; keep it one argv entry.
            argv.append(arg)
            continue
        if arg == "{pm}":
            host = detect_host(cfg, system=system)
            pm = shlex.split(cfg["hosts"][host]["repos"][repo].get("pnpm_cmd", "pnpm"))
            if not pm or pm[0].startswith("-"):
                raise ValueError("empty package manager command or first word starts with -")
            argv.extend(safe(a, options=True) for a in pm)
        elif arg == "{role}":
            argv.append(safe(role))
        elif arg == "{out}":
            argv.append(out)
        else:
            argv.append(safe(arg, options=True))
    print("\n".join([service, entry] + argv))
except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
    print("error: invalid staging config or role: " + str(exc) + " (check projects.<repo>.staging and hosts.<host>.repos.<repo>.pnpm_cmd)", file=sys.stderr)
    sys.exit(2)
' "$config" "$repo" "$role" "$system" "$clear" "$worktree" "$1"
}
DIR="$(read_settings dir)" || exit 2
if [ "$clear" = 1 ]; then
  # Only direct, regular *.json files are removed. Symlinks/directories remain
  # and count as failures, as do denied unlinks; never claim a partial clear.
  python3 -c '
import os, sys
try:
    directory = sys.argv[1]
    if os.path.exists(directory):
        with os.scandir(directory) as entries:
            files = [entry for entry in entries if entry.name.endswith(".json")]
        for entry in files:
            if entry.is_file(follow_symlinks=False):
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass
        with os.scandir(directory) as entries:
            if any(entry.name.endswith(".json") for entry in entries):
                raise OSError("session files remain")
except OSError:
    print("error: failed to clear all session files", file=sys.stderr)
    sys.exit(1)
' "$DIR" || exit 1
  echo "cleared: $DIR"
  exit 0
fi
# Validate the role as a basename before touching its previous session. Full
# profile validation follows deletion, so broken config cannot retain old state.
[[ "$role" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || { echo "error: invalid staging role" >&2; exit 2; }
out="$DIR/$role.json"
cleanup() {
  rc=$?
  trap - EXIT
  unset email secret entry
  if [ "$rc" -ne 0 ] && [ -d "$DIR" ] && [ ! -L "$DIR" ]; then
    rm -f "$out" 2>/dev/null || echo "error: failed to remove session: $out" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
rm -f "$out" || { echo "error: cannot remove previous session: $out" >&2; exit 4; }
settings="$(read_settings config)" || exit 2
values=()
while IFS= read -r value; do values+=("$value"); done <<< "$settings"
service="${values[0]}"; pass_entry="${values[1]}"; session_cmd=("${values[@]:2}")
[ -f "$worktree/package.json" ] || { echo "error: $worktree is not a project checkout (run from the issue worktree or pass --worktree)" >&2; exit 2; }
# Fail closed before the store is read, including a failed mkdir or chmod.
if ! mkdir -p "$DIR" || [ -L "$DIR" ] || [ ! -d "$DIR" ] || [ ! -O "$DIR" ] || ! chmod 700 "$DIR"; then
  echo "error: cannot prepare session directory: $DIR" >&2
  exit 4
fi

# Unset first: inherited exported variables must not turn these into environment
# variables in credential readers, parsers, or the session helper.
unset email secret entry
email="" ; secret=""
case "$system" in
  Darwin)
    email="$(security find-generic-password -s "$service" 2>/dev/null | sed -n 's/^ *"acct"<blob>="\(.*\)"$/\1/p')" || true
    secret="$(security find-generic-password -s "$service" -w 2>/dev/null)" || {
      echo "error: no Keychain item $service, or the Keychain is locked / unavailable over ssh (run in a Herdr pane)" >&2; exit 3; }
    ;;
  *)
    command -v pass >/dev/null || { echo "error: pass is not installed (see skills/agent-routing/SKILL.md, staging logins)" >&2; exit 3; }
    entry="$(pass show "$pass_entry" 2>/dev/null)" || {
      echo "error: no pass entry $pass_entry, or GPG is locked (echo test | gpg --clearsign >/dev/null)" >&2; exit 3; }
    secret="$(builtin printf '%s\n' "$entry" | sed -n '1p')"
    email="$(builtin printf '%s\n' "$entry" | sed -n 's/^login: *//p' | head -n1)"
    unset entry
    ;;
esac
[ -n "$email" ] && [ -n "$secret" ] || { echo "error: the stored entry for $role has no email or no password" >&2; exit 3; }

# stdin only: line 1 email, line 2 password. Output is discarded except the exit code,
# so nothing the helper prints (or fails to redact) reaches the caller's transcript.
if builtin printf '%s\n%s\n' "$email" "$secret" | (cd "$worktree" && exec "${session_cmd[@]}") >/dev/null 2>&1; then
  unset secret email
  # The expected basename is inside the validated directory. Reject directory
  # replacement and output symlinks before chmod can touch an outside target.
  [ ! -L "$DIR" ] && [ -d "$DIR" ] && [ -O "$DIR" ] &&
    [ ! -L "$out" ] && [ -f "$out" ] && [ -s "$out" ] && chmod 600 "$out" || {
      echo "error: the session helper wrote no regular nonempty session, or securing it failed" >&2; exit 4; }
  echo "session: $out"
  echo "use it as Playwright storageState; it's a credential: don't read or print it; remove with --clear"
else
  rc=$?
  unset secret email
  echo "error: sign-in failed for $role (helper exit $rc; output suppressed so nothing secret is echoed). Run it yourself to see why: missing session helper, wrong password, or staging down" >&2
  exit 4
fi
