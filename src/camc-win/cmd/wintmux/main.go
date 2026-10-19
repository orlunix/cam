package main

import (
	"fmt"
	"math"
	"os"
	"strings"
	"time"

	"camc-win/internal/attach"
	"camc-win/internal/cli"
	"camc-win/internal/daemon"
	"camc-win/internal/ipc"
	"camc-win/internal/spawn"
)

// version is overridable at build time: -ldflags "-X main.version=x.y.z"
var version = "0.2.0"

func main() {
	args := os.Args[1:]

	if len(args) == 0 {
		printUsage()
		os.Exit(1)
	}

	if args[0] == "-V" {
		fmt.Printf("wintmux %s\n", version)
		os.Exit(0)
	}

	cmd, err := cli.Parse(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "wintmux: %v\n", err)
		os.Exit(1)
	}

	if cmd.DaemonMode {
		runDaemon(cmd)
		return
	}

	os.Exit(execute(cmd))
}

func runDaemon(cmd *cli.Command) {
	workdir := cmd.StartDir
	if workdir == "" {
		workdir, _ = os.Getwd()
	}
	cols, rows := cmd.Cols, cmd.Rows
	if cols <= 0 {
		cols = 120
	}
	if rows <= 0 {
		rows = 40
	}
	if err := daemon.Run(cmd.SocketPath, cmd.SessionName, workdir, cmd.ShellCmd, cols, rows); err != nil {
		fmt.Fprintf(os.Stderr, "daemon error: %v\n", err)
		os.Exit(1)
	}
}

func execute(cmd *cli.Command) int {
	switch cmd.Type {
	case cli.CmdNewSession:
		return executeNewSession(cmd)
	case cli.CmdSendKeys:
		return executeSendKeys(cmd)
	case cli.CmdCapturePane:
		return executeCapturePane(cmd)
	case cli.CmdHasSession:
		return executeHasSession(cmd)
	case cli.CmdKillSession:
		return executeKillSession(cmd)
	case cli.CmdSetOption:
		return executeSetOption(cmd)
	case cli.CmdPipePane:
		return executePipePane(cmd)
	case cli.CmdAttach:
		return attach.Run(cmd.SocketPath)
	default:
		fmt.Fprintln(os.Stderr, "wintmux: command not implemented")
		return 1
	}
}

func executeNewSession(cmd *cli.Command) int {
	if err := spawn.Daemon(cmd.SocketPath, cmd.SessionName, cmd.StartDir, cmd.ShellCmd, cmd.Cols, cmd.Rows); err != nil {
		fmt.Fprintf(os.Stderr, "wintmux: failed to create session: %v\n", err)
		return 1
	}

	// Poll until the daemon is reachable (up to 5 seconds).
	for i := 0; i < 50; i++ {
		time.Sleep(100 * time.Millisecond)
		resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{Action: ipc.ActionPing})
		if err == nil && resp.OK {
			return 0
		}
	}

	fmt.Fprintln(os.Stderr, "wintmux: session created but daemon not responding")
	return 1
}

// specialKeys is the set of tmux key names that should be sent through
// the send_key action (interpreted) rather than send_keys (literal).
var specialKeys = map[string]bool{
	"Enter": true, "Escape": true, "BSpace": true,
	"Tab": true, "Space": true,
	"C-c": true, "C-d": true, "C-z": true,
	"Up": true, "Down": true, "Left": true, "Right": true,
	"Home": true, "End": true, "DC": true,
	"PageUp": true, "PageDown": true,
}

func executeSendKeys(cmd *cli.Command) int {
	if cmd.Literal {
		text := strings.Join(cmd.Keys, " ")
		resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{
			Action:  ipc.ActionSendKeys,
			Text:    text,
			Literal: true,
		})
		if err != nil {
			fmt.Fprintf(os.Stderr, "wintmux: %v\n", err)
			return 1
		}
		if !resp.OK {
			fmt.Fprintf(os.Stderr, "wintmux: %s\n", resp.Error)
			return 1
		}
		return 0
	}

	for _, key := range cmd.Keys {
		var req ipc.Request
		if specialKeys[key] {
			req = ipc.Request{Action: ipc.ActionSendKey, Key: key}
		} else {
			req = ipc.Request{Action: ipc.ActionSendKeys, Text: key}
		}
		resp, err := ipc.SendRequest(cmd.SocketPath, &req)
		if err != nil {
			fmt.Fprintf(os.Stderr, "wintmux: %v\n", err)
			return 1
		}
		if !resp.OK {
			fmt.Fprintf(os.Stderr, "wintmux: %s\n", resp.Error)
			return 1
		}
	}
	return 0
}

func executeCapturePane(cmd *cli.Command) int {
	// Daemon Lines semantics: <0 = full scrollback, 0 = visible screen
	// (tmux capture-pane default), >0 = last N rows.
	lines := 0
	if cmd.StartLine == -1<<30 {
		lines = -1 // `-S -` — full scrollback
	} else if cmd.StartLine < 0 {
		lines = int(math.Abs(float64(cmd.StartLine)))
	}

	resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{
		Action:    ipc.ActionCapture,
		Lines:     lines,
		Alternate: cmd.Alternate,
		Join:      cmd.JoinLines,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "wintmux: %v\n", err)
		return 1
	}
	if !resp.OK {
		fmt.Fprintf(os.Stderr, "wintmux: %s\n", resp.Error)
		return 1
	}

	if cmd.Print {
		fmt.Print(resp.Output)
		if !strings.HasSuffix(resp.Output, "\n") {
			fmt.Println()
		}
	}
	return 0
}

func executeHasSession(cmd *cli.Command) int {
	resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{
		Action: ipc.ActionHasSession,
	})
	if err != nil {
		return 1
	}
	if resp.Exists {
		return 0
	}
	return 1
}

func executeKillSession(cmd *cli.Command) int {
	resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{
		Action: ipc.ActionKillSession,
	})
	if err != nil {
		return 0
	}
	if !resp.OK {
		fmt.Fprintf(os.Stderr, "wintmux: %s\n", resp.Error)
		return 1
	}
	return 0
}

func executeSetOption(cmd *cli.Command) int {
	resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{
		Action: ipc.ActionSetOption,
		Option: cmd.Option,
		Value:  cmd.Value,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "wintmux: %v\n", err)
		return 1
	}
	if !resp.OK {
		fmt.Fprintf(os.Stderr, "wintmux: %s\n", resp.Error)
		return 1
	}
	return 0
}

func executePipePane(cmd *cli.Command) int {
	resp, err := ipc.SendRequest(cmd.SocketPath, &ipc.Request{
		Action:   ipc.ActionPipePane,
		ShellCmd: cmd.PipeCmd,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "wintmux: %v\n", err)
		return 1
	}
	if !resp.OK {
		fmt.Fprintf(os.Stderr, "wintmux: %s\n", resp.Error)
		return 1
	}
	return 0
}

func printUsage() {
	fmt.Fprintf(os.Stderr, `wintmux %s — Windows-native tmux-compatible session manager

Usage:
  wintmux [-S socket-path] command [flags]

Commands:
  new-session    Create a new session
  send-keys      Send keys to a session
  capture-pane   Capture pane output
  has-session    Check if a session exists
  kill-session   Kill a session
  set-option     Set a session option
  pipe-pane      Pipe pane output to a file
  attach         Attach to a session (not yet implemented)

Flags:
  -S path        Socket path (session identification)
  -V             Show version
`, version)
}
