# agent-collect

`agent-collect` reads the data behind the agent-routing views (`route.py --status`,
`--watch`, `--dashboard`): the agents on each machine, their model and effort, the
subagents they started, each machine's load, and the routed tasks, queues and
capacity. It only reads; routing and dispatch stay in Python.

```sh
agent-collect probe '<json>'        # one machine: agents, pane tags, profiles, load
agent-collect snapshot < req.json   # every reachable machine in parallel
agent-collect version
```

`route.py` uses it when it's on `PATH` (or in `~/.local/bin`) and falls back to the
Python readers otherwise; `AGENT_COLLECT=0` forces Python. Both produce identical
JSON, so the views draw either. Install it on each machine with
`GOBIN=~/.local/bin go install ./cmd/agent-collect`. Standard library only, plus
go-cmp in tests.

## Python and Go

The Python readers were fast enough. The Go collector was an experiment: how much
quicker would Go make the refresh? The views refresh from one probe per machine.
Times for one probe of the Mac, with about 140 agents and 7,700 Codex session files:

| Probe | Time | Why |
| --- | --- | --- |
| Python readers | 1.1–1.3 s | Each agent's profile searches the session tree once (~5 ms per search). |
| Go, first version | 4.3–4.8 s | The same search per agent, but Go's `filepath.Glob` took ~29 ms per search, so the port was slower than Python. |
| Go, now | 0.26–0.31 s | The session tree is listed once per probe (~4 ms) and profiles are read 8 at a time. The output is identical. |

Over SSH the round trip adds about 0.25 s. Tests check the listing against
`filepath.Glob` itself, so the faster path can't drift from what the search found.
