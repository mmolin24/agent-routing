package collector

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
	"github.com/google/go-cmp/cmp/cmpopts"
)

func TestReadProfileCodex(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	const cwd = "/work/example-app/.worktrees/APP-833"
	tests := []struct {
		name    string
		session string
		want    *Profile
	}{
		{
			name: "model, effort and default tier",
			session: `{"type":"session_meta","payload":{"cwd":"` + cwd + `"}}
{"type":"turn_context","payload":{"model":"gpt-5.6-terra","effort":"medium"}}
{"type":"event_msg","payload":{"info":{"service_tier":"default"}}}
{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"high"}}
`,
			want: &Profile{Model: "gpt-6.1-sol", Effort: "high", Tier: "default"},
		},
		{
			name: "fast tier",
			session: `{"type":"session_meta","payload":{"cwd":"` + cwd + `"}}
{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"low"}}
{"type":"event_msg","payload":{"service_tier":"priority"}}
`,
			want: &Profile{Model: "gpt-6.1-sol", Effort: "low", Tier: "priority", Fast: true},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := filepath.Join(home, ".codex", "sessions", "2026", "10", "08")
			os.RemoveAll(filepath.Join(home, ".codex"))
			writeFile(t, dir, "rollout-2026-10-08T18-05-11-abc.jsonl", tt.session)
			got := ReadProfile("codex", cwd, "")
			if diff := cmp.Diff(tt.want, got, cmpopts.IgnoreFields(Profile{}, "Source", "ActiveAt")); diff != "" {
				t.Errorf("ReadProfile(codex) mismatch (-want +got):\n%s", diff)
			}
			if got != nil && time.Since(time.Unix(got.ActiveAt, 0)) > time.Minute {
				t.Errorf("ActiveAt = %d, want the rollout's mtime (just written)", got.ActiveAt)
			}
		})
	}
	if got := ReadProfile("codex", "/no/such/dir", ""); got != nil {
		t.Errorf("ReadProfile(codex, unknown cwd) = %+v, want nil", got)
	}
}

func TestReadProfileClaude(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	const cwd = "/home/dev/projects/agent-routing"
	pdir := filepath.Join(home, ".claude", "projects", "-home-dev-projects-agent-routing")
	writeFile(t, pdir, "sess.jsonl", `{"type":"user","message":{"content":"hi"}}
{"type":"assistant","effort":"medium","message":{"model":"claude-opus-5-5","usage":{"speed":"standard"}}}
{"type":"assistant","effort":"high","message":{"model":"claude-opus-5-5","usage":{"speed":"fast"}}}
{"type":"user","message":{"content":"tool result"}}
`)
	sub := filepath.Join(pdir, "sess", "subagents")
	writeFile(t, sub, "agent-a1.meta.json", `{"agentType":"Explore","description":"Map the repo","spawnDepth":1}`)
	writeFile(t, sub, "agent-a1.jsonl", `{"type":"assistant","message":{"model":"claude-sonnet-5-5"}}`+"\n")
	writeFile(t, sub, "agent-old.meta.json", `{"agentType":"general-purpose","description":"Stale","spawnDepth":1}`)
	old := writeFile(t, sub, "agent-old.jsonl", `{"type":"assistant","message":{"model":"claude-haiku-5-5"}}`+"\n")
	stale := time.Now().Add(-2 * time.Hour)
	if err := os.Chtimes(old, stale, stale); err != nil {
		t.Fatalf("Chtimes: %v", err)
	}

	got := ReadProfile("claude", cwd, "")
	want := &Profile{Model: "claude-opus-5-5", Effort: "high", Fast: true,
		Subagents: []Subagent{{ID: "a1", Description: "Map the repo", Type: "Explore", Depth: 1,
			Model: "claude-sonnet-5-5", Running: true}}}
	if diff := cmp.Diff(want, got, cmpopts.IgnoreFields(Profile{}, "Source", "ActiveAt")); diff != "" {
		t.Errorf("ReadProfile(claude) mismatch (-want +got):\n%s", diff)
	}
	if got != nil && time.Since(time.Unix(got.ActiveAt, 0)) > time.Minute {
		t.Errorf("ActiveAt = %d, want the transcript's mtime (just written)", got.ActiveAt)
	}
}

// Two agents in one directory: each gets its own session's profile when herdr
// gives its session id, and the newest one only as a fallback.
func TestReadProfileBySession(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	const cwd = "/home/dev/projects/agent-routing"
	pdir := filepath.Join(home, ".claude", "projects", "-home-dev-projects-agent-routing")
	const mine, theirs = "cc82d044-a5ca-04bc-ca55-c63daf0d8560", "990a1023-e026-6cd6-d37e-1a3f437be670"
	minePath := writeFile(t, pdir, mine+".jsonl", `{"type":"assistant","effort":"high","message":{"model":"claude-opus-5-5"}}`+"\n")
	writeFile(t, filepath.Join(pdir, mine, "subagents"), "agent-m1.meta.json", `{"agentType":"Explore","description":"mine","spawnDepth":1}`)
	writeFile(t, filepath.Join(pdir, mine, "subagents"), "agent-m1.jsonl", `{"type":"assistant","message":{"model":"claude-haiku-5-5"}}`+"\n")
	writeFile(t, pdir, theirs+".jsonl", `{"type":"assistant","effort":"low","message":{"model":"claude-sonnet-5-5"}}`+"\n")
	older := time.Now().Add(-time.Minute)
	if err := os.Chtimes(minePath, older, older); err != nil { // theirs is newest
		t.Fatalf("Chtimes: %v", err)
	}
	got := ReadProfile("claude", cwd, mine)
	if got.Model != "claude-opus-5-5" || !got.BySession || got.Shared || len(got.Subagents) != 1 || got.Subagents[0].Description != "mine" {
		t.Errorf("by session: got %+v", got)
	}
	if got := ReadProfile("claude", cwd, ""); got.Model != "claude-sonnet-5-5" || got.BySession || !got.Shared {
		t.Errorf("fallback: got %+v, want the newest (theirs), shared", got)
	}
	if got := ReadProfile("claude", cwd, "*"); got.BySession {
		t.Errorf("a glob in the id must not be used: got %+v", got)
	}
	if got := ReadProfile("claude", cwd, "0000dead-beef"); got.Model != "claude-sonnet-5-5" || got.BySession {
		t.Errorf("unknown id falls back to the newest: got %+v", got)
	}

	const codexCwd = "/work/example-app/.worktrees/APP-611"
	dir := filepath.Join(home, ".codex", "sessions", "2026", "10", "09")
	const impl, gate = "c3221865-94a5-57f3-2f4a-6204eae22369", "fc1c9f38-f03b-2ed9-deeb-feb883a04e4b"
	implPath := writeFile(t, dir, "rollout-2026-10-09T11-36-27-"+impl+".jsonl", `{"type":"session_meta","payload":{"cwd":"`+codexCwd+`"}}
{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"high"}}
`)
	writeFile(t, dir, "rollout-2026-10-09T12-10-00-"+gate+".jsonl", `{"type":"session_meta","payload":{"cwd":"`+codexCwd+`"}}
{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"medium"}}
`)
	if err := os.Chtimes(implPath, older, older); err != nil {
		t.Fatalf("Chtimes: %v", err)
	}
	if got := ReadProfile("codex", codexCwd, impl); got.Effort != "high" || !got.BySession || got.Shared {
		t.Errorf("codex by session: got %+v", got)
	}
	if got := ReadProfile("codex", codexCwd, ""); got.Effort != "medium" || !got.Shared {
		t.Errorf("codex fallback: got %+v, want the newest (gate), shared", got)
	}
}
