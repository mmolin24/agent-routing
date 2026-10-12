package collector

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/google/go-cmp/cmp"
)

func TestTaskHeavy(t *testing.T) {
	tests := []struct {
		name     string
		contract string
		want     bool
	}{
		{name: "web work is light", contract: `{"routing":{"requires":["linux"]}}`, want: false},
		{name: "docker work is heavy", contract: `{"routing":{"requires":["linux","docker"]}}`, want: true},
		{name: "docker marked light", contract: `{"routing":{"requires":["docker"]},"resources":{"class":"light"}}`, want: false},
		{name: "compose is always heavy", contract: `{"resources":{"runtime":"compose","class":"light"}}`, want: true},
		{name: "explicit heavy", contract: `{"resources":{"class":"heavy"}}`, want: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var tk task
			if err := json.Unmarshal([]byte(tt.contract), &tk); err != nil {
				t.Fatalf("bad test contract: %v", err)
			}
			if got := tk.heavy(); got != tt.want {
				t.Errorf("heavy() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestAddAgents(t *testing.T) {
	rep := &Report{Tasks: []TaskRow{{TaskID: "app833-impl", Host: "desktop", Agent: "app833-impl", Agents: []Entry{}}}, Untracked: []Entry{}}
	index := map[[2]string]int{{"desktop", "w8"}: 0}
	pr := &ProbeResult{
		Agents: []Agent{
			{PaneID: "w8:p1", Name: "app833-impl", Kind: "codex", Status: "working", Cwd: "/wt", WorkspaceID: "w8"},
			{PaneID: "w8:p2", Name: "app833-tests", Kind: "codex", Status: "idle", Cwd: "/wt", WorkspaceID: "w8"},
			{PaneID: "w1:p1", Kind: "claude", Status: "idle", Cwd: "/main", WorkspaceID: "w1"},
		},
		Tokens:   map[string]map[string]string{"w8:p2": {"parent": "w8:p1"}},
		Profiles: []*Profile{{Model: "gpt-6.1-sol", Effort: "high"}, nil, {Model: "claude-opus-5-5", Fast: true}},
	}
	addAgents(rep, index, "desktop", pr)

	wantTask := []Entry{
		{PaneID: "w8:p1", Name: "app833-impl", Kind: "codex", Status: "working", Cwd: "/wt", Model: "gpt-6.1-sol",
			Effort: "high", ProfileShared: true, Subagents: []Subagent{}, Role: "worker"},
		{PaneID: "w8:p2", Name: "app833-tests", Kind: "codex", Status: "idle", Cwd: "/wt", ProfileShared: true,
			Parent: "w8:p1", Subagents: []Subagent{}, Role: "child"},
	}
	if diff := cmp.Diff(wantTask, rep.Tasks[0].Agents); diff != "" {
		t.Errorf("task agents mismatch (-want +got):\n%s", diff)
	}
	wantUntracked := []Entry{{PaneID: "w1:p1", Kind: "claude", Status: "idle", Cwd: "/main", Model: "claude-opus-5-5",
		Fast: true, Subagents: []Subagent{}, Host: "desktop"}}
	if diff := cmp.Diff(wantUntracked, rep.Untracked); diff != "" {
		t.Errorf("untracked mismatch (-want +got):\n%s", diff)
	}
}

func TestSnapshotRejectsUnknownOrchestrator(t *testing.T) {
	_, err := Snapshot(context.Background(), SnapshotRequest{Config: Config{Hosts: map[string]Host{"desktop": {}}}, Orchestrator: "laptop"})
	if err == nil {
		t.Error("Snapshot() with an unknown orchestrator host succeeded, want an error")
	}
}

func TestShellQuote(t *testing.T) {
	tests := []struct{ in, want string }{
		{in: `{"a":1}`, want: `'{"a":1}'`},
		{in: `it's`, want: `'it'\''s'`},
	}
	for _, tt := range tests {
		if got := shellQuote(tt.in); got != tt.want {
			t.Errorf("shellQuote(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestAddAgentsGroupsByTab(t *testing.T) {
	// One workspace per issue (w9 = APP-981), a tab per task.
	rep := &Report{Tasks: []TaskRow{
		{TaskID: "app981-impl", Host: "mac", Agent: "app981-impl", WorkspaceID: "w9", TabID: "w9:t1", Agents: []Entry{}},
		{TaskID: "app981-gate", Host: "mac", Agent: "app981-gate", WorkspaceID: "w9", TabID: "w9:t2", Agents: []Entry{}},
	}, Untracked: []Entry{}}
	index := map[[2]string]int{{"mac", "tab:w9:t1"}: 0, {"mac", "tab:w9:t2"}: 1}
	pr := &ProbeResult{Agents: []Agent{
		{PaneID: "w9:p1", Name: "app981-impl", Status: "done", WorkspaceID: "w9", TabID: "w9:t1"},
		{PaneID: "w9:p2", Name: "app981-gate", Status: "working", WorkspaceID: "w9", TabID: "w9:t2"},
		{PaneID: "w9:p3", Name: "", Status: "idle", WorkspaceID: "w9", TabID: "w9:t3"}, // a tab no task owns
	}}
	addAgents(rep, index, "mac", pr)

	got := map[string][]string{}
	for _, row := range rep.Tasks {
		for _, a := range row.Agents {
			got[row.TaskID] = append(got[row.TaskID], a.PaneID+" "+a.Role)
		}
	}
	want := map[string][]string{"app981-impl": {"w9:p1 worker"}, "app981-gate": {"w9:p2 worker"}}
	if diff := cmp.Diff(want, got); diff != "" {
		t.Errorf("agents per task mismatch (-want +got):\n%s", diff)
	}
	if len(rep.Untracked) != 1 || rep.Untracked[0].PaneID != "w9:p3" {
		t.Errorf("untracked = %+v, want only w9:p3", rep.Untracked)
	}
}

func TestAddAgentsFollowsMovedTab(t *testing.T) {
	// The link recorded w9:t1, but someone moved the tab into another workspace:
	// Herdr renumbered it wT:t7, and w9:t1 now belongs to nobody (or someone else).
	rep := &Report{Tasks: []TaskRow{
		{TaskID: "app857-impl", Host: "mac", Agent: "app857-impl", WorkspaceID: "w9", TabID: "w9:t1", Agents: []Entry{}},
	}, Untracked: []Entry{}}
	index := map[[2]string]int{{"mac", "tab:w9:t1"}: 0}
	pr := &ProbeResult{Agents: []Agent{
		{PaneID: "wT:p7", Name: "app857-impl", Status: "working", WorkspaceID: "wT", TabID: "wT:t7"},
		{PaneID: "wT:p8", Name: "", Status: "working", WorkspaceID: "wT", TabID: "wT:t7"},          // its child, same tab
		{PaneID: "w9:p1", Name: "someone-else", Status: "idle", WorkspaceID: "w9", TabID: "w9:t1"}, // reused id
	}}
	addAgents(rep, index, "mac", pr)
	var got []string
	for _, a := range rep.Tasks[0].Agents {
		got = append(got, a.PaneID+" "+a.Role)
	}
	if diff := cmp.Diff([]string{"wT:p7 worker", "wT:p8 child"}, got); diff != "" {
		t.Errorf("task agents mismatch (-want +got):\n%s", diff)
	}
	if len(rep.Untracked) != 1 || rep.Untracked[0].PaneID != "w9:p1" {
		t.Errorf("untracked = %+v, want only w9:p1", rep.Untracked)
	}
}
