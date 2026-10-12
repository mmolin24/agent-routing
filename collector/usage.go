package collector

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Window is one plan-usage window: how much of it is used, and when it resets.
type Window struct {
	UsedPercent   float64 `json:"used_percent"`
	ResetsAt      int64   `json:"resets_at,omitempty"`      // Unix seconds
	WindowMinutes int     `json:"window_minutes,omitempty"` // Codex only
}

// ToolUsage is a tool's subscription usage as last seen on this machine.
type ToolUsage struct {
	At        float64 `json:"at"`             // Unix seconds the reading was taken
	Plan      string  `json:"plan,omitempty"` // Codex: plan_type
	FiveHour  *Window `json:"five_hour,omitempty"`
	Week      *Window `json:"week,omitempty"`
	Credits   string  `json:"credits,omitempty"` // Codex: credit balance
	Exhausted string  `json:"exhausted,omitempty"`
}

// Usage holds the per-tool readings this machine has.
type Usage struct {
	Codex  *ToolUsage `json:"codex,omitempty"`
	Claude *ToolUsage `json:"claude,omitempty"`
}

// ReadUsage reads Codex's latest rate_limits from its newest session files and
// Claude's from the file its status line writes (scripts/claude-statusline.sh).
func ReadUsage() Usage {
	return Usage{Codex: readCodexUsage(filepath.Join(home(), ".codex", "sessions")),
		Claude: readClaudeUsage(filepath.Join(home(), ".claude", "usage", "rate_limits.json"))}
}

func readCodexUsage(root string) *ToolUsage {
	days := glob(filepath.Join(root, "*", "*", "*")) // YYYY/MM/DD
	sort.Sort(sort.Reverse(sort.StringSlice(days)))
	if len(days) > 2 {
		days = days[:2]
	}
	var files []string
	for _, d := range days {
		files = append(files, glob(filepath.Join(d, "rollout-*.jsonl"))...)
	}
	for _, f := range newestFirst(files, 6) {
		if u := codexUsageIn(f); u != nil {
			return u
		}
	}
	return nil
}

// codexUsageIn finds the last token_count event with rate limits in a rollout.
func codexUsageIn(path string) *ToolUsage {
	var out *ToolUsage
	reverseLines(path, 2_000_000, func(line string) bool {
		if !strings.Contains(line, `"rate_limits"`) {
			return true
		}
		var ev struct {
			Timestamp string `json:"timestamp"`
			Payload   struct {
				RateLimits *struct {
					Primary   *codexWindow `json:"primary"`
					Secondary *codexWindow `json:"secondary"`
					Credits   *struct {
						Balance string `json:"balance"`
					} `json:"credits"`
					PlanType    string `json:"plan_type"`
					ReachedType string `json:"rate_limit_reached_type"`
				} `json:"rate_limits"`
			} `json:"payload"`
		}
		if json.Unmarshal([]byte(line), &ev) != nil || ev.Payload.RateLimits == nil {
			return true
		}
		rl := ev.Payload.RateLimits
		u := &ToolUsage{Plan: rl.PlanType, Exhausted: rl.ReachedType}
		if t, err := time.Parse(time.RFC3339Nano, ev.Timestamp); err == nil {
			u.At = float64(t.UnixNano()) / 1e9
		}
		for _, w := range []*codexWindow{rl.Primary, rl.Secondary} {
			if w == nil {
				continue
			}
			win := &Window{UsedPercent: w.UsedPercent, ResetsAt: w.ResetsAt, WindowMinutes: w.WindowMinutes}
			if w.WindowMinutes > 0 && w.WindowMinutes <= 600 {
				u.FiveHour = win
			} else {
				u.Week = win
			}
		}
		if rl.Credits != nil && rl.Credits.Balance != "" {
			if v, err := strconv.ParseFloat(rl.Credits.Balance, 64); err == nil {
				u.Credits = strconv.FormatFloat(v, 'f', 0, 64)
			}
		}
		if u.FiveHour == nil && u.Week == nil {
			return true
		}
		out = u
		return false
	})
	return out
}

type codexWindow struct {
	UsedPercent   float64 `json:"used_percent"`
	WindowMinutes int     `json:"window_minutes"`
	ResetsAt      int64   `json:"resets_at"`
}

func readClaudeUsage(path string) *ToolUsage {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var f struct {
		CapturedAt float64 `json:"captured_at"`
		RateLimits struct {
			FiveHour *claudeWindow `json:"five_hour"`
			SevenDay *claudeWindow `json:"seven_day"`
		} `json:"rate_limits"`
	}
	if json.Unmarshal(b, &f) != nil {
		return nil
	}
	u := &ToolUsage{At: f.CapturedAt}
	now := time.Now().Unix()
	// A window past its reset time says nothing about now: leave it out, not 0%.
	if w := f.RateLimits.FiveHour; w != nil && (w.ResetsAt == 0 || w.ResetsAt > now) {
		u.FiveHour = &Window{UsedPercent: w.UsedPercentage, ResetsAt: w.ResetsAt}
	}
	if w := f.RateLimits.SevenDay; w != nil && (w.ResetsAt == 0 || w.ResetsAt > now) {
		u.Week = &Window{UsedPercent: w.UsedPercentage, ResetsAt: w.ResetsAt}
	}
	if u.FiveHour == nil && u.Week == nil {
		return nil
	}
	return u
}

type claudeWindow struct {
	UsedPercentage float64 `json:"used_percentage"`
	ResetsAt       int64   `json:"resets_at"`
}

// newestUsage keeps, per tool, the most recent reading across machines.
func newestUsage(all []Usage) Usage {
	var out Usage
	for _, u := range all {
		if u.Codex != nil && (out.Codex == nil || u.Codex.At > out.Codex.At) {
			out.Codex = u.Codex
		}
		if u.Claude != nil && (out.Claude == nil || u.Claude.At > out.Claude.At) {
			out.Claude = u.Claude
		}
	}
	return out
}
