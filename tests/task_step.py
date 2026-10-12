"""route.task_step: a worker's step from its name, ignoring herdr's numeric suffix."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R  # noqa: E402

cases = {"app451-gate-gate-2": "gate", "app451-gate-gate": "gate", "app599-gate-gate-12": "gate",
         "app130-mainmerge2-impl": "impl", "app611-staging-3": "staging", "app414-impl": "impl",
         "app414-review-2": "review", "w18": "impl"}
bad = {n: (R.task_step(n), w) for n, w in cases.items() if R.task_step(n) != w}
print("task_step ok" if not bad else f"task_step FAIL {bad}")
sys.exit(1 if bad else 0)
