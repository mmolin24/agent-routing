"""route.stale_base: an open PR stacked on a merged or closed PR's branch."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R  # noqa: E402

prs = {"feature/APP-1": {"number": 1, "state": "merged"}, "feature/APP-2": {"number": 2, "state": "open"},
       "feature/APP-3": {"number": 3, "state": "closed"}}
cases = [
    ({"number": 9, "state": "open", "base": "feature/APP-1"}, {"branch": "feature/APP-1", "number": 1, "state": "merged"}),
    ({"number": 9, "state": "open", "base": "feature/APP-3"}, {"branch": "feature/APP-3", "number": 3, "state": "closed"}),
    ({"number": 9, "state": "open", "base": "feature/APP-2"}, None),     # base still open
    ({"number": 9, "state": "open", "base": "main"}, None),
    ({"number": 9, "state": "open", "base": "feature/old"}, None),       # base PR not in the list: unknown
    ({"number": 9, "state": "merged", "base": "feature/APP-1"}, None),   # only open PRs
    ({"number": 9, "state": "open"}, None),                              # no base (old cache)
]
bad = [(pr, want, R.stale_base(pr, prs)) for pr, want in cases if R.stale_base(pr, prs) != want]
for b in bad:
    print("FAIL", b)
print("stale_base ok" if not bad else "stale_base FAIL")
sys.exit(1 if bad else 0)
