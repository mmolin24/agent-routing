package collector

import "fmt"

// Level grades one load reading.
type Level string

// Load levels, from best to worst.
const (
	OK      Level = "ok"
	Warn    Level = "warn"
	Hot     Level = "hot"
	Unknown Level = "unknown"
)

// HotLimits are a machine's thresholds for Hot; zero fields use the defaults
// (load 1.0 per core, 10% memory available, Docker at 90% of its limit).
type HotLimits struct {
	LoadPerCore    float64 `json:"load_per_core"`
	MinMemAvailPct float64 `json:"min_mem_avail_pct"`
	MaxDockerPct   float64 `json:"max_docker_pct"`
}

// Levels is a graded load: per reading, the worst of them, and why it's hot.
type Levels struct {
	CPU     Level    `json:"cpu,omitempty"`
	Mem     Level    `json:"mem,omitempty"`
	Swap    Level    `json:"swap,omitempty"`
	Docker  Level    `json:"docker,omitempty"`
	Overall Level    `json:"overall"`
	Hot     []string `json:"hot"`
}

func (h HotLimits) withDefaults() HotLimits {
	if h.LoadPerCore == 0 {
		h.LoadPerCore = 1.0
	}
	if h.MinMemAvailPct == 0 {
		h.MinMemAvailPct = 10
	}
	if h.MaxDockerPct == 0 {
		h.MaxDockerPct = 90
	}
	return h
}

// Grade rates a load against limits. Swap alone is never Hot: macOS keeps
// swap allocated even when it isn't short of memory. A Hot machine takes no
// new heavy work (the router queues it).
func Grade(l *Load, limits HotLimits) Levels {
	lv := Levels{Overall: Unknown, Hot: []string{}}
	if l == nil {
		return lv
	}
	lim := limits.withDefaults()
	var graded []Level
	if l.Load1 != nil && l.Cores > 0 {
		r := *l.Load1 / float64(l.Cores)
		lv.CPU = band(r >= lim.LoadPerCore, r >= 0.7*lim.LoadPerCore)
		if lv.CPU == Hot {
			lv.Hot = append(lv.Hot, fmt.Sprintf("load %.1f on %d cores", *l.Load1, l.Cores))
		}
		graded = append(graded, lv.CPU)
	}
	if l.MemTotal != nil && *l.MemTotal > 0 && l.MemAvail != nil {
		pct := 100 * float64(*l.MemAvail) / float64(*l.MemTotal)
		lv.Mem = band(pct < lim.MinMemAvailPct, pct < 2*lim.MinMemAvailPct)
		if lv.Mem == Hot {
			lv.Hot = append(lv.Hot, fmt.Sprintf("only %.0f%% memory available", pct))
		}
	}
	// macOS's own memory pressure level: warn grades the Mac Warn, critical Hot.
	if l.Pressure != nil && (*l.Pressure == "warn" || *l.Pressure == "critical") {
		p := band(*l.Pressure == "critical", true)
		if p == Hot && lv.Mem != Hot {
			lv.Hot = append(lv.Hot, "memory pressure critical")
		}
		if rank(p) > rank(lv.Mem) {
			lv.Mem = p
		}
	}
	if lv.Mem != "" {
		graded = append(graded, lv.Mem)
	}
	if l.SwapTotal != nil && *l.SwapTotal > 0 {
		used := int64(0)
		if l.SwapUsed != nil {
			used = *l.SwapUsed
		}
		lv.Swap = band(false, float64(used)/float64(*l.SwapTotal) >= 0.75)
		graded = append(graded, lv.Swap)
	}
	if l.Docker != nil && *l.Docker == "up" && l.DockerLimit != nil && *l.DockerLimit > 0 {
		used := int64(0)
		if l.DockerUsed != nil {
			used = *l.DockerUsed
		}
		pct := 100 * float64(used) / float64(*l.DockerLimit)
		lv.Docker = band(pct >= lim.MaxDockerPct, pct >= 0.83*lim.MaxDockerPct)
		if lv.Docker == Hot {
			lv.Hot = append(lv.Hot, fmt.Sprintf("Docker at %.0f%% of its memory limit", pct))
		}
		graded = append(graded, lv.Docker)
	}
	if len(graded) > 0 {
		lv.Overall = OK
		for _, g := range graded {
			if rank(g) > rank(lv.Overall) {
				lv.Overall = g
			}
		}
	}
	return lv
}

func band(hot, warn bool) Level {
	switch {
	case hot:
		return Hot
	case warn:
		return Warn
	}
	return OK
}

func rank(l Level) int {
	switch l {
	case Warn:
		return 1
	case Hot:
		return 2
	}
	return 0
}
