// Package collector gathers the data behind the agent-routing views: which
// agents run on each machine, what model and effort they use, the agents and
// subagents they started, each machine's load, and the routed tasks, queues
// and capacity from the router's records.
//
// It produces exactly the JSON that `scripts/route.py --snapshot` produces, so
// the Python views (route.py --watch, --status, --dashboard) can draw either.
// Routing and dispatch stay in Python; this package only reads.
//
// Two entry points:
//
//   - [Probe] runs on one machine and reports its agents, pane tags, agent
//     profiles and load.
//   - [Snapshot] probes every reachable machine in parallel (in-process for
//     the local one, one SSH call for the other) and assembles the full view.
//
// Per-machine failures are part of the data: they are reported in
// [Report.Unreachable] instead of failing the whole snapshot.
package collector
