package collector

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
)

func TestCodexUsageIn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rollout-x.jsonl")
	lines := `{"timestamp":"2026-10-09T16:00:00Z","type":"event_msg","payload":{"type":"token_count","info":null,"rate_limits":{"primary":{"used_percent":80.0,"window_minutes":10080,"resets_at":100},"secondary":null}}}
{"timestamp":"2026-10-09T16:51:02.5Z","type":"event_msg","payload":{"type":"token_count","info":{},"rate_limits":{"primary":{"used_percent":85.0,"window_minutes":10080,"resets_at":1791949663},"secondary":{"used_percent":12.5,"window_minutes":300,"resets_at":1791580000},"credits":{"balance":"12345.67"},"plan_type":"pro","rate_limit_reached_type":null}}}
{"timestamp":"2026-10-09T16:52:00Z","type":"response_item","payload":{"type":"message"}}
`
	if err := os.WriteFile(path, []byte(lines), 0o644); err != nil {
		t.Fatal(err)
	}
	got := codexUsageIn(path)
	want := &ToolUsage{At: 1791564662.5, Plan: "pro", Credits: "12346",
		Week:     &Window{UsedPercent: 85, ResetsAt: 1791949663, WindowMinutes: 10080},
		FiveHour: &Window{UsedPercent: 12.5, ResetsAt: 1791580000, WindowMinutes: 300}}
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("codexUsageIn mismatch (-want +got):\n%s", diff)
	}
}

func TestReadClaudeUsage(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rate_limits.json")
	future, past := time.Now().Unix()+3600, time.Now().Unix()-60
	body := fmt.Sprintf(`{"captured_at":1791564780,"rate_limits":{"five_hour":{"used_percentage":23.5,"resets_at":%d},"seven_day":{"used_percentage":41.2,"resets_at":%d}}}`, past, future)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	got := readClaudeUsage(path)
	want := &ToolUsage{At: 1791564780, Week: &Window{UsedPercent: 41.2, ResetsAt: future}} // 5h window reset: dropped
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("readClaudeUsage mismatch (-want +got):\n%s", diff)
	}
	if readClaudeUsage(filepath.Join(t.TempDir(), "missing.json")) != nil {
		t.Error("missing file should give nil")
	}
}

func TestNewestUsage(t *testing.T) {
	old := &ToolUsage{At: 10, Week: &Window{UsedPercent: 1}}
	newer := &ToolUsage{At: 20, Week: &Window{UsedPercent: 2}}
	got := newestUsage([]Usage{{Codex: newer, Claude: old}, {Codex: old, Claude: newer}})
	if got.Codex != newer || got.Claude != newer {
		t.Errorf("newestUsage = %+v, want the At=20 reading for both", got)
	}
}
