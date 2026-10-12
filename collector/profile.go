package collector

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Profile is what an agent is running, read from its own session files.
type Profile struct {
	Model  string `json:"model,omitempty"`
	Effort string `json:"effort,omitempty"`
	Fast   bool   `json:"fast"`
	// Source is the session file the profile was read from.
	Source string `json:"source"`
	// Shared is true when other sessions use the same directory, so the
	// match may belong to a different agent.
	Shared bool `json:"shared"`
	// BySession is true when the file was picked by the agent's own session
	// id (herdr's agent_session), so it can't belong to a neighbour.
	BySession bool   `json:"by_session,omitempty"`
	Tier      string `json:"tier,omitempty"`
	// ActiveAt is the session file's mtime (unix seconds): the agent's last activity.
	ActiveAt  int64      `json:"active_at,omitempty"`
	Subagents []Subagent `json:"subagents,omitempty"`
}

// Subagent is an in-process subagent (Claude Code's Agent tool).
type Subagent struct {
	ID          string `json:"id"`
	Description string `json:"description,omitempty"`
	Type        string `json:"type,omitempty"`
	Depth       int    `json:"depth"`
	Model       string `json:"model,omitempty"`
	Parent      string `json:"parent,omitempty"`
	Running     bool   `json:"running"`
}

const (
	sessionReadLimit  = 8_000_000 // bytes read backwards from a session file at most
	subagentReadLimit = 400_000
	subagentRecent    = 30 * time.Minute
	subagentActive    = 90 * time.Second
	maxSubagents      = 12
	codexScan         = 60 // newest Codex rollouts checked for a matching cwd
	profileWorkers    = 8  // profiles read at once
)

// sessionRE matches the session ids herdr reports; it also keeps glob
// characters out of the patterns built from them.
var sessionRE = regexp.MustCompile(`^[0-9A-Fa-f][0-9A-Fa-f-]{7,63}$`)

// ReadProfile returns the profile of a "codex" or "claude" agent working in
// cwd, or nil when no session file matches. session is the agent's own
// session id (herdr's agent_session.value) when known: its file is read
// directly; otherwise the newest session for cwd is used.
func ReadProfile(kind, cwd, session string) *Profile {
	return new(codexIndex).profile(kind, cwd, session)
}

// ProfileRequest is one agent to read a profile for.
type ProfileRequest struct{ Kind, Cwd, Session string }

// ReadProfiles is ReadProfile for each request, in order. It lists the Codex
// sessions once for all of them and reads up to profileWorkers at a time.
func ReadProfiles(reqs []ProfileRequest) []*Profile {
	out := make([]*Profile, len(reqs))
	x := new(codexIndex)
	next := make(chan int)
	var wg sync.WaitGroup
	for range min(profileWorkers, len(reqs)) {
		wg.Go(func() {
			for i := range next {
				out[i] = x.profile(reqs[i].Kind, reqs[i].Cwd, reqs[i].Session)
			}
		})
	}
	for i := range reqs {
		next <- i
	}
	close(next)
	wg.Wait()
	return out
}

func (x *codexIndex) profile(kind, cwd, session string) *Profile {
	if cwd == "" {
		return nil
	}
	switch kind {
	case "codex":
		return x.codexProfile(cwd, session)
	case "claude":
		return x.claudeProfile(cwd, session)
	}
	return nil
}

// ownSessionFile is the agent's own session file when its id is known and the
// file exists, else "".
func (x *codexIndex) ownSessionFile(kind, cwd, session string) string {
	if !sessionRE.MatchString(session) {
		return ""
	}
	if kind == "codex" {
		if hits := x.bySession(session); len(hits) > 0 {
			return hits[0]
		}
		return ""
	}
	path := filepath.Join(claudeDir(cwd), session+".jsonl")
	if st, err := os.Stat(path); err == nil && st.Mode().IsRegular() {
		return path
	}
	return ""
}

func claudeDir(cwd string) string {
	return filepath.Join(home(), ".claude", "projects", strings.NewReplacer("/", "-", ".", "-").Replace(cwd))
}

// codexProfile reads the agent's rollout (by session id, else the newest
// whose session_meta.cwd is cwd): the last turn_context gives model and
// effort, and any service_tier other than default/standard/auto/flex means
// fast mode.
func (x *codexIndex) codexProfile(cwd, session string) *Profile {
	var hits []string
	own := x.ownSessionFile("codex", cwd, session)
	if own != "" {
		hits = []string{own}
	} else {
		for _, f := range x.newestRollouts() {
			var meta struct {
				Payload struct {
					Cwd string `json:"cwd"`
				} `json:"payload"`
			}
			if firstLineJSON(f, &meta) == nil && meta.Payload.Cwd == cwd {
				hits = append(hits, f)
			}
		}
	}
	if len(hits) == 0 {
		return nil
	}
	p := &Profile{Source: hits[0], Shared: len(hits) > 1, BySession: own != "", ActiveAt: mtime(hits[0])}
	var haveContext, haveTier bool
	reverseLines(hits[0], sessionReadLimit, func(line string) bool {
		if !haveContext && strings.Contains(line, `"turn_context"`) {
			var r struct {
				Payload struct {
					Model  string `json:"model"`
					Effort string `json:"effort"`
				} `json:"payload"`
			}
			if json.Unmarshal([]byte(line), &r) == nil {
				p.Model, p.Effort, haveContext = r.Payload.Model, r.Payload.Effort, true
			}
		}
		if !haveTier && strings.Contains(line, `"service_tier"`) {
			var r any
			if json.Unmarshal([]byte(line), &r) == nil {
				if tier, ok := findString(r, "service_tier"); ok {
					p.Tier, haveTier = tier, true
				}
			}
		}
		return !(haveContext && haveTier)
	})
	switch p.Tier {
	case "", "default", "standard", "auto", "flex":
	default:
		p.Fast = true
	}
	return p
}

// claudeProfile reads the agent's transcript in ~/.claude/projects/<cwd with
// "/" and "." as "-"> (<session>.jsonl, else the newest): the last assistant
// entry's model, effort and usage.speed, plus the session's in-process
// subagents.
func (x *codexIndex) claudeProfile(cwd, session string) *Profile {
	files := []string{}
	own := x.ownSessionFile("claude", cwd, session)
	if own != "" {
		files = append(files, own)
	} else {
		files = newestFirst(glob(filepath.Join(claudeDir(cwd), "*.jsonl")), 0)
	}
	if len(files) == 0 {
		return nil
	}
	p := &Profile{Source: files[0], Shared: len(files) > 1, BySession: own != "", ActiveAt: mtime(files[0])}
	reverseLines(files[0], sessionReadLimit, func(line string) bool {
		if !strings.Contains(line, `"assistant"`) {
			return true
		}
		var r struct {
			Type          string `json:"type"`
			Effort        string `json:"effort"`
			PerTurnEffort string `json:"perTurnEffort"`
			Message       struct {
				Model string `json:"model"`
				Usage struct {
					Speed string `json:"speed"`
				} `json:"usage"`
			} `json:"message"`
		}
		if json.Unmarshal([]byte(line), &r) != nil || r.Type != "assistant" || r.Message.Model == "" {
			return true
		}
		p.Model = r.Message.Model
		p.Effort = r.Effort
		if p.Effort == "" {
			p.Effort = r.PerTurnEffort
		}
		p.Fast = r.Message.Usage.Speed == "fast"
		return false
	})
	p.Subagents = claudeSubagents(files[0], time.Now())
	return p
}

// claudeSubagents lists the subagents Claude Code recorded next to session
// (<session>/subagents/agent-<id>.jsonl plus .meta.json), newest first. Only
// ones written in the last 30 minutes are shown; running means written in the
// last 90 seconds.
func claudeSubagents(session string, now time.Time) []Subagent {
	dir := filepath.Join(strings.TrimSuffix(session, ".jsonl"), "subagents")
	var out []Subagent
	for _, metaPath := range newestFirst(glob(filepath.Join(dir, "*.meta.json")), 0) {
		transcript := strings.TrimSuffix(metaPath, ".meta.json") + ".jsonl"
		st, err := os.Stat(transcript)
		if err != nil || now.Sub(st.ModTime()) > subagentRecent {
			continue
		}
		b, err := os.ReadFile(metaPath)
		if err != nil {
			continue
		}
		var meta struct {
			AgentType     string `json:"agentType"`
			Description   string `json:"description"`
			SpawnDepth    int    `json:"spawnDepth"`
			ParentAgentID string `json:"parentAgentId"`
			ParentID      string `json:"parentId"`
		}
		if json.Unmarshal(b, &meta) != nil {
			continue
		}
		s := Subagent{
			ID:          strings.TrimSuffix(strings.TrimPrefix(filepath.Base(transcript), "agent-"), ".jsonl"),
			Description: meta.Description,
			Type:        meta.AgentType,
			Depth:       max(meta.SpawnDepth, 1),
			Parent:      meta.ParentAgentID,
			Running:     now.Sub(st.ModTime()) < subagentActive,
		}
		if s.Parent == "" {
			s.Parent = meta.ParentID
		}
		reverseLines(transcript, subagentReadLimit, func(line string) bool {
			if !strings.Contains(line, `"model"`) {
				return true
			}
			var r struct {
				Message struct {
					Model string `json:"model"`
				} `json:"message"`
			}
			if json.Unmarshal([]byte(line), &r) == nil && r.Message.Model != "" {
				s.Model = r.Message.Model
				return false
			}
			return true
		})
		out = append(out, s)
		if len(out) >= maxSubagents {
			break
		}
	}
	return out
}

// mtime is a file's modification time in unix seconds, 0 when unreadable.
func mtime(path string) int64 {
	if st, err := os.Stat(path); err == nil {
		return st.ModTime().Unix()
	}
	return 0
}
