#!/usr/bin/env bash
# Read-only host/repository preflight for agent routing.
#
# Runs on the host being checked. Run it remotely with:
#   ssh -o BatchMode=yes <alias> bash -s -- [args] < scripts/preflight.sh
#
# It never fetches, pulls, resets, cleans, checks out or writes to the repo
# (git runs with --no-optional-locks so even the index stat cache is untouched).
# Bash 3.2 compatible (macOS /bin/bash).
#
# Output: key=value lines, ending with preflight_status=ok|blocked.
# Exit:   0 ok, 20 blocked, 2 usage error.

set -u
# Ignore inherited git/cd redirection; no globbing of tool or path lists.
unset CDPATH GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CEILING_DIRECTORIES GIT_NAMESPACE
set -f

usage() {
  cat <<'USAGE'
Usage: preflight.sh --repo PATH [--expect-remote URL]
                    [--expect-commit SHA | --need-commit SHA]
                    [--tools "git pnpm ..."] [--herdr-bin PATH]
                    [--path-prepend "dir1:dir2"] [--require-clean]
                    [--ports-free "3010 5442 ..."] [--need-signing]

  --expect-commit  checkout HEAD must equal SHA (work runs in this checkout)
  --need-commit    SHA must exist locally (work runs in a new worktree at SHA)
  --behind-ref     with --need-commit: block if the commit doesn't contain REF (as of the last
                   fetch), e.g. origin/main for a readiness gate, which would stop anyway
  --behind-warn    with --behind-ref: report how far behind, don't block (the task opted in)
  --worktree-at    the task's worktree path: if it already exists, it must be at --need-commit
                   (a project helper won't move an existing branch, so a stale one would fail late)
USAGE
}

REPO="" EXPECT_REMOTE="" EXPECT_COMMIT="" NEED_COMMIT="" WORKTREE_AT="" BEHIND_REF="" BEHIND_WARN=0 TOOLS="git" HERDR_BIN="herdr"
PATH_PREPEND="" REQUIRE_CLEAN=0 PORTS_FREE="" NEED_SIGNING=0

need_val() { [ $# -ge 2 ] || { echo "error: $1 needs a value" >&2; usage >&2; exit 2; }; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo|--expect-remote|--expect-commit|--need-commit|--worktree-at|--behind-ref|--tools|--herdr-bin|--path-prepend|--ports-free)
      need_val "$@" ;;
  esac
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --expect-remote) EXPECT_REMOTE="${2:-}"; shift 2 ;;
    --expect-commit) EXPECT_COMMIT="${2:-}"; shift 2 ;;
    --need-commit) NEED_COMMIT="${2:-}"; shift 2 ;;
    --worktree-at) WORKTREE_AT="${2:-}"; shift 2 ;;
    --behind-ref) BEHIND_REF="${2:-}"; shift 2 ;;
    --behind-warn) BEHIND_WARN=1; shift ;;
    --tools) TOOLS="${2:-}"; shift 2 ;;
    --herdr-bin) HERDR_BIN="${2:-}"; shift 2 ;;
    --path-prepend) PATH_PREPEND="${2:-}"; shift 2 ;;
    --require-clean) REQUIRE_CLEAN=1; shift ;;
    --need-signing) NEED_SIGNING=1; shift ;;
    --ports-free) PORTS_FREE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[ -n "$REPO" ] || { echo "error: --repo is required" >&2; usage >&2; exit 2; }

# Non-interactive SSH shells do not read interactive rc files, so tools in
# ~/.local/bin, Homebrew or nvm may be missing from PATH. Prepend explicitly.
expand_home() {
  case "$1" in
    '$HOME'*) printf '%s' "$HOME${1#\$HOME}" ;;
    '~'|'~/'*) printf '%s' "$HOME${1#\~}" ;;
    *) printf '%s' "$1" ;;
  esac
}
if [ -n "$PATH_PREPEND" ]; then
  # Keep the given order: the first entry wins.
  prefix=""
  OLD_IFS="$IFS"; IFS=':'
  for d in $PATH_PREPEND; do prefix="${prefix:+$prefix:}$(expand_home "$d")"; done
  IFS="$OLD_IFS"
  PATH="$prefix:$PATH"
  export PATH
fi
HERDR_BIN="$(expand_home "$HERDR_BIN")"

BLOCKERS=""
block() { BLOCKERS="${BLOCKERS:+$BLOCKERS; }$1"; }
kv() { printf '%s=%s\n' "$1" "$2"; }
# No index refresh, no fsmonitor daemon, no lazy fetch in partial clones.
export GIT_NO_LAZY_FETCH=1
G() { git --no-optional-locks -c core.fsmonitor=false -c core.untrackedCache=false -C "$REPO" "$@"; }

kv host "$(hostname 2>/dev/null || uname -n)"
kv os "$(uname -s) $(uname -m)"
kv kernel "$(uname -r)"   # WSL kernels say microsoft: the brief adds the host-fault rules
kv home "$HOME"   # where the task's durable report directory goes (ROUTE_REPORT_DIR)
# Which boot this is: routing compares it later, so a task dispatched before a restart shows as interrupted.
# On WSL a distro restart keeps the VM's kernel boot id, so PID 1's start time is part of it.
if [ -r /proc/sys/kernel/random/boot_id ]; then
  kv boot_id "$(cat /proc/sys/kernel/random/boot_id)/$(sed 's/.*) //' /proc/1/stat 2>/dev/null | cut -d' ' -f20)"
else
  kv boot_id "$(sysctl -n kern.bootsessionuuid 2>/dev/null || echo unknown)"
fi
kv checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Tools
for t in $TOOLS; do
  p="$(command -v "$t" 2>/dev/null || true)"
  if [ -n "$p" ]; then kv "tool_$t" "$p"; else kv "tool_$t" "MISSING"; block "tool missing: $t"; fi
done
# Bare pnpm version, without letting corepack or pnpm download another build.
if command -v pnpm >/dev/null 2>&1; then
  kv pnpm_version "$(cd / && COREPACK_ENABLE_NETWORK=0 npm_config_manage_package_manager_versions=false pnpm --version 2>/dev/null | head -1)"
fi

# Herdr
if [ -x "$HERDR_BIN" ] || command -v "$HERDR_BIN" >/dev/null 2>&1; then
  kv herdr_version "$("$HERDR_BIN" --version 2>/dev/null | head -1)"
  st="$("$HERDR_BIN" status server 2>/dev/null || "$HERDR_BIN" status 2>/dev/null)"
  srv="$(printf '%s\n' "$st" | sed -n 's/^ *status: *//p' | head -1)"
  kv herdr_server "${srv:-unknown}"
  [ "${srv:-}" = "running" ] || block "herdr server not running"
else
  kv herdr_version "MISSING"; block "herdr not found at $HERDR_BIN"
fi

# Ports a task will publish must not already be listening on this host.
if [ -n "$PORTS_FREE" ]; then
  lister=""
  if command -v ss >/dev/null 2>&1 && raw="$(ss -ltnH 2>/dev/null)"; then
    lister=ss; listening="$(printf '%s\n' "$raw" | awk '{print $4}' | sed 's/.*://')"
  elif command -v lsof >/dev/null 2>&1 && raw="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null; true)"; then
    # Without sudo, macOS lsof lists only this user's listeners (good enough for our own stacks).
    lister=lsof; listening="$(printf '%s\n' "$raw" | awk 'NR>1{print $9}' | sed 's/.*://')"
  fi
  [ -n "$lister" ] || block "cannot list listening ports (no ss or lsof); port check not done"
  kv ports_lister "${lister:-none}"
  busy=""
  for port in $PORTS_FREE; do
    case "$port" in *[!0-9]*|'') echo "error: bad port $port" >&2; exit 2 ;; esac
    if printf '%s\n' "$listening" | grep -qx "$port"; then busy="${busy:+$busy }$port"; fi
  done
  kv ports_checked "$PORTS_FREE"
  if [ -n "$busy" ]; then kv ports_busy "$busy"; block "ports already listening: $busy"; else kv ports_busy none; fi
fi

# Repository identity
if [ ! -d "$REPO" ]; then
  kv repo_present no; block "repo path missing: $REPO"
elif ! G rev-parse --git-dir >/dev/null 2>&1; then
  kv repo_present no; block "not a git repository: $REPO"
else
  kv repo_present yes
  kv repo_toplevel "$(G rev-parse --show-toplevel)"
  # The project's own pnpm pin (packageManager), so routing needn't copy it.
  pin="$(sed -n 's/.*"packageManager": *"pnpm@\([^"+]*\).*/\1/p' "$REPO/package.json" 2>/dev/null | head -1)"
  [ -n "$pin" ] && kv repo_pnpm_pin "$pin"
  remote="$(G remote get-url origin 2>/dev/null || echo NONE)"
  kv repo_remote "$remote"
  if [ -n "$EXPECT_REMOTE" ] && [ "$remote" != "$EXPECT_REMOTE" ]; then
    block "remote mismatch: got $remote want $EXPECT_REMOTE"
  fi
  head="$(G rev-parse HEAD 2>/dev/null || echo NONE)"
  kv repo_branch "$(G symbolic-ref --short -q HEAD || echo DETACHED)"
  kv repo_head "$head"
  # Ahead/behind is relative to the remote-tracking ref as of the LAST fetch.
  ab="$(G rev-list --left-right --count '@{u}...HEAD' 2>/dev/null | tr '\t' ' ')"
  kv repo_behind_ahead_last_fetch "${ab:-no-upstream}"
  # FETCH_HEAD lives in the common dir (shared by linked worktrees).
  cd_="$(G rev-parse --git-common-dir)"
  case "$cd_" in /*) ;; *) cd_="$(cd "$REPO" && cd "$cd_" && pwd)" ;; esac
  fh="$cd_/FETCH_HEAD"
  kv repo_last_fetch "$(stat -c %y "$fh" 2>/dev/null || stat -f %Sm "$fh" 2>/dev/null || echo no-fetch-recorded)"
  # GIT_NO_LAZY_FETCH needs git >= 2.44; older git in a partial clone may fetch on cat-file.
  LAZY_RISK=0
  if [ -n "$(G config --get extensions.partialClone 2>/dev/null)" ]; then
    kv repo_partial_clone yes
    gv="$(git --version | awk '{print $3}')"
    maj="${gv%%.*}"; rest="${gv#*.}"; min="${rest%%.*}"
    if [ "${maj:-0}" -lt 2 ] 2>/dev/null || { [ "${maj:-0}" -eq 2 ] && [ "${min:-0}" -lt 44 ]; } 2>/dev/null; then LAZY_RISK=1; fi
  fi

  porcelain="$(G status --porcelain=v1 --untracked-files=normal 2>/dev/null)"
  tracked="$(printf '%s\n' "$porcelain" | grep -c '^[^?]' || true)"
  untracked="$(printf '%s\n' "$porcelain" | grep -c '^??' || true)"
  kv repo_dirty_tracked "$tracked"
  kv repo_untracked "$untracked"
  if [ "$REQUIRE_CLEAN" = 1 ] && [ $((tracked + untracked)) -gt 0 ]; then
    block "checkout not clean ($tracked tracked, $untracked untracked); not cleaning automatically"
  fi
  kv repo_worktrees "$(G worktree list --porcelain 2>/dev/null | grep -c '^worktree ')"

  if [ -n "$EXPECT_COMMIT" ]; then
    if [ "$head" = "$EXPECT_COMMIT" ]; then
      kv commit_match yes
    else
      kv commit_match no
      if [ "$LAZY_RISK" = 1 ]; then
        kv expected_commit_available_locally unknown
        block "HEAD $head != required $EXPECT_COMMIT (partial clone on git < 2.44: not probing, it could lazy-fetch)"
      elif G cat-file -e "${EXPECT_COMMIT}^{commit}" 2>/dev/null; then
        kv expected_commit_available_locally yes
        block "HEAD $head != required $EXPECT_COMMIT (commit exists locally; a worktree at it is possible without fetching)"
      else
        kv expected_commit_available_locally no
        block "HEAD $head != required $EXPECT_COMMIT (commit not present locally; fetch first)"
      fi
    fi
  fi

  # Tasks that will commit: if the repo requires signed commits, the signing
  # key must already be unlocked (workers can't answer a passphrase prompt).
  if [ "$NEED_SIGNING" = 1 ]; then
    if [ "$(G config --get commit.gpgsign 2>/dev/null)" != "true" ]; then
      kv commit_signing not-required
    elif [ "$(G config --get gpg.format 2>/dev/null || echo openpgp)" != "openpgp" ]; then
      kv commit_signing "$(G config --get gpg.format) (not checked)"
    elif ! command -v gpg >/dev/null 2>&1; then
      kv commit_signing gpg-missing; block "commits must be signed but gpg is not installed"
    elif grep -qs '^pinentry-program.*pinentry-mac' "$HOME/.gnupg/gpg-agent.conf"; then
      # A configured pinentry-mac helper may retrieve a Keychain passphrase;
      # a no-pinentry check cannot verify that configuration.
      kv commit_signing "keychain (pinentry-mac)"
    elif echo test | { if command -v timeout >/dev/null 2>&1; then timeout 10 gpg --batch --pinentry-mode error --clearsign; else gpg --batch --pinentry-mode error --clearsign; fi; } >/dev/null 2>&1; then
      kv commit_signing unlocked
    else
      kv commit_signing locked
      block "GPG is locked, so the worker's commits would fail: ask the human to run, in their own terminal: echo test | gpg --clearsign >/dev/null"
    fi
  fi

  if [ -n "$NEED_COMMIT" ]; then
    if [ "$LAZY_RISK" = 1 ]; then
      kv need_commit_available_locally unknown
      block "partial clone on git < 2.44: not probing $NEED_COMMIT (it could lazy-fetch); check by hand"
    elif G cat-file -e "${NEED_COMMIT}^{commit}" 2>/dev/null; then
      kv need_commit_available_locally yes
    else
      kv need_commit_available_locally no
      block "required commit $NEED_COMMIT not present locally; run git fetch first (preflight never fetches)"
    fi
  fi
fi

if [ -n "$BEHIND_REF" ] && [ -n "$NEED_COMMIT" ] && [ "$LAZY_RISK" != 1 ]; then
  behind="$(G rev-list --count "${NEED_COMMIT}..${BEHIND_REF}" 2>/dev/null || echo unknown)"
  kv commit_behind_ref "$BEHIND_REF $behind"
  if [ "$BEHIND_WARN" = 1 ]; then
    kv commit_behind_allowed yes      # the task opted in (allow_behind): reported, not blocked
  elif [ "$behind" != "unknown" ] && [ "$behind" -gt 0 ] 2>/dev/null; then
    block "$NEED_COMMIT is $behind commits behind $BEHIND_REF (last fetch): the gate would stop; bring the branch up to date in a fix round first"
  fi
fi

if [ -n "$WORKTREE_AT" ] && [ -n "$NEED_COMMIT" ]; then
  if [ -e "$WORKTREE_AT/.git" ]; then
    wt_head="$(git --no-optional-locks -C "$WORKTREE_AT" -c core.fsmonitor=false rev-parse HEAD 2>/dev/null || true)"
    kv existing_worktree_head "${wt_head:-unreadable}"
    if [ "$wt_head" != "$NEED_COMMIT" ]; then
      wt_dirty="$(git --no-optional-locks -C "$WORKTREE_AT" -c core.fsmonitor=false status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
      block "existing worktree $WORKTREE_AT is at ${wt_head:-?}, not $NEED_COMMIT (${wt_dirty} changed paths); the worktree helper won't move an existing branch: bring that worktree to the commit, or route with --commit ${wt_head:-<its HEAD>}"
    fi
  else
    kv existing_worktree_head none
  fi
fi

if [ -n "$BLOCKERS" ]; then
  kv blockers "$BLOCKERS"
  kv preflight_status blocked
  exit 20
fi
kv preflight_status ok
exit 0
