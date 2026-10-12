"""Read each agent's live model, effort and fast mode from its own session files.

Runs on the machine where the agents run (locally, or piped over SSH), so it
uses only the standard library and works on Python 3.8+.

  python3 agent_profile.py '[{"kind": "codex", "cwd": "/path", "session": "<id>"}, ...]'
  -> JSON list of {"model", "effort", "fast", "source"} in the same order

Sources (what each CLI writes about itself):
  codex : ~/.codex/sessions/**/rollout-*.jsonl, newest whose session_meta.cwd
          matches; last turn_context.model / .effort; fast when the last
          service_tier is not default/standard/auto.
  claude: ~/.claude/projects/<cwd with / and . as ->/*.jsonl, newest; last
          assistant entry's message.model, effort, usage.speed == "fast".
When herdr reports the agent's session id (agent_list agent_session.value), that
session's own file is read (rollout-*-<id>.jsonl, <id>.jsonl) and the result is
marked "by_session": true. Without one, agents sharing a directory can't be told
apart: the newest session wins and the result is marked "shared": true.
"""

import glob
import json
import os
import re
import sys

TAIL = 1_500_000  # bytes read from the end of large session files
SESSION_RE = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f-]{7,63}")   # also keeps glob characters out


def reverse_lines(path, chunk=65536, limit=8_000_000):
    """Lines of a file from the end backwards, read in small chunks (newest first).
    Most answers sit in the last few KB, so this rarely reads more than one chunk."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, buf, read = f.tell(), b"", 0
        while pos > 0 and read < limit:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            read += step
            parts = buf.split(b"\n")
            buf = parts[0]                       # possibly partial; keep for the next chunk
            for ln in reversed(parts[1:]):
                if ln.strip():
                    yield ln.decode("utf-8", "replace")
        if pos == 0 and buf.strip():
            yield buf.decode("utf-8", "replace")


def tail_lines(path, size=TAIL):
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        f.seek(max(0, end - size))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    return lines[1:] if end > size else lines   # first line may be cut


def first_json(path):
    try:
        with open(path) as f:
            return json.loads(f.readline())
    except (OSError, ValueError):
        return {}


def session_file(kind, cwd, session):
    """The agent's own session file when its id is known and the file exists, else None."""
    if not session or not SESSION_RE.fullmatch(session):
        return None
    if kind == "codex":
        hits = glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*-" + session + ".jsonl"))
        return max(hits, key=os.path.getmtime) if hits else None
    path = os.path.join(claude_dir(cwd), session + ".jsonl")
    return path if os.path.isfile(path) else None


def claude_dir(cwd):
    return os.path.expanduser("~/.claude/projects/" + cwd.replace("/", "-").replace(".", "-"))


def codex_files(cwd, session=None):
    """Rollouts for this agent, best first: its own by session id, else every one whose
    cwd matches, newest first."""
    own = session_file("codex", cwd, session)
    if own:
        return [own], True
    files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:60]
    return [p for p in files if (first_json(p).get("payload") or {}).get("cwd") == cwd], False


def claude_files(cwd, session=None):
    own = session_file("claude", cwd, session)
    if own:
        return [own], True
    return sorted(glob.glob(os.path.join(claude_dir(cwd), "*.jsonl")), key=os.path.getmtime, reverse=True), False


def codex(cwd, session=None):
    hits, own = codex_files(cwd, session)
    if not hits:
        return None
    out = {"model": None, "effort": None, "fast": False, "source": hits[0], "shared": len(hits) > 1,
           "active_at": int(os.path.getmtime(hits[0]))}
    if own:
        out["by_session"] = True
    tier, have_ctx = None, False
    for ln in reverse_lines(hits[0]):              # newest first; stop once both are known
        if not have_ctx and '"turn_context"' in ln:
            try:
                p = (json.loads(ln).get("payload") or {})
                out["model"], out["effort"], have_ctx = p.get("model"), p.get("effort"), True
            except ValueError:
                pass
        if tier is None and '"service_tier"' in ln:
            try:
                tier = find_key(json.loads(ln), "service_tier")
            except ValueError:
                pass
        if have_ctx and tier is not None:
            break
    out["fast"] = bool(tier) and tier not in ("default", "standard", "auto", "flex")
    out["tier"] = tier
    return out


def last_message(kind, cwd, limit=6000, session=None):
    """The worker's last reply, from its own session file (not scraped terminal text):
    {"text", "source"} or None. Codex: its rollout (by session id, else the newest for
    cwd); Claude: its transcript (likewise)."""
    if kind == "codex":
        hits, _ = codex_files(cwd, session)
        for ln in reverse_lines(hits[0]) if hits else []:
            if '"assistant"' not in ln:
                continue
            try:
                p = json.loads(ln).get("payload") or {}
            except ValueError:
                continue
            if p.get("type") == "message" and p.get("role") == "assistant":
                text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
                if text.strip():
                    return {"text": text.strip()[-limit:], "source": hits[0]}
        return None
    files, _ = claude_files(cwd, session)
    for ln in reverse_lines(files[0]) if files else []:
        if '"assistant"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        if r.get("type") == "assistant":
            text = "\n".join(c.get("text", "") for c in m.get("content") or [] if isinstance(c, dict)
                             and c.get("type") == "text")
            if text.strip():
                return {"text": text.strip()[-limit:], "source": files[0]}
    return None


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for v in obj.values():
            hit = find_key(v, key)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_key(v, key)
            if hit:
                return hit
    return None


def claude(cwd, session=None):
    files, own = claude_files(cwd, session)
    if not files:
        return None
    out = {"model": None, "effort": None, "fast": False, "source": files[0], "shared": len(files) > 1,
           "active_at": int(os.path.getmtime(files[0]))}
    if own:
        out["by_session"] = True
    for ln in reverse_lines(files[0]):
        if '"assistant"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        if r.get("type") == "assistant" and m.get("model"):
            out["model"] = m["model"]
            out["effort"] = r.get("effort") or r.get("perTurnEffort")
            out["fast"] = (m.get("usage") or {}).get("speed") == "fast"
            break
    out["subagents"] = claude_subagents(files[0])
    return out


def claude_subagents(session_file, recent_s=1800, active_s=90, limit=12):
    """In-process subagents (Agent tool) of a Claude session, newest first.
    Claude Code writes <session>/subagents/agent-<id>.jsonl plus .meta.json
    (agentType, description, spawnDepth). Running = written in the last 90s."""
    import time
    sdir = os.path.join(session_file[:-len(".jsonl")], "subagents")
    now, out = time.time(), []
    metas = sorted(glob.glob(os.path.join(sdir, "*.meta.json")), key=os.path.getmtime, reverse=True)
    for mp in metas:
        tp = mp[:-len(".meta.json")] + ".jsonl"
        if not os.path.exists(tp) or now - os.path.getmtime(tp) > recent_s:
            continue
        try:
            meta = json.load(open(mp))
        except (OSError, ValueError):
            continue
        model = None
        for ln in reverse_lines(tp, limit=400_000):
            if '"model"' in ln:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                model = ((r.get("message") or {}) if isinstance(r.get("message"), dict) else {}).get("model")
                if model:
                    break
        out.append({"id": os.path.basename(tp)[len("agent-"):-len(".jsonl")], "description": meta.get("description"),
                    "type": meta.get("agentType"), "depth": meta.get("spawnDepth") or 1, "model": model,
                    "parent": meta.get("parentAgentId") or meta.get("parentId"),
                    "running": now - os.path.getmtime(tp) < active_s})
        if len(out) >= limit:
            break
    return out


def profiles(items):
    res = []
    for it in items:
        try:
            fn = {"codex": codex, "claude": claude}.get(it.get("kind"))
            res.append(fn(it["cwd"], it.get("session")) if fn and it.get("cwd") else None)
        except Exception as e:  # one unreadable session must not hide the rest
            res.append({"error": str(e)[:120]})
    return res


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--last-message":
        req = json.loads(sys.argv[2])
        print(json.dumps(last_message(req.get("kind"), req.get("cwd"), session=req.get("session"))))
    else:
        print(json.dumps(profiles(json.loads(sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()))))
