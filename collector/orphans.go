package collector

import (
	"context"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Orphan is a dev server whose starter is gone: its parent is init (launchd, /init,
// systemd, WSL's Relay). Routing only lists them; stopping one is the human's call.
type Orphan struct {
	PID   int    `json:"pid"`
	AgeS  int    `json:"age_s"`
	Ports []int  `json:"ports"`
	RSS   int64  `json:"rss"`
	Cwd   string `json:"cwd"`
	Cmd   string `json:"cmd"`
}

var (
	devRE  = regexp.MustCompile(`\b(expo start|vite|next dev|next start|webpack serve|storybook dev|react-native start)\b`)
	initRE = regexp.MustCompile(`^(/sbin/launchd|launchd|/init|init|/sbin/init|.*/systemd|systemd|Relay\(\d+\))(\s|$)`)
	portRE = regexp.MustCompile(`--port[= ](\d+)`)
)

// maxCwd is how many orphans get their directory read per probe, as in host_load.MAX_CWD.
const maxCwd = 10

type psProc struct {
	ppid, age int
	rss       int64
	cmd       string
}

// etimeS converts ps etime ([[dd-]hh:]mm:ss) to seconds.
func etimeS(text string) (int, bool) {
	text = strings.TrimSpace(text)
	days := 0
	if i := strings.LastIndex(text, "-"); i >= 0 {
		d, err := strconv.Atoi(text[:i])
		if err != nil {
			return 0, false
		}
		days, text = d, text[i+1:]
	}
	var parts []int
	for _, x := range strings.Split(text, ":") {
		n, err := strconv.Atoi(x)
		if err != nil {
			return 0, false
		}
		parts = append(parts, n)
	}
	for len(parts) < 3 {
		parts = append([]int{0}, parts...)
	}
	return days*86400 + parts[0]*3600 + parts[1]*60 + parts[2], true
}

// findOrphans parses `ps -axo pid=,ppid=,etime=,rss=,command=` output, the same way as
// host_load.orphans.
func findOrphans(psText string, cwdOf func(int) string) []Orphan {
	procs := map[int]psProc{}
	kids := map[int][]int{}
	for _, ln := range strings.Split(psText, "\n") {
		f := strings.Fields(ln)
		if len(f) < 5 {
			continue
		}
		pid, e1 := strconv.Atoi(f[0])
		ppid, e2 := strconv.Atoi(f[1])
		age, ok := etimeS(f[2])
		rss, e3 := strconv.ParseInt(f[3], 10, 64)
		if e1 != nil || e2 != nil || e3 != nil || !ok {
			continue
		}
		procs[pid] = psProc{ppid: ppid, age: age, rss: rss * 1024, cmd: strings.Join(f[4:], " ")}
	}
	pids := make([]int, 0, len(procs))
	for pid, p := range procs {
		pids = append(pids, pid)
		kids[p.ppid] = append(kids[p.ppid], pid)
	}
	sort.Ints(pids)
	out := []Orphan{}
	for _, pid := range pids {
		p := procs[pid]
		m := devRE.FindStringSubmatch(p.cmd)
		if m == nil {
			continue
		}
		parent, has := procs[p.ppid]
		if p.ppid != 1 && !(has && initRE.MatchString(parent.cmd)) {
			continue
		}
		var rss int64
		ports := map[int]bool{}
		stack := []int{pid}
		for len(stack) > 0 {
			q := stack[len(stack)-1]
			stack = stack[:len(stack)-1]
			rss += procs[q].rss
			for _, pm := range portRE.FindAllStringSubmatch(procs[q].cmd, -1) {
				if n, err := strconv.Atoi(pm[1]); err == nil {
					ports[n] = true
				}
			}
			stack = append(stack, kids[q]...)
		}
		list := []int{}
		for n := range ports {
			list = append(list, n)
		}
		sort.Ints(list)
		cwd := ""
		if len(out) < maxCwd { // lsof per orphan on the Mac: cap the cost
			cwd = cwdOf(pid)
		}
		out = append(out, Orphan{PID: pid, AgeS: p.age, Ports: list, RSS: rss, Cwd: cwd, Cmd: m[1]})
	}
	return out
}

// procCwd is a process's working directory, or "" when it can't be read.
func procCwd(ctx context.Context) func(int) string {
	return func(pid int) string {
		if d, err := os.Readlink("/proc/" + strconv.Itoa(pid) + "/cwd"); err == nil {
			return d
		}
		out := command(ctx, 4*time.Second, "lsof", "-a", "-p", strconv.Itoa(pid), "-d", "cwd", "-Fn")
		for _, ln := range strings.Split(out, "\n") {
			if strings.HasPrefix(ln, "n") {
				return ln[1:]
			}
		}
		return ""
	}
}

func readOrphans(ctx context.Context) []Orphan {
	return findOrphans(command(ctx, 6*time.Second, "ps", "-axo", "pid=,ppid=,etime=,rss=,command="), procCwd(ctx))
}
