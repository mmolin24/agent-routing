package collector

import (
	"context"
	"encoding/json"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/go-cmp/cmp"
)

// TestReadTreeGit runs against the layout tests/fixtures_gitstate.sh builds, with
// the same expectations as tests/worktree_git.py, so both readers emit the same JSON.
func TestReadTreeGit(t *testing.T) {
	dir := t.TempDir()
	if out, err := exec.Command(filepath.Join("..", "tests", "fixtures_gitstate.sh"), dir).CombinedOutput(); err != nil {
		t.Fatalf("fixture: %v\n%s", err, out)
	}
	tests := []struct {
		name, path, branch, want string
	}{
		{"pushed branch", "wt-pushed", "feature/APP-1",
			`{"dirty":3,"stash":1,"unpushed":2,"pushed":true,"oldest_unpushed":1000000200}`},
		{"never pushed, rebased onto a newer main", "wt-never", "feature/APP-2",
			`{"dirty":0,"stash":1,"unpushed":1,"pushed":false,"oldest_unpushed":1000000500}`},
		{"missing worktree", "gone", "feature/APP-3",
			`{"dirty":0,"stash":0,"unpushed":0,"pushed":false,"error":"no worktree"}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			g := ReadTreeGit(context.Background(), TreeRequest{Task: "t", Path: filepath.Join(dir, tt.path), Branch: tt.branch})
			b, _ := json.Marshal(g)
			var got, want map[string]any
			json.Unmarshal(b, &got)
			json.Unmarshal([]byte(tt.want), &want)
			if diff := cmp.Diff(want, got); diff != "" {
				t.Errorf("ReadTreeGit mismatch (-want +got):\n%s", diff)
			}
		})
	}
}

func TestReadTreesBudget(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // budget already spent
	got := ReadTrees(ctx, []TreeRequest{{Task: "a", Path: t.TempDir(), Branch: "b"}})
	if got["a"] == nil || got["a"].Error != "timeout" {
		t.Errorf("ReadTrees with no time left = %+v, want error timeout", got["a"])
	}
}

// A timeout after git status must be an error, never false zeros.
func TestReadTreeGitTimeoutMidway(t *testing.T) {
	real, cap := runGit, gitCmd
	defer func() { runGit, gitCmd = real, cap }()
	gitCmd = 50 * time.Millisecond
	calls := 0
	runGit = func(ctx context.Context, dir string, args ...string) ([]byte, error) {
		if calls++; calls > 1 {
			<-ctx.Done() // the command outlives its time
			return nil, ctx.Err()
		}
		return []byte(" M f\n"), nil
	}
	got := ReadTreeGit(context.Background(), TreeRequest{Task: "t", Path: t.TempDir(), Branch: "b"})
	if got.Error != "timeout" || got.Dirty != 0 || got.Unpushed != 0 {
		t.Errorf("ReadTreeGit with a timeout after status = %+v, want only error timeout", got)
	}
}
