package collector

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// TreeRequest asks a machine for the git state of one worker's worktree.
type TreeRequest struct {
	Task   string `json:"task"`
	Path   string `json:"path"`
	Branch string `json:"branch"`
}

const gitBudget = 10 * time.Second // all worktrees' git together, so it never sinks the whole probe

var gitCmd = 5 * time.Second // one git command; a variable for tests

// TreeGit is the work a worker hasn't pushed yet. Mirrors probe.py's tree_git.
type TreeGit struct {
	Dirty          int    `json:"dirty"`
	Stash          int    `json:"stash"` // stashes made on this branch (they're shared repo-wide)
	Unpushed       int    `json:"unpushed"`
	Pushed         bool   `json:"pushed"` // origin/<branch> exists
	OldestUnpushed int64  `json:"oldest_unpushed,omitempty"`
	Error          string `json:"error,omitempty"`
}

// ReadTrees reads every tree's git state within one shared time budget; trees
// left when it runs out report "timeout".
func ReadTrees(ctx context.Context, trees []TreeRequest) map[string]*TreeGit {
	ctx, cancel := context.WithTimeout(ctx, gitBudget)
	defer cancel()
	out := map[string]*TreeGit{}
	for _, t := range trees {
		out[t.Task] = ReadTreeGit(ctx, t)
	}
	return out
}

// ReadTreeGit reads a worktree's dirty, stash and unpushed counts, read-only:
// no optional locks (no index refresh writes), no fsmonitor, no lazy fetch.
// Unpushed counts commits on HEAD that no origin ref has (as of the last fetch
// or push), so main merged or rebased in doesn't count.
func ReadTreeGit(ctx context.Context, t TreeRequest) *TreeGit {
	if st, err := os.Stat(t.Path); err != nil || !st.IsDir() {
		return &TreeGit{Error: "no worktree"}
	}
	// Any failure or timeout makes the whole entry an error, never false zeros.
	dirty, err := gitLines(ctx, t.Path, "status", "--porcelain")
	if err != nil {
		return &TreeGit{Error: err.Error()}
	}
	_, err = gitLines(ctx, t.Path, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/"+t.Branch)
	if err != nil && err != errAbsent {
		return &TreeGit{Error: err.Error()}
	}
	g := &TreeGit{Dirty: len(dirty), Pushed: err == nil}
	times, err := gitLines(ctx, t.Path, "log", "--format=%ct", "HEAD", "--not", "--remotes=origin")
	if err != nil {
		return &TreeGit{Error: err.Error()}
	}
	g.Unpushed = len(times)
	for _, s := range times {
		if v, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64); err == nil && (g.OldestUnpushed == 0 || v < g.OldestUnpushed) {
			g.OldestUnpushed = v
		}
	}
	own := regexp.MustCompile(`^(?:WIP on|On) ` + regexp.QuoteMeta(t.Branch) + `:`)
	stashes, err := gitLines(ctx, t.Path, "stash", "list", "--format=%gs")
	if err != nil {
		return &TreeGit{Error: err.Error()}
	}
	for _, s := range stashes {
		if own.MatchString(s) {
			g.Stash++
		}
	}
	return g
}

var (
	errTimeout   = errors.New("timeout")
	errGitFailed = errors.New("git failed")
	errAbsent    = errors.New("absent") // exit 1, e.g. rev-parse --verify on a missing ref
)

// runGit runs git; a variable so tests can simulate a slow machine.
var runGit = func(ctx context.Context, dir string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-c", "core.fsmonitor=false", "--no-optional-locks", "-C", dir}, args...)...)
	cmd.Env = append(os.Environ(), "GIT_OPTIONAL_LOCKS=0", "GIT_NO_LAZY_FETCH=1")
	return cmd.Output()
}

// gitLines runs a read-only git command in dir and returns its non-blank lines,
// or errTimeout, errAbsent (exit 1) or errGitFailed.
func gitLines(ctx context.Context, dir string, args ...string) ([]string, error) {
	if ctx.Err() != nil {
		return nil, errTimeout
	}
	cctx, cancel := context.WithTimeout(ctx, gitCmd)
	defer cancel()
	out, err := runGit(cctx, dir, args...)
	if cctx.Err() != nil {
		return nil, errTimeout
	}
	var ee *exec.ExitError
	switch {
	case errors.As(err, &ee) && ee.ExitCode() == 1:
		return nil, errAbsent
	case err != nil:
		return nil, errGitFailed
	}
	var lines []string
	for _, l := range strings.Split(string(out), "\n") {
		if strings.TrimSpace(l) != "" {
			lines = append(lines, l)
		}
	}
	return lines, nil
}
