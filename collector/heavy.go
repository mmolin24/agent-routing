package collector

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
)

// HeavyRun is one command holding or waiting for a heavy slot (scripts/heavy).
type HeavyRun struct {
	PID   int     `json:"pid"`
	Task  string  `json:"task,omitempty"`
	Cmd   string  `json:"cmd"`
	Since float64 `json:"since"`
}

// HeavySlots is who holds this machine's heavy slots and who waits, oldest first.
type HeavySlots struct {
	Holders []HeavyRun `json:"holders"`
	Waiting []HeavyRun `json:"waiting"`
}

// heavyDir is where scripts/heavy keeps its slots (RT_HEAVY_DIR overrides).
func heavyDir() string {
	if d := os.Getenv("RT_HEAVY_DIR"); d != "" {
		return expandHome(d)
	}
	return expandHome("~/.cache/agent-routing/heavy")
}

// ReadHeavySlots reads dir. Entries whose process is gone are skipped: the
// slot's file lock was released when it died, so it's free.
func ReadHeavySlots(dir string) HeavySlots {
	out := HeavySlots{Holders: []HeavyRun{}, Waiting: []HeavyRun{}}
	holders, _ := filepath.Glob(filepath.Join(dir, "slot-*.json"))
	for _, p := range holders {
		if r, ok := readRun(p); ok {
			out.Holders = append(out.Holders, r)
		}
	}
	sort.Slice(out.Holders, func(i, j int) bool { return out.Holders[i].Since < out.Holders[j].Since })
	waiting, _ := filepath.Glob(filepath.Join(dir, "waiting", "*.json"))
	sort.Strings(waiting) // names start with a nanosecond timestamp
	for _, p := range waiting {
		if r, ok := readRun(p); ok {
			out.Waiting = append(out.Waiting, r)
		}
	}
	return out
}

func readRun(path string) (HeavyRun, bool) {
	b, err := os.ReadFile(path)
	if err != nil || strings.HasSuffix(path, ".tmp") {
		return HeavyRun{}, false
	}
	var r HeavyRun
	if json.Unmarshal(b, &r) != nil || r.PID <= 0 || syscall.Kill(r.PID, 0) != nil {
		return HeavyRun{}, false
	}
	return r, true
}
