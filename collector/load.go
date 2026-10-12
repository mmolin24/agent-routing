package collector

import (
	"bufio"
	"context"
	"math"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Load is a machine's live load. Unknown values are null in JSON.
type Load struct {
	OS          string   `json:"os"`
	Cores       int      `json:"cores"`
	Load1       *float64 `json:"load1"`
	Load5       *float64 `json:"load5"`
	Load15      *float64 `json:"load15"`
	MemTotal    *int64   `json:"mem_total"`
	MemAvail    *int64   `json:"mem_avail"`
	SwapTotal   *int64   `json:"swap_total"`
	SwapUsed    *int64   `json:"swap_used"`
	Pressure    *string  `json:"pressure,omitempty"` // macOS only: normal, warn or critical
	Docker      *string  `json:"docker"`             // "up", "down", or null when not installed
	DockerLimit *int64   `json:"docker_limit,omitempty"`
	DockerUsed  *int64   `json:"docker_used,omitempty"`
	Containers  *int     `json:"containers,omitempty"`
	BootAt      *int64   `json:"boot_at"`   // epoch seconds; a change means every process died
	BootID      *string  `json:"boot_id"`   // changes on every restart and nothing else
	DiskPath    string   `json:"disk_path"` // the volume backing the machine's disks (WSL: /mnt/c)
	DiskFree    *int64   `json:"disk_free"`
	DiskTotal   *int64   `json:"disk_total"`
	Orphans     []Orphan `json:"orphans"`
}

// ReadLoad reads the local machine's load: /proc on Linux; sysctl and
// memory_pressure on macOS; `docker info`/`docker stats` for Docker. A source
// that can't be read leaves its fields null.
func ReadLoad(ctx context.Context) *Load {
	l := &Load{Cores: runtime.NumCPU()}
	switch runtime.GOOS {
	case "linux":
		l.OS = "Linux"
		l.readLinux()
	case "darwin":
		l.OS = "Darwin"
		l.readDarwin(ctx)
	default:
		l.OS = runtime.GOOS
	}
	l.readDocker(ctx)
	l.BootAt, l.BootID = bootAt(ctx), bootID(ctx)
	l.DiskPath = diskPath()
	if free, total, ok := statDisk(l.DiskPath); ok {
		l.DiskFree, l.DiskTotal = &free, &total
	}
	l.Orphans = readOrphans(ctx)
	return l
}

func (l *Load) setLoad(fields []string) {
	vals := make([]*float64, 3)
	for i := 0; i < 3 && i < len(fields); i++ {
		if f, err := strconv.ParseFloat(fields[i], 64); err == nil {
			r := math.Round(f*100) / 100
			vals[i] = &r
		}
	}
	l.Load1, l.Load5, l.Load15 = vals[0], vals[1], vals[2]
}

func (l *Load) readLinux() {
	if b, err := os.ReadFile("/proc/loadavg"); err == nil {
		l.setLoad(strings.Fields(string(b)))
	}
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return
	}
	defer f.Close()
	info := map[string]int64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		name, rest, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		if fields := strings.Fields(rest); len(fields) > 0 {
			if kb, err := strconv.ParseInt(fields[0], 10, 64); err == nil {
				info[name] = kb * 1024
			}
		}
	}
	total, avail := info["MemTotal"], info["MemAvailable"]
	swapTotal, swapUsed := info["SwapTotal"], info["SwapTotal"]-info["SwapFree"]
	l.MemTotal, l.MemAvail, l.SwapTotal, l.SwapUsed = &total, &avail, &swapTotal, &swapUsed
	if p := psiPressure("/proc/pressure/memory"); p != "" {
		l.Pressure = &p
	}
}

// psiPressure is Linux's memory pressure (PSI) as the Mac's levels: the share of the
// last minute tasks stalled on memory. full avg60 >= 10% critical, some avg60 >= 10%
// warn; "" when the kernel has no PSI.
func psiPressure(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	avg := map[string]float64{}
	for _, ln := range strings.Split(string(b), "\n") {
		f := strings.Fields(ln)
		if len(f) < 3 || !strings.HasPrefix(f[2], "avg60=") {
			continue
		}
		v, err := strconv.ParseFloat(strings.TrimPrefix(f[2], "avg60="), 64)
		if err != nil {
			return ""
		}
		avg[f[0]] = v
	}
	if len(avg) == 0 {
		return ""
	}
	switch {
	case avg["full"] >= psiPct:
		return "critical"
	case avg["some"] >= psiPct:
		return "warn"
	}
	return "normal"
}

const psiPct = 10.0

var (
	swapTotalRE = regexp.MustCompile(`total = ([\d.]+)M`)
	swapUsedRE  = regexp.MustCompile(`used = ([\d.]+)M`)
	freePctRE   = regexp.MustCompile(`free percentage:\s*(\d+)%`)
)

func (l *Load) readDarwin(ctx context.Context) {
	// vm.loadavg looks like "{ 2.81 2.49 2.41 }".
	if out := command(ctx, 6*time.Second, "sysctl", "-n", "vm.loadavg"); out != "" {
		l.setLoad(strings.Fields(strings.Trim(out, "{} \n")))
	}
	if total, err := strconv.ParseInt(command(ctx, 6*time.Second, "sysctl", "-n", "hw.memsize"), 10, 64); err == nil {
		l.MemTotal = &total
		// memory_pressure's free percentage is macOS's own estimate, including
		// reclaimable memory; it is what "available" means on a Mac.
		if m := freePctRE.FindStringSubmatch(command(ctx, 8*time.Second, "memory_pressure")); m != nil {
			pct, _ := strconv.ParseInt(m[1], 10, 64)
			avail := total * pct / 100
			l.MemAvail = &avail
		}
	}
	// macOS's own verdict on memory (compression and swapping), not swap size: swap
	// files grow on demand, so "% used" is no limit. 1 normal, 2 warn, 4 critical.
	if p, ok := pressureNames[command(ctx, 6*time.Second, "sysctl", "-n", "kern.memorystatus_vm_pressure_level")]; ok {
		l.Pressure = &p
	}
	swap := command(ctx, 6*time.Second, "sysctl", "-n", "vm.swapusage") // total = 11264.00M  used = 10195.00M ...
	t, u := swapTotalRE.FindStringSubmatch(swap), swapUsedRE.FindStringSubmatch(swap)
	if t != nil && u != nil {
		tm, _ := strconv.ParseFloat(t[1], 64)
		um, _ := strconv.ParseFloat(u[1], 64)
		total, used := int64(tm*(1<<20)), int64(um*(1<<20))
		l.SwapTotal, l.SwapUsed = &total, &used
	}
}

// dockerPaths are tried when docker isn't on PATH (non-interactive SSH shells).
var dockerPaths = []string{"/usr/local/bin/docker", "/opt/homebrew/bin/docker",
	"/Applications/Docker.app/Contents/Resources/bin/docker"}

func (l *Load) readDocker(ctx context.Context) {
	exe, err := exec.LookPath("docker")
	if err != nil {
		for _, p := range dockerPaths {
			if _, err := os.Stat(p); err == nil {
				exe = p
				break
			}
		}
	}
	if exe == "" {
		return // null: Docker isn't installed
	}
	info := strings.Fields(command(ctx, 6*time.Second, exe, "info", "--format", "{{.MemTotal}} {{.ContainersRunning}}"))
	if len(info) != 2 {
		down := "down"
		l.Docker = &down
		return
	}
	limit, err := strconv.ParseInt(info[0], 10, 64)
	if err != nil {
		down := "down"
		l.Docker = &down
		return
	}
	running, _ := strconv.Atoi(info[1])
	var used int64
	if running > 0 { // `docker stats` samples for ~0.5s; skip it when nothing runs
		for _, line := range strings.Split(command(ctx, 10*time.Second, exe, "stats", "--no-stream", "--format", "{{.MemUsage}}"), "\n") {
			if usage, _, ok := strings.Cut(line, "/"); ok {
				used += parseBytes(usage)
			}
		}
	}
	up := "up"
	l.Docker, l.DockerLimit, l.DockerUsed, l.Containers = &up, &limit, &used, &running
}

var bytesRE = regexp.MustCompile(`([\d.]+)\s*([KMGT]?i?B)`)

// parseBytes reads docker's sizes such as "123.4MiB" or "1.2GB".
func parseBytes(s string) int64 {
	m := bytesRE.FindStringSubmatch(strings.TrimSpace(s))
	if m == nil {
		return 0
	}
	n, _ := strconv.ParseFloat(m[1], 64)
	mult := map[string]float64{"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
		"KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}[m[2]]
	return int64(n * mult)
}

// command runs name with args and returns trimmed stdout, or "" on failure.
func command(ctx context.Context, timeout time.Duration, name string, args ...string) string {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = noBusEnv()
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// noBusEnv is this environment with DBUS_SESSION_BUS_ADDRESS=disabled: unless a real
// bus is set. Without a session bus, repeated probes can spawn persistent D-Bus
// daemons and exhaust resources.
func noBusEnv() []string {
	if os.Getenv("DBUS_SESSION_BUS_ADDRESS") != "" {
		return os.Environ()
	}
	return append(os.Environ(), "DBUS_SESSION_BUS_ADDRESS=disabled:")
}

var pressureNames = map[string]string{"1": "normal", "2": "warn", "4": "critical"}
