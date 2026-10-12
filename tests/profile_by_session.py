"""Agents sharing a directory: each gets its own session's profile when herdr gives
its session id (agent_profile.py; collector/profile_test.go mirrors this). Temp HOME."""
import json, os, sys, tempfile, time
HOME = tempfile.mkdtemp(prefix="prof-")
os.environ["HOME"] = HOME
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import agent_profile as P

def write(d, name, text, age=0):
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "w") as f:
        f.write(text)
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path

fails = []
def check(name, ok, got=None):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {got}"))
    ok or fails.append(name)

cwd = "/home/dev/projects/agent-routing"
pdir = os.path.join(HOME, ".claude", "projects", "-home-dev-projects-agent-routing")
mine, theirs = "cc82d044-a5ca-04bc-ca55-c63daf0d8560", "990a1023-e026-6cd6-d37e-1a3f437be670"
write(pdir, mine + ".jsonl", '{"type":"assistant","effort":"high","message":{"model":"claude-opus-5-5","content":[{"type":"text","text":"mine"}]}}\n', age=60)
write(os.path.join(pdir, mine, "subagents"), "agent-m1.meta.json", '{"agentType":"Explore","description":"mine","spawnDepth":1}')
write(os.path.join(pdir, mine, "subagents"), "agent-m1.jsonl", '{"type":"assistant","message":{"model":"claude-haiku-5-5"}}\n')
write(pdir, theirs + ".jsonl", '{"type":"assistant","effort":"low","message":{"model":"claude-sonnet-5-5","content":[{"type":"text","text":"theirs"}]}}\n')

got = P.claude(cwd, mine)
check("claude by session: own model, own subagents, not shared",
      got["model"] == "claude-opus-5-5" and got.get("by_session") and not got["shared"]
      and [s["description"] for s in got["subagents"]] == ["mine"], got)
got = P.claude(cwd)
check("claude without id: newest, shared", got["model"] == "claude-sonnet-5-5" and got["shared"] and "by_session" not in got, got)
check("glob in the id is ignored", "by_session" not in P.claude(cwd, "*"))
check("unknown id falls back to newest", P.claude(cwd, "0000dead-beef")["model"] == "claude-sonnet-5-5")
check("last_message by session", P.last_message("claude", cwd, session=mine)["text"] == "mine")
check("profiles() passes the session", P.profiles([{"kind": "claude", "cwd": cwd, "session": mine}])[0]["model"] == "claude-opus-5-5")

ccwd = "/work/example-app/.worktrees/APP-611"
d = os.path.join(HOME, ".codex", "sessions", "2026", "10", "09")
impl, gate = "c3221865-94a5-57f3-2f4a-6204eae22369", "fc1c9f38-f03b-2ed9-deeb-feb883a04e4b"
write(d, f"rollout-2026-10-09T11-36-27-{impl}.jsonl", '{"type":"session_meta","payload":{"cwd":"%s"}}\n'
      '{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"high"}}\n'
      '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"text":"impl done"}]}}\n' % ccwd, age=60)
write(d, f"rollout-2026-10-09T12-10-00-{gate}.jsonl", '{"type":"session_meta","payload":{"cwd":"%s"}}\n'
      '{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"medium"}}\n'
      '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"text":"gate done"}]}}\n' % ccwd)
got = P.codex(ccwd, impl)
check("codex by session", got["effort"] == "high" and got.get("by_session") and not got["shared"], got)
check("codex fallback: newest, shared", P.codex(ccwd)["effort"] == "medium" and P.codex(ccwd)["shared"])
check("codex last_message by session (impl, not the newer gate)",
      P.last_message("codex", ccwd, session=impl)["text"] == "impl done")
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
