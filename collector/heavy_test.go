package collector

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/google/go-cmp/cmp"
)

func TestReadHeavySlots(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "waiting"), 0o755); err != nil {
		t.Fatal(err)
	}
	write := func(name string, r HeavyRun) {
		b, _ := json.Marshal(r)
		if err := os.WriteFile(filepath.Join(dir, name), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	me := os.Getpid()
	write("slot-0.json", HeavyRun{PID: me, Task: "app1-gate", Cmd: "pnpm ready:check 1", Since: 20})
	write("slot-1.json", HeavyRun{PID: 999999999, Task: "dead", Cmd: "x", Since: 10}) // process gone: free
	write("waiting/200-1.json", HeavyRun{PID: me, Task: "app3-impl", Cmd: "pnpm typecheck", Since: 30})
	write("waiting/100-1.json", HeavyRun{PID: me, Task: "app2-impl", Cmd: "pnpm test", Since: 25})

	got := ReadHeavySlots(dir)
	want := HeavySlots{
		Holders: []HeavyRun{{PID: me, Task: "app1-gate", Cmd: "pnpm ready:check 1", Since: 20}},
		Waiting: []HeavyRun{{PID: me, Task: "app2-impl", Cmd: "pnpm test", Since: 25},
			{PID: me, Task: "app3-impl", Cmd: "pnpm typecheck", Since: 30}},
	}
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("ReadHeavySlots mismatch (-want +got):\n%s", diff)
	}
	if got := ReadHeavySlots(filepath.Join(dir, "missing")); len(got.Holders)+len(got.Waiting) != 0 {
		t.Errorf("missing dir = %+v, want empty", got)
	}
}
