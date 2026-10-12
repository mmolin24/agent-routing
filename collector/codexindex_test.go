package collector

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
)

// codexTree writes rollouts that probe the edges of filepath.Glob: dot
// entries, a file where a directory is expected, non-rollout names, an empty
// "*" and sessions that are suffixes of other sessions.
func codexTree(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	s := filepath.Join(home, ".codex", "sessions")
	for _, rel := range []string{
		"2026/10/09/rollout-2026-10-09T11-36-27-deadbeef-0000-4000-8000-000000000001.jsonl",
		"2026/10/09/rollout-2026-10-09T12-00-00-dead-beef-0001.jsonl",
		"2026/10/09/rollout-2026-10-09T12-00-01-beef-0001.jsonl",
		"2026/10/09/rollout--beef-0002.jsonl",
		"2026/10/09/rollout-beef-0003.jsonl",
		"2026/10/09/notes.jsonl",
		"2026/10/09/rollout-x.json",
		"2026/10/10/rollout-2026-10-10T08-00-00-beef-0001.jsonl",
		"2026/.hidden/01/rollout-2026-10-11T00-00-00-beef-0004.jsonl",
		"2026/10/11/deeper/rollout-x-beef-0005.jsonl",
		"stray/10/rollout-x-beef-0006.jsonl",
	} {
		writeFile(t, s, rel, "{}\n")
	}
	writeFile(t, s, "2026/notadir", "")
	if err := os.MkdirAll(filepath.Join(s, "2026", "10", "12", "rollout-dir-beef-0007.jsonl"), 0o755); err != nil {
		t.Fatal(err)
	}
	return home
}

func TestCodexIndexMatchesGlob(t *testing.T) {
	codexTree(t)
	x := new(codexIndex)
	if diff := cmp.Diff(globHome(".codex/sessions/*/*/*/rollout-*.jsonl"), x.rollouts()); diff != "" {
		t.Errorf("rollouts mismatch with filepath.Glob (-glob +index):\n%s", diff)
	}
	for _, session := range []string{
		"deadbeef-0000-4000-8000-000000000001", "beef-0001", "dead-beef-0001", "0001",
		"beef-0002", "beef-0003", "beef-0004", "beef-0005", "beef-0006", "beef-0007", "ffffffff",
	} {
		want := globHome(".codex/sessions/*/*/*/rollout-*-" + session + ".jsonl")
		if diff := cmp.Diff(want, x.fromIndex(session)); diff != "" {
			t.Errorf("fromIndex(%q) mismatch with filepath.Glob (-glob +index):\n%s", session, diff)
		}
		if diff := cmp.Diff(newestFirst(want, 0), x.bySession(session)); diff != "" && len(want) < 2 {
			t.Errorf("bySession(%q) mismatch (-glob +index):\n%s", session, diff)
		}
	}
}

func TestCodexIndexFindsLateRollout(t *testing.T) {
	home := codexTree(t)
	x := new(codexIndex)
	x.rollouts()
	late := writeFile(t, filepath.Join(home, ".codex", "sessions", "2026", "10", "12"), "rollout-2026-10-12T09-00-00-beef-0008.jsonl", "{}\n")
	if got := x.fromIndex("beef-0008"); len(got) != 0 {
		t.Fatalf("fromIndex(late) = %v, want nothing: the listing predates it", got)
	}
	if got := x.bySession("beef-0008"); !cmp.Equal(got, []string{late}) {
		t.Errorf("bySession(late) = %v, want [%s]", got, late)
	}
}

func TestReadProfilesMatchesReadProfile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dir := filepath.Join(home, ".codex", "sessions", "2026", "10", "09")
	base := time.Now().Add(-time.Hour)
	var reqs []ProfileRequest
	for i := range 40 {
		cwd := fmt.Sprintf("/work/app/.worktrees/APP-%d", i%7)
		session := fmt.Sprintf("deadbeef-0000-4000-8000-%012d", i)
		path := writeFile(t, dir, "rollout-2026-10-09T11-36-27-"+session+".jsonl", `{"type":"session_meta","payload":{"cwd":"`+cwd+`"}}
{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"`+[]string{"low", "medium", "high"}[i%3]+`"}}
`)
		mod := base.Add(time.Duration(i) * time.Second)
		if err := os.Chtimes(path, mod, mod); err != nil {
			t.Fatal(err)
		}
		if i%4 == 0 {
			session = "" // by cwd: the newest rollout for it
		}
		reqs = append(reqs, ProfileRequest{Kind: "codex", Cwd: cwd, Session: session})
	}
	reqs = append(reqs, ProfileRequest{Kind: "codex", Cwd: "/no/such"}, ProfileRequest{Kind: "claude", Cwd: "/no/such"}, ProfileRequest{Kind: "shell", Cwd: "/x"}, ProfileRequest{Kind: "codex"})
	want := make([]*Profile, len(reqs))
	for i, r := range reqs {
		want[i] = ReadProfile(r.Kind, r.Cwd, r.Session)
	}
	if diff := cmp.Diff(want, ReadProfiles(reqs)); diff != "" {
		t.Errorf("ReadProfiles mismatch (-ReadProfile +ReadProfiles):\n%s", diff)
	}
	if got := ReadProfiles(nil); got == nil || len(got) != 0 {
		t.Errorf("ReadProfiles(nil) = %#v, want an empty non-nil slice (JSON [])", got)
	}
}
