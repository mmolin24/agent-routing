package collector

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/go-cmp/cmp"
)

// writeFile creates dir/name with content and returns its path.
func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("MkdirAll(%q): %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("WriteFile(%q): %v", path, err)
	}
	return path
}

func TestReverseLines(t *testing.T) {
	// Lines long enough that the file spans several 64 KB chunks, so lines
	// cut across chunk boundaries must be reassembled.
	var lines []string
	for i := range 300 {
		lines = append(lines, fmt.Sprintf("line-%03d-%s", i, strings.Repeat("x", 700)))
	}
	path := writeFile(t, t.TempDir(), "s.jsonl", strings.Join(lines, "\n")+"\n\n")

	tests := []struct {
		name  string
		limit int64
		stop  int // stop after this many lines (0 = never)
		want  []string
	}{
		{name: "all lines newest first", limit: 1 << 30, want: reversed(lines)},
		{name: "stops when told", limit: 1 << 30, stop: 3, want: reversed(lines)[:3]},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var got []string
			reverseLines(path, tt.limit, func(line string) bool {
				got = append(got, line)
				return tt.stop == 0 || len(got) < tt.stop
			})
			if diff := cmp.Diff(tt.want, got); diff != "" {
				t.Errorf("reverseLines() mismatch (-want +got):\n%s", diff)
			}
		})
	}

	t.Run("respects the read limit", func(t *testing.T) {
		var n int
		reverseLines(path, readChunk, func(string) bool { n++; return true })
		if n == 0 || n >= len(lines) {
			t.Errorf("reverseLines() with a one-chunk limit read %d lines, want between 1 and %d", n, len(lines)-1)
		}
	})
}

func reversed(s []string) []string {
	out := make([]string, len(s))
	for i, v := range s {
		out[len(s)-1-i] = v
	}
	return out
}

func TestQueueIDs(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir, "1700000000000000002-APP637-Gate.json", "{}")
	writeFile(t, dir, "1700000000000000001-app600-gate.json", "{}")
	got := queueIDs(dir)
	want := []string{"app600-gate", "app637-gate"}
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("queueIDs() mismatch (-want +got):\n%s", diff)
	}
	if got := queueIDs(filepath.Join(dir, "missing")); len(got) != 0 {
		t.Errorf("queueIDs(missing dir) = %v, want empty", got)
	}
}
