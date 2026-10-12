"""The herdr prefix printed for humans and orchestrators uses herdr's own machine
selector (case-sensitive profile label), never the routing host name. No live systems."""
import copy, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R

cfg = R.load_json(os.path.join(R.ROOT, "tests", "fixtures", "hosts.golden.json"))
fails = []
def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}: {got!r}")
    ok or fails.append(name)

check("desktop → mac uses the 'Mac' profile label", R.herdr_prefix(cfg, "mac", "desktop"), "herdr --machine Mac")
check("mac → desktop uses 'Desktop WSL', quoted", R.herdr_prefix(cfg, "desktop", "mac"), "herdr --machine 'Desktop WSL'")
check("local is bare herdr", R.herdr_prefix(cfg, "desktop", "desktop"), "herdr")
check("prefix matches rendered commands", R.herdr_cmd(cfg, "mac", "desktop", "agent", "get", "x"),
      R.herdr_prefix(cfg, "mac", "desktop") + " agent get x")
ssh = copy.deepcopy(cfg)
ssh["hosts"]["mac"]["reach"]["desktop"] = {"mode": "ssh", "ssh_alias": "dev-mac"}
check("ssh uses the full herdr path, expanded on the far side", R.herdr_prefix(ssh, "mac", "desktop"),
      "ssh -o BatchMode=yes dev-mac '$HOME/.local/bin/herdr'")
check("every host listed from the desktop", sorted(R.herdr_prefixes(cfg, "desktop")), ["desktop", "mac"])
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
