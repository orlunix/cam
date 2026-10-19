package cli

import (
	"fmt"
	"strconv"
	"strings"
)

// CommandType identifies which tmux subcommand was parsed.
type CommandType int

const (
	CmdNewSession CommandType = iota
	CmdSendKeys
	CmdCapturePane
	CmdHasSession
	CmdKillSession
	CmdSetOption
	CmdPipePane
	CmdAttach
	CmdListSessions
)

// Command holds all parsed arguments for a single wintmux invocation.
// Fields are populated based on the CommandType.
type Command struct {
	Type       CommandType
	SocketPath string

	// new-session flags
	Detached    bool
	SessionName string
	WindowName  string
	StartDir    string
	ShellCmd    string
	Cols        int
	Rows        int

	// send-keys flags
	Target  string
	Keys    []string
	Literal bool

	// capture-pane flags
	Print     bool
	JoinLines bool
	Alternate bool
	StartLine int

	// set-option fields
	Option string
	Value  string

	// pipe-pane field
	PipeCmd string

	// internal: daemon mode
	DaemonMode bool
}

// Parse converts a tmux-style argument list into a Command struct.
// Expected format: [-S socket] [--daemon] command [command-flags] [args...]
func Parse(args []string) (*Command, error) {
	if len(args) == 0 {
		return nil, fmt.Errorf("no command specified")
	}

	cmd := &Command{}
	i := 0

	// Parse global flags preceding the subcommand.
	for i < len(args) {
		switch args[i] {
		case "-S":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-S requires an argument")
			}
			cmd.SocketPath = args[i]
			i++
		case "--daemon":
			cmd.DaemonMode = true
			i++
		case "-u":
			// tmux -u enables UTF-8 mode; wintmux is always UTF-8 -- silently ignore.
			i++
		default:
			goto parseCommand
		}
	}

parseCommand:
	if i >= len(args) {
		if cmd.DaemonMode {
			cmd.Type = CmdNewSession
			return cmd, nil
		}
		return nil, fmt.Errorf("no command specified")
	}

	subcommand := args[i]
	i++
	remaining := args[i:]

	switch subcommand {
	case "new-session":
		return parseNewSession(cmd, remaining)
	case "send-keys":
		return parseSendKeys(cmd, remaining)
	case "capture-pane":
		return parseCapturePane(cmd, remaining)
	case "has-session":
		return parseHasSession(cmd, remaining)
	case "kill-session":
		return parseKillSession(cmd, remaining)
	case "set-option":
		return parseSetOption(cmd, remaining)
	case "pipe-pane":
		return parsePipePane(cmd, remaining)
	case "attach", "attach-session":
		return parseAttach(cmd, remaining)
	case "list-sessions", "ls":
		cmd.Type = CmdListSessions
		return cmd, nil
	default:
		return nil, fmt.Errorf("unknown command: %s", subcommand)
	}
}

func parseNewSession(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdNewSession
	i := 0
	for i < len(args) {
		switch args[i] {
		case "-d":
			cmd.Detached = true
			i++
		case "-s":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-s requires a session name")
			}
			cmd.SessionName = args[i]
			i++
		case "-n":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-n requires a window name")
			}
			cmd.WindowName = args[i]
			i++
		case "-c":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-c requires a directory")
			}
			cmd.StartDir = args[i]
			i++
		case "-x":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-x requires a column count")
			}
			n, err := strconv.Atoi(args[i])
			if err != nil || n <= 0 {
				return nil, fmt.Errorf("invalid -x value %q", args[i])
			}
			cmd.Cols = n
			i++
		case "-y":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-y requires a row count")
			}
			n, err := strconv.Atoi(args[i])
			if err != nil || n <= 0 {
				return nil, fmt.Errorf("invalid -y value %q", args[i])
			}
			cmd.Rows = n
			i++
		default:
			cmd.ShellCmd = strings.Join(args[i:], " ")
			i = len(args)
		}
	}
	return cmd, nil
}

func parseSendKeys(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdSendKeys
	i := 0
	pastOptions := false

	for i < len(args) {
		if pastOptions {
			cmd.Keys = append(cmd.Keys, args[i])
			i++
			continue
		}
		switch args[i] {
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		case "-l":
			cmd.Literal = true
			i++
		case "--":
			pastOptions = true
			i++
		default:
			cmd.Keys = append(cmd.Keys, args[i])
			i++
		}
	}
	return cmd, nil
}

func parseCapturePane(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdCapturePane
	i := 0
	for i < len(args) {
		switch args[i] {
		case "-p":
			cmd.Print = true
			i++
		case "-J":
			cmd.JoinLines = true
			i++
		case "-a":
			cmd.Alternate = true
			i++
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		case "-S":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("capture-pane -S requires a line number")
			}
			if args[i] == "-" {
				// tmux `-S -` = full scrollback
				cmd.StartLine = -1 << 30
				i++
				continue
			}
			n, err := strconv.Atoi(args[i])
			if err != nil {
				return nil, fmt.Errorf("invalid start line %q: %w", args[i], err)
			}
			cmd.StartLine = n
			i++
		default:
			return nil, fmt.Errorf("unknown capture-pane flag: %s", args[i])
		}
	}
	return cmd, nil
}

func parseHasSession(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdHasSession
	for i := 0; i < len(args); {
		switch args[i] {
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		default:
			return nil, fmt.Errorf("unknown has-session flag: %s", args[i])
		}
	}
	return cmd, nil
}

func parseKillSession(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdKillSession
	for i := 0; i < len(args); {
		switch args[i] {
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		default:
			return nil, fmt.Errorf("unknown kill-session flag: %s", args[i])
		}
	}
	return cmd, nil
}

func parseSetOption(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdSetOption
	i := 0
	for i < len(args) {
		switch args[i] {
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		default:
			if i+1 < len(args) {
				cmd.Option = args[i]
				cmd.Value = args[i+1]
				i += 2
			} else {
				cmd.Option = args[i]
				i++
			}
		}
	}
	return cmd, nil
}

func parsePipePane(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdPipePane
	i := 0
	for i < len(args) {
		switch args[i] {
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		default:
			cmd.PipeCmd = strings.Join(args[i:], " ")
			i = len(args)
		}
	}
	return cmd, nil
}

func parseAttach(cmd *Command, args []string) (*Command, error) {
	cmd.Type = CmdAttach
	for i := 0; i < len(args); {
		switch args[i] {
		case "-t":
			i++
			if i >= len(args) {
				return nil, fmt.Errorf("-t requires a target")
			}
			cmd.Target = args[i]
			i++
		default:
			return nil, fmt.Errorf("unknown attach flag: %s", args[i])
		}
	}
	return cmd, nil
}
