"""host_load.orphans: dev servers reparented to init, same fixture as collector/orphans_test.go."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import host_load as H  # noqa: E402

ps = open(os.path.join(HERE, "..", "collector", "testdata", "ps_orphans.txt")).read()
got = H.orphans(ps, lambda pid: "/w/" + {500: "a", 600: "b"}.get(pid, ""))
want = [
    {"pid": 500, "age_s": 377484, "ports": [8851], "rss": (2560 + 1552 + 6464 + 90000) * 1024, "cwd": "/w/a", "cmd": "expo start"},
    {"pid": 600, "age_s": 4953, "ports": [5173], "rss": 3000 * 1024, "cwd": "/w/b", "cmd": "vite"},
    {"pid": 901, "age_s": 60, "ports": [9000], "rss": 1000 * 1024, "cwd": "/w/", "cmd": "expo start"},
]
ok = got == want and H.orphans("", None) == []
print("orphans ok" if ok else f"orphans FAIL\n got  {got}\n want {want}")
sys.exit(0 if ok else 1)
