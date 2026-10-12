package collector

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"time"
)

// ProbeRequest is what one machine is asked to report.
type ProbeRequest struct {
	// Herdr is the herdr binary on that machine; "$HOME" and "~/" are expanded.
	Herdr string `json:"herdr"`
	// Workspaces lists the routed tasks' Herdr workspaces on that machine,
	// whose panes are read for parent tokens.
	Workspaces []string `json:"workspaces"`
	// Trees lists the worktrees of tasks that may push (draft-pr), for their
	// unpushed, stash and dirty counts.
	Trees []TreeRequest `json:"trees,omitempty"`
}

// ProbeResult is one machine's live state. Agents, Tokens and Profiles keep
// the field names of `herdr agent list` and of the Python probe.
type ProbeResult struct {
	Agents   []Agent                      `json:"agents"`
	Tokens   map[string]map[string]string `json:"tokens"`
	Profiles []*Profile                   `json:"profiles"`
	Load     *Load                        `json:"load"`
	Heavy    *HeavySlots                  `json:"heavy,omitempty"` // scripts/heavy slots on this machine
	Usage    *Usage                       `json:"usage,omitempty"` // Codex / Claude plan usage seen here
	Git      map[string]*TreeGit          `json:"git,omitempty"`   // task id -> its worktree's git state
}

// Agent is one row of `herdr agent list`.
type Agent struct {
	PaneID      string `json:"pane_id"`
	Name        string `json:"name,omitempty"`
	Kind        string `json:"agent"`
	Status      string `json:"agent_status"`
	Cwd         string `json:"cwd"`
	WorkspaceID string `json:"workspace_id"`
	TabID       string `json:"tab_id,omitempty"`
	// Session is herdr's agent_session: Value is the agent's own session id.
	Session *AgentSession `json:"agent_session,omitempty"`
}

// AgentSession is herdr's reference to an agent's native session.
type AgentSession struct {
	Value string `json:"value"`
}

// ErrHerdr reports that herdr could not be queried on a machine.
var ErrHerdr = errors.New("herdr unavailable")

// Probe reports the local machine's agents, the tokens on the given
// workspaces' panes, each agent's profile (from its own session files) and
// the machine's load. It fails only if herdr itself can't be listed; errors
// wrap [ErrHerdr].
func Probe(ctx context.Context, req ProbeRequest) (*ProbeResult, error) {
	herdr := expandHome(req.Herdr)
	if herdr == "" {
		herdr = "herdr"
	}
	var list struct {
		Result struct {
			Agents []Agent `json:"agents"`
		} `json:"result"`
	}
	if err := herdrJSON(ctx, herdr, &list, "agent", "list"); err != nil {
		return nil, fmt.Errorf("%w: agent list: %v", ErrHerdr, err)
	}
	slots := ReadHeavySlots(heavyDir())
	usage := ReadUsage()
	res := &ProbeResult{
		Heavy:  &slots,
		Usage:  &usage,
		Agents: list.Result.Agents,
		Tokens: map[string]map[string]string{},
	}
	for _, ws := range req.Workspaces {
		var panes struct {
			Result struct {
				Panes []struct {
					PaneID string            `json:"pane_id"`
					Tokens map[string]string `json:"tokens"`
				} `json:"panes"`
			} `json:"result"`
		}
		if err := herdrJSON(ctx, herdr, &panes, "pane", "list", "--workspace", ws); err != nil {
			continue // a closed workspace only loses its parent links
		}
		for _, p := range panes.Result.Panes {
			if p.Tokens == nil {
				p.Tokens = map[string]string{}
			}
			res.Tokens[p.PaneID] = p.Tokens
		}
	}
	reqs := make([]ProfileRequest, len(res.Agents))
	for i, a := range res.Agents {
		reqs[i] = ProfileRequest{Kind: a.Kind, Cwd: a.Cwd}
		if a.Session != nil {
			reqs[i].Session = a.Session.Value
		}
	}
	res.Profiles = ReadProfiles(reqs)
	if len(req.Trees) > 0 {
		res.Git = ReadTrees(ctx, req.Trees)
	}
	res.Load = ReadLoad(ctx)
	return res, nil
}

// herdrJSON runs `herdr args...` and decodes its JSON output into v.
func herdrJSON(ctx context.Context, herdr string, v any, args ...string) error {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, herdr, args...).Output()
	if err != nil {
		return err
	}
	if len(bytes.TrimSpace(out)) == 0 {
		return errors.New("empty output")
	}
	return json.Unmarshal(out, v)
}
