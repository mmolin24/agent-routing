// Command agent-collect prints the data behind the agent-routing views.
//
// Usage:
//
//	agent-collect probe '<json>'      on a machine: its agents, pane tags,
//	                                  agent profiles and load
//	agent-collect snapshot < req.json all reachable machines, in parallel:
//	                                  {report, queues, capacity}
//	agent-collect version
//
// route.py --snapshot/--status/--watch/--dashboard call `snapshot` when this
// binary is on PATH (or in ~/.local/bin) and fall back to Python otherwise.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"

	"github.com/mmolin24/agent-routing/collector"
)

const version = "agent-collect 1"

func main() {
	// Every child (docker, herdr, git) inherits a disabled session bus unless a real one
	// is set: on WSL, D-Bus clients otherwise autolaunch a dbus-daemon that never exits.
	if os.Getenv("DBUS_SESSION_BUS_ADDRESS") == "" {
		os.Setenv("DBUS_SESSION_BUS_ADDRESS", "disabled:")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if err := run(ctx, os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "agent-collect:", err)
		os.Exit(2)
	}
}

func run(ctx context.Context, args []string, stdin io.Reader, stdout io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("missing command; use probe, snapshot or version")
	}
	var out any
	switch args[0] {
	case "probe":
		raw := []byte{}
		if len(args) > 1 {
			raw = []byte(args[1])
		} else {
			b, err := io.ReadAll(stdin)
			if err != nil {
				return fmt.Errorf("reading probe request: %w", err)
			}
			raw = b
		}
		var req collector.ProbeRequest
		if err := json.Unmarshal(raw, &req); err != nil {
			return fmt.Errorf("probe request is not valid JSON: %w", err)
		}
		r, err := collector.Probe(ctx, req)
		if err != nil {
			return err
		}
		out = r
	case "snapshot":
		var req collector.SnapshotRequest
		if err := json.NewDecoder(stdin).Decode(&req); err != nil {
			return fmt.Errorf("snapshot request on stdin is not valid JSON: %w", err)
		}
		r, err := collector.Snapshot(ctx, req)
		if err != nil {
			return err
		}
		out = r
	case "version":
		_, err := fmt.Fprintln(stdout, version)
		return err
	default:
		return fmt.Errorf("unknown command %q; use probe, snapshot or version", args[0])
	}
	enc := json.NewEncoder(stdout)
	enc.SetEscapeHTML(false)
	return enc.Encode(out)
}
