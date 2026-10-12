package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// SnapshotRequest says where the router keeps its records and which host the
// snapshot is taken from; route.py passes its own parsed config.
type SnapshotRequest struct {
	Config       Config `json:"config"`
	ActiveDir    string `json:"active_dir"`
	QueueDir     string `json:"queue_dir"`
	Orchestrator string `json:"orchestrator"`
	// RepoRoot holds scripts/*.py, sent to a machine that has no agent-collect.
	RepoRoot string `json:"repo_root"`
}

// Config is the part of config/hosts.*.json the collector needs.
type Config struct {
	Hosts map[string]Host `json:"hosts"`
}

// Host is one machine in the config.
type Host struct {
	HerdrBin  string           `json:"herdr_bin"`
	Reach     map[string]Reach `json:"reach"`
	MaxHeavy  *int             `json:"max_heavy"`
	MaxQueue  *int             `json:"max_queue"`
	HotLimits HotLimits        `json:"hot_limits"`
}

// Reach says how one host is reached from another.
type Reach struct {
	Mode     string `json:"mode"` // "local", "ssh" or "machine"
	Selector string `json:"selector"`
	SSHAlias string `json:"ssh_alias"`
}

// Result is the data behind the views: route.py --snapshot's JSON.
type Result struct {
	Report   Report              `json:"report"`
	Queues   map[string][]string `json:"queues"`
	Capacity map[string]Capacity `json:"capacity"`
}

// Report lists open tasks with their agents, agents that belong to no task,
// machines that couldn't be read, and each machine's load.
type Report struct {
	Tasks       []TaskRow        `json:"tasks"`
	Untracked   []Entry          `json:"untracked"`
	Unreachable []string         `json:"unreachable"`
	Loads       map[string]*Load `json:"loads"`
	Usage       *Usage           `json:"usage,omitempty"` // newest Codex/Claude plan usage across machines
}

// TaskRow is one open, routed task.
type TaskRow struct {
	TaskID      string   `json:"task_id"`
	Host        string   `json:"host"`
	Heavy       bool     `json:"heavy"`
	Summary     string   `json:"summary"`
	WorkspaceID string   `json:"workspace_id"`
	TabID       string   `json:"tab_id,omitempty"`
	Group       string   `json:"group,omitempty"`     // the issue's workspace label, e.g. APP-1
	TabLabel    string   `json:"tab_label,omitempty"` // the task's tab in it, e.g. gate
	Agent       string   `json:"agent"`
	Dispatch    string   `json:"dispatch"`
	Agents      []Entry  `json:"agents"`
	Git         *TreeGit `json:"git,omitempty"` // unpushed work, for tasks that may push
}

// Entry is one live agent with its profile.
type Entry struct {
	PaneID        string     `json:"pane_id"`
	Name          string     `json:"name"`
	Kind          string     `json:"agent"`
	Status        string     `json:"status"`
	Cwd           string     `json:"cwd"`
	Model         string     `json:"model"`
	Effort        string     `json:"effort"`
	Fast          bool       `json:"fast"`
	ProfileShared bool       `json:"profile_shared"`
	Parent        string     `json:"parent"`
	ActiveAt      int64      `json:"active_at"` // last write to its session file (0: none found); --reap's idle clock
	Subagents     []Subagent `json:"subagents"`
	Role          string     `json:"role,omitempty"` // "worker" or "child", for agents in a task's workspace
	Host          string     `json:"host,omitempty"` // for untracked agents
}

// Capacity is one machine's open work and load.
type Capacity struct {
	Heavy    int         `json:"heavy"`
	Light    int         `json:"light"`
	MaxHeavy *int        `json:"max_heavy"`
	MaxQueue int         `json:"max_queue"`
	Load     *Load       `json:"load,omitempty"`
	Levels   *Levels     `json:"levels,omitempty"`
	Slots    *HeavySlots `json:"slots,omitempty"` // heavy phases running and waiting (scripts/heavy)
}

// task is the part of a recorded task contract the collector reads.
type task struct {
	TaskID        string `json:"task_id"`
	Summary       string `json:"summary"`
	SelectedHost  string `json:"selected_host"`
	Authorization struct {
		Scope string `json:"scope"`
	} `json:"authorization"`
	Routing struct {
		Requires []string `json:"requires"`
	} `json:"routing"`
	Resources struct {
		Runtime string `json:"runtime"`
		Class   string `json:"class"`
	} `json:"resources"`
	Link *struct {
		Host        string `json:"host"`
		WorkspaceID string `json:"workspace_id"`
		TabID       string `json:"tab_id"`
		Group       string `json:"group"`
		TabLabel    string `json:"tab_label"`
		Agent       string `json:"agent"`
		State       string `json:"state"`
	} `json:"link"`
}

// heavy mirrors route.is_heavy: a Compose runtime or class "heavy", or
// docker work unless the class is explicitly "light".
func (t task) heavy() bool {
	if t.Resources.Runtime == "compose" || t.Resources.Class == "heavy" {
		return true
	}
	if t.Resources.Class == "light" {
		return false
	}
	for _, r := range t.Routing.Requires {
		if r == "docker" {
			return true
		}
	}
	return false
}

// Snapshot probes every machine reachable from req.Orchestrator in parallel
// and assembles the views' data. It fails only on unusable input; a machine
// that can't be read is listed in Report.Unreachable.
func Snapshot(ctx context.Context, req SnapshotRequest) (*Result, error) {
	if _, ok := req.Config.Hosts[req.Orchestrator]; !ok {
		return nil, fmt.Errorf("orchestrator host %q is not in the config", req.Orchestrator)
	}
	tasks := readTasks(req.ActiveDir, req.Config)
	res := &Result{
		Report:   Report{Tasks: []TaskRow{}, Untracked: []Entry{}, Unreachable: []string{}, Loads: map[string]*Load{}},
		Queues:   map[string][]string{},
		Capacity: map[string]Capacity{},
	}
	// (host, "tab:"+tab) -> row for tasks in the grouped layout (one workspace per
	// issue, a tab per task); (host, workspace) -> row for older tasks.
	index := map[[2]string]int{}
	spaces := map[string]map[string]bool{} // host -> workspaces holding open tasks
	trees := map[string][]TreeRequest{}    // host -> worktrees of tasks that may push
	for _, t := range tasks {
		row := TaskRow{TaskID: t.TaskID, Host: t.SelectedHost, Heavy: t.heavy(), Summary: t.Summary,
			Dispatch: "recorded, not dispatched", Agents: []Entry{}}
		if t.Link != nil {
			row.WorkspaceID, row.TabID, row.Agent = t.Link.WorkspaceID, t.Link.TabID, t.Link.Agent
			row.Group, row.TabLabel = t.Link.Group, t.Link.TabLabel
			if t.Link.State != "" {
				row.Dispatch = t.Link.State
			}
			switch {
			case row.TabID != "":
				index[[2]string{t.Link.Host, "tab:" + row.TabID}] = len(res.Report.Tasks)
			case row.WorkspaceID != "":
				index[[2]string{t.Link.Host, row.WorkspaceID}] = len(res.Report.Tasks)
			}
			if row.WorkspaceID != "" {
				if spaces[t.Link.Host] == nil {
					spaces[t.Link.Host] = map[string]bool{}
				}
				spaces[t.Link.Host][row.WorkspaceID] = true
			}
		}
		res.Report.Tasks = append(res.Report.Tasks, row)
		if tr := pushTree(req.RepoRoot, t); tr != nil {
			trees[t.SelectedHost] = append(trees[t.SelectedHost], *tr)
		}
	}

	var hosts []string
	for h, cfg := range req.Config.Hosts {
		if _, ok := cfg.Reach[req.Orchestrator]; ok {
			hosts = append(hosts, h)
		}
	}
	sort.Strings(hosts)
	type outcome struct {
		res  *ProbeResult
		note string
		err  error
	}
	outcomes := make([]outcome, len(hosts))
	slots := map[string]*HeavySlots{}
	var usages []Usage
	var wg sync.WaitGroup
	for i, h := range hosts {
		workspaces := []string{} // never null: the Python probe iterates it
		for ws := range spaces[h] {
			workspaces = append(workspaces, ws)
		}
		sort.Strings(workspaces)
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, note, err := probeHost(ctx, req, h, workspaces, trees[h])
			outcomes[i] = outcome{r, note, err}
		}()
	}
	wg.Wait()

	for i, h := range hosts {
		o := outcomes[i]
		if o.err != nil {
			res.Report.Unreachable = append(res.Report.Unreachable, fmt.Sprintf("%s: %v", h, o.err))
			continue
		}
		if o.note != "" {
			res.Report.Unreachable = append(res.Report.Unreachable, fmt.Sprintf("%s: %s", h, o.note))
		}
		res.Report.Loads[h] = o.res.Load
		if o.res.Heavy != nil {
			slots[h] = o.res.Heavy
		}
		if o.res.Usage != nil {
			usages = append(usages, *o.res.Usage)
		}
		addAgents(&res.Report, index, h, o.res)
		for j := range res.Report.Tasks {
			if g := o.res.Git[res.Report.Tasks[j].TaskID]; g != nil && res.Report.Tasks[j].Host == h {
				res.Report.Tasks[j].Git = g
			}
		}
	}

	if len(usages) > 0 {
		u := newestUsage(usages)
		res.Report.Usage = &u
	}
	for h, cfg := range req.Config.Hosts {
		c := Capacity{MaxHeavy: cfg.MaxHeavy, MaxQueue: 10}
		if cfg.MaxQueue != nil {
			c.MaxQueue = *cfg.MaxQueue
		}
		for _, t := range tasks {
			if t.SelectedHost != h {
				continue
			}
			if t.heavy() {
				c.Heavy++
			} else {
				c.Light++
			}
		}
		if _, ok := cfg.Reach[req.Orchestrator]; ok {
			c.Load = res.Report.Loads[h]
			c.Slots = slots[h]
			lv := Grade(c.Load, cfg.HotLimits)
			c.Levels = &lv
		}
		res.Capacity[h] = c
		res.Queues[h] = queueIDs(filepath.Join(req.QueueDir, h))
	}
	return res, nil
}

// addAgents files host's agents under their task, or as untracked.
//
// A task's worker is found by name: Herdr keeps agent names unique per machine,
// and the name follows the agent when someone moves its tab between workspaces
// (which renumbers tab and workspace ids, so a recorded id can go stale or even
// point at someone else's tab). The other agents in the worker's current tab
// (its whole workspace, for tasks from before the grouped layout) are its
// children. Only a task with no live worker falls back to the recorded ids.
func addAgents(rep *Report, index map[[2]string]int, host string, pr *ProbeResult) {
	perCwd := map[string]int{}
	byName := map[string]int{}
	for row, t := range rep.Tasks {
		if t.Host == host && t.Agent != "" {
			byName[t.Agent] = row
		}
	}
	live := map[string]int{} // the worker's current "tab:<id>" (or workspace, legacy) -> row
	hasWorker := map[int]bool{}
	for _, a := range pr.Agents {
		perCwd[a.Cwd]++
		row, ok := byName[a.Name]
		if !ok || a.Name == "" {
			continue
		}
		hasWorker[row] = true
		if a.TabID != "" {
			live["tab:"+a.TabID] = row
		}
		if rep.Tasks[row].TabID == "" && a.WorkspaceID != "" {
			live[a.WorkspaceID] = row
		}
	}
	for i, a := range pr.Agents {
		e := Entry{PaneID: a.PaneID, Name: a.Name, Kind: a.Kind, Status: a.Status, Cwd: a.Cwd,
			// Agents sharing a directory can't be told apart by session files.
			ProfileShared: perCwd[a.Cwd] > 1,
			// Set by whoever started the pane: `herdr pane report-metadata <pane> --token parent=<pane>`.
			Parent:    pr.Tokens[a.PaneID]["parent"],
			Subagents: []Subagent{}}
		if i < len(pr.Profiles) && pr.Profiles[i] != nil {
			p := pr.Profiles[i]
			e.Model, e.Effort, e.Fast, e.ActiveAt = p.Model, p.Effort, p.Fast, p.ActiveAt
			if p.Subagents != nil {
				e.Subagents = p.Subagents
			}
		}
		row, role := taskFor(a, host, byName, live, index, hasWorker)
		if row < 0 {
			e.Host = host
			rep.Untracked = append(rep.Untracked, e)
			continue
		}
		e.Role = role
		rep.Tasks[row].Agents = append(rep.Tasks[row].Agents, e)
	}
}

// taskFor returns the row an agent belongs to and its role, or -1.
func taskFor(a Agent, host string, byName, live map[string]int, index map[[2]string]int, hasWorker map[int]bool) (int, string) {
	if row, ok := byName[a.Name]; ok && a.Name != "" {
		return row, "worker"
	}
	if row, ok := live["tab:"+a.TabID]; ok && a.TabID != "" {
		return row, "child"
	}
	if row, ok := live[a.WorkspaceID]; ok && a.WorkspaceID != "" {
		return row, "child"
	}
	// No live worker to follow: trust the ids the link recorded.
	if row, ok := index[[2]string{host, "tab:" + a.TabID}]; ok && a.TabID != "" && !hasWorker[row] {
		return row, "child"
	}
	if row, ok := index[[2]string{host, a.WorkspaceID}]; ok && !hasWorker[row] {
		return row, "child"
	}
	return -1, ""
}

// probeHost probes one host: in-process when it's the orchestrator's own
// machine, else with one SSH call. The note explains a degraded result.
func probeHost(ctx context.Context, req SnapshotRequest, host string, workspaces []string, trees []TreeRequest) (*ProbeResult, string, error) {
	cfg := req.Config.Hosts[host]
	preq := ProbeRequest{Herdr: cfg.HerdrBin, Workspaces: workspaces, Trees: trees}
	reach := cfg.Reach[req.Orchestrator]
	if reach.Mode == "local" {
		r, err := Probe(ctx, preq)
		return r, "", err
	}
	r, err := probeOverSSH(ctx, req.RepoRoot, reach.SSHAlias, preq)
	if err == nil {
		return r, "", nil
	}
	if reach.Mode == "machine" {
		// SSH to the target may not work from here; Herdr's own link still lists agents.
		agents, merr := agentsViaMachine(ctx, req.Config.Hosts[req.Orchestrator].HerdrBin, reach.Selector)
		if merr == nil {
			return &ProbeResult{Agents: agents, Tokens: map[string]map[string]string{},
				Profiles: make([]*Profile, len(agents))}, fmt.Sprintf("probe over SSH failed (%v); agents only via Herdr", err), nil
		}
	}
	return nil, "", err
}

// remotePython picks the newest Python on the far side; non-interactive SSH
// shells skip ~/.local/bin and Homebrew. Mirrors route.py's REMOTE_PY.
const remotePython = `P=; for c in "$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; ` +
	`do command -v "$c" >/dev/null 2>&1 && P=$c && break; done; "$P" -`

// probeOverSSH runs agent-collect probe on the far side, or the Python probe
// (sent on stdin) when agent-collect isn't installed there.
func probeOverSSH(ctx context.Context, repoRoot, alias string, preq ProbeRequest) (*ProbeResult, error) {
	if alias == "" {
		return nil, errors.New("no ssh_alias configured")
	}
	body, err := json.Marshal(preq)
	if err != nil {
		return nil, err
	}
	arg := shellQuote(string(body))
	remote := `A="$HOME/.local/bin/agent-collect"; if [ -x "$A" ]; then exec "$A" probe ` + arg +
		`; else ` + remotePython + ` ` + arg + `; fi`
	script, err := pythonProbeScript(repoRoot)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, remote)
	cmd.Stdin = strings.NewReader(script)
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) && len(ee.Stderr) > 0 {
			return nil, fmt.Errorf("ssh %s: %s", alias, lastLine(string(ee.Stderr)))
		}
		return nil, fmt.Errorf("ssh %s: %w", alias, err)
	}
	var r ProbeResult
	if err := json.Unmarshal(out, &r); err != nil {
		return nil, fmt.Errorf("probe output from %s: %w", alias, err)
	}
	return &r, nil
}

// pythonProbeScript concatenates the Python readers (without their CLI blocks)
// into one script, as route.py's probe_script does.
func pythonProbeScript(repoRoot string) (string, error) {
	var b strings.Builder
	b.WriteString("PROBE_EMBEDDED = True\n")
	for _, name := range []string{"agent_profile.py", "host_load.py", "probe.py"} {
		src, err := os.ReadFile(filepath.Join(repoRoot, "scripts", name))
		if err != nil {
			return "", err
		}
		body, _, _ := strings.Cut(string(src), "\nif __name__ == \"__main__\"")
		b.WriteString(body + "\n")
	}
	b.WriteString("print(json.dumps(probe(json.loads(sys.argv[1]), profiles, snapshot)))\n")
	return b.String(), nil
}

func agentsViaMachine(ctx context.Context, herdrBin, selector string) ([]Agent, error) {
	var list struct {
		Result struct {
			Agents []Agent `json:"agents"`
		} `json:"result"`
	}
	if err := herdrJSON(ctx, expandHome(herdrBin), &list, "--machine", selector, "agent", "list"); err != nil {
		return nil, err
	}
	return list.Result.Agents, nil
}

// readTasks reads the open task contracts; unreadable files and tasks with no
// known recorded host are skipped (route.py --status reports those).
// pushTree is the worktree of a dispatched task whose grant lets it push
// (project-workflow, e.g. draft-pr), from its runs/tasks/<id>/link.json; else
// nil. Mirrors dispatch.push_tree.
func pushTree(repoRoot string, t task) *TreeRequest {
	if t.Authorization.Scope != "project-workflow" {
		return nil
	}
	b, err := os.ReadFile(filepath.Join(repoRoot, "runs", "tasks", t.TaskID, "link.json"))
	if err != nil {
		return nil
	}
	var ln struct {
		State    string `json:"state"`
		Worktree string `json:"worktree"`
		Branch   string `json:"branch"`
	}
	if json.Unmarshal(b, &ln) != nil || ln.State != "dispatched" || ln.Worktree == "" || ln.Branch == "" {
		return nil
	}
	return &TreeRequest{Task: t.TaskID, Path: ln.Worktree, Branch: ln.Branch}
}

func readTasks(dir string, cfg Config) []task {
	var out []task
	files := glob(filepath.Join(dir, "*.json"))
	sort.Strings(files)
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		var t task
		if json.Unmarshal(b, &t) != nil || t.TaskID == "" {
			continue
		}
		if _, ok := cfg.Hosts[t.SelectedHost]; !ok {
			continue
		}
		out = append(out, t)
	}
	return out
}

// queueIDs lists a host's queued task ids, oldest first (files are
// "<nanoseconds>-<task_id>.json").
func queueIDs(dir string) []string {
	files := glob(filepath.Join(dir, "*.json"))
	sort.Strings(files)
	ids := []string{}
	for _, f := range files {
		if _, id, ok := strings.Cut(filepath.Base(f), "-"); ok {
			ids = append(ids, strings.ToLower(strings.TrimSuffix(id, ".json")))
		}
	}
	return ids
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	return lines[len(lines)-1]
}
