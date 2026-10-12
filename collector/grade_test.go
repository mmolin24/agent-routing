package collector

import (
	"os"
	"testing"

	"github.com/google/go-cmp/cmp"
)

func ptr[T any](v T) *T { return &v }

func TestGrade(t *testing.T) {
	const gib = int64(1 << 30)
	tests := []struct {
		name   string
		load   *Load
		limits HotLimits
		want   Levels
	}{
		{
			name: "no reading",
			load: nil,
			want: Levels{Overall: Unknown, Hot: []string{}},
		},
		{
			name: "idle desktop",
			load: &Load{Cores: 16, Load1: ptr(0.1), MemTotal: ptr(16 * gib), MemAvail: ptr(13 * gib),
				SwapTotal: ptr(4 * gib), SwapUsed: ptr(int64(0)), Docker: ptr("up"), DockerLimit: ptr(15 * gib), DockerUsed: ptr(int64(0))},
			want: Levels{CPU: OK, Mem: OK, Swap: OK, Docker: OK, Overall: OK, Hot: []string{}},
		},
		{
			name: "swap alone only warns",
			load: &Load{Cores: 15, Load1: ptr(2.8), MemTotal: ptr(24 * gib), MemAvail: ptr(16 * gib),
				SwapTotal: ptr(11 * gib), SwapUsed: ptr(10 * gib)},
			want: Levels{CPU: OK, Mem: OK, Swap: Warn, Overall: Warn, Hot: []string{}},
		},
		{
			name: "overloaded cpu is hot",
			load: &Load{Cores: 15, Load1: ptr(52.0)},
			want: Levels{CPU: Hot, Overall: Hot, Hot: []string{"load 52.0 on 15 cores"}},
		},
		{
			name: "low memory is hot",
			load: &Load{Cores: 16, Load1: ptr(1.0), MemTotal: ptr(16 * gib), MemAvail: ptr(gib)},
			want: Levels{CPU: OK, Mem: Hot, Overall: Hot, Hot: []string{"only 6% memory available"}},
		},
		{
			name: "docker near its limit is hot",
			load: &Load{Cores: 16, Docker: ptr("up"), DockerLimit: ptr(8 * gib), DockerUsed: ptr(int64(7.5 * float64(gib)))},
			want: Levels{Docker: Hot, Overall: Hot, Hot: []string{"Docker at 94% of its memory limit"}},
		},
		{
			name:   "custom limits",
			load:   &Load{Cores: 10, Load1: ptr(6.0)},
			limits: HotLimits{LoadPerCore: 0.5},
			want:   Levels{CPU: Hot, Overall: Hot, Hot: []string{"load 6.0 on 10 cores"}},
		},
		{
			name: "macOS pressure warn grades the Mac warn (swap size aside)",
			load: &Load{Cores: 15, Load1: ptr(2.0), MemTotal: ptr(24 * gib), MemAvail: ptr(10 * gib),
				SwapTotal: ptr(12 * gib), SwapUsed: ptr(117 * gib / 10), Pressure: ptr("warn")},
			want: Levels{CPU: OK, Mem: Warn, Swap: Warn, Overall: Warn, Hot: []string{}},
		},
		{
			name: "critical pressure is hot",
			load: &Load{Cores: 15, Load1: ptr(2.0), MemTotal: ptr(24 * gib), MemAvail: ptr(10 * gib), Pressure: ptr("critical")},
			want: Levels{CPU: OK, Mem: Hot, Overall: Hot, Hot: []string{"memory pressure critical"}},
		},
		{
			name: "normal pressure changes nothing",
			load: &Load{Cores: 16, Load1: ptr(0.5), MemTotal: ptr(16 * gib), MemAvail: ptr(13 * gib), Pressure: ptr("normal")},
			want: Levels{CPU: OK, Mem: OK, Overall: OK, Hot: []string{}},
		},
		{
			name: "docker down is not graded",
			load: &Load{Cores: 4, Load1: ptr(0.5), Docker: ptr("down")},
			want: Levels{CPU: OK, Overall: OK, Hot: []string{}},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Grade(tt.load, tt.limits)
			if diff := cmp.Diff(tt.want, got); diff != "" {
				t.Errorf("Grade() mismatch (-want +got):\n%s", diff)
			}
		})
	}
}

func TestPSIPressure(t *testing.T) {
	dir := t.TempDir()
	for _, tt := range []struct{ name, body, want string }{
		{"calm", "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n", "normal"},
		{"some", "some avg10=30.00 avg60=12.50 avg300=3.00 total=1\nfull avg10=2.00 avg60=1.00 avg300=0.10 total=1\n", "warn"},
		{"full", "some avg10=60.00 avg60=40.00 avg300=9.00 total=1\nfull avg10=20.00 avg60=11.00 avg300=2.00 total=1\n", "critical"},
		{"garbage", "nonsense\n", ""},
	} {
		p := dir + "/" + tt.name
		if err := os.WriteFile(p, []byte(tt.body), 0o644); err != nil {
			t.Fatal(err)
		}
		if got := psiPressure(p); got != tt.want {
			t.Errorf("%s: psiPressure = %q, want %q", tt.name, got, tt.want)
		}
	}
	if got := psiPressure(dir + "/missing"); got != "" {
		t.Errorf("missing file: %q, want \"\"", got)
	}
}
