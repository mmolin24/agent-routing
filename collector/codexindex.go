package collector

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
)

// codexIndex lists ~/.codex/sessions/*/*/*/rollout-*.jsonl once and answers
// every agent's lookup from that list. A Mac with thousands of rollouts spent
// ~29 ms per agent in filepath.Glob, so a probe of 140 agents took 4 s. The
// list matches filepath.Glob exactly: same names (dot entries included), same
// order (each directory sorted), directories at the first three levels only.
// It is filled on first use and safe for concurrent use. Within one probe it
// may miss a second rollout a resumed session starts after the listing; the
// listing is at most one probe (~0.3 s) old.
type codexIndex struct {
	once   sync.Once
	all    []string // every rollout, in filepath.Glob order
	newest sync.Once
	recent []string // all, newest first, at most codexScan
}

func (x *codexIndex) rollouts() []string {
	x.once.Do(func() {
		dirs := []string{filepath.Join(home(), ".codex", "sessions")}
		for range 3 {
			var next []string
			for _, d := range dirs {
				for _, n := range globNames(d) {
					next = append(next, filepath.Join(d, n))
				}
			}
			dirs = next
		}
		for _, d := range dirs {
			for _, n := range globNames(d) {
				if isRollout(n) {
					x.all = append(x.all, filepath.Join(d, n))
				}
			}
		}
	})
	return x.all
}

// bySession is the glob rollout-*-<session>.jsonl, newest first. A session
// missing from the list (its rollout may have been created after the list was
// made) is looked up again in a fresh listing.
func (x *codexIndex) bySession(session string) []string {
	hits := x.fromIndex(session)
	if len(hits) == 0 {
		hits = new(codexIndex).fromIndex(session)
	}
	return newestFirst(hits, 0)
}

// fromIndex is the listed rollouts matching rollout-*-<session>.jsonl, in
// list order. sessionRE keeps glob metacharacters out of session.
func (x *codexIndex) fromIndex(session string) []string {
	suffix := "-" + session + ".jsonl"
	var hits []string
	for _, p := range x.rollouts() {
		n := filepath.Base(p)
		if strings.HasSuffix(n, suffix) && len(n) >= len("rollout-")+len(suffix) {
			hits = append(hits, p)
		}
	}
	return hits
}

// newestRollouts is every rollout newest first, at most codexScan.
func (x *codexIndex) newestRollouts() []string {
	x.newest.Do(func() { x.recent = newestFirst(x.rollouts(), codexScan) })
	return x.recent
}

// isRollout is filepath.Match("rollout-*.jsonl", name).
func isRollout(name string) bool {
	return strings.HasPrefix(name, "rollout-") && strings.HasSuffix(name, ".jsonl") &&
		len(name) >= len("rollout-.jsonl")
}

// globNames is the sorted entries of dir, or nil when dir isn't a readable
// directory: what filepath.Glob reads for a "*" component.
func globNames(dir string) []string {
	if st, err := os.Stat(dir); err != nil || !st.IsDir() {
		return nil
	}
	f, err := os.Open(dir)
	if err != nil {
		return nil
	}
	defer f.Close()
	names, _ := f.Readdirnames(-1)
	slices.Sort(names)
	return names
}
