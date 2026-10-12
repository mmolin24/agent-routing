"""Memory pressure in the load grade: macOS's own level and Linux PSI
map to normal/warn/critical; warn grades the machine warn, critical hot. Swap size alone
still only warns. Mirrors collector/grade_test.go."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import route as R
import host_load as H
G = 2**30
fails = []
def check(name, ok, got=None):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"  got {got}"))
    ok or fails.append(name)
mac = {"cores": 15, "load1": 2.0, "mem_total": 24 * G, "mem_avail": 10 * G, "swap_total": 12 * G, "swap_used": int(11.7 * G)}
lv = R.load_levels({**mac, "pressure": "warn"})
check("warn pressure: mem warn, overall warn", lv["mem"] == "warn" and lv["overall"] == "warn" and lv["hot"] == [], lv)
lv = R.load_levels({**mac, "pressure": "critical"})
check("critical pressure: hot, with a reason", lv["mem"] == "hot" and lv["overall"] == "hot"
      and lv["hot"] == ["memory pressure critical"], lv)
lv = R.load_levels({**mac, "pressure": "normal"})
check("normal pressure: 97% swap still only warns", lv["mem"] == "ok" and lv["swap"] == "warn" and lv["overall"] == "warn", lv)
d = tempfile.mkdtemp()
for name, body, want in (("calm", "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n", "normal"),
                         ("some", "some avg10=30.00 avg60=12.50 avg300=3.00 total=1\nfull avg10=2.00 avg60=1.00 avg300=0.10 total=1\n", "warn"),
                         ("full", "some avg10=60.00 avg60=40.00 avg300=9.00 total=1\nfull avg10=20.00 avg60=11.00 avg300=2.00 total=1\n", "critical"),
                         ("garbage", "nonsense\n", None)):
    p = os.path.join(d, name); open(p, "w").write(body)
    check(f"PSI {name} -> {want}", H.psi_pressure(p) == want, H.psi_pressure(p))
check("PSI missing -> None", H.psi_pressure(os.path.join(d, "missing")) is None)
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
