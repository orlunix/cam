// camc-win — the Windows-native camc: manages coding-agent sessions on a
// Windows node through wintmux's ConPTY daemon, speaking the camc CLI
// subset CAM Desktop drives over SSH. Stateless per invocation; state
// lives in %USERPROFILE%\.cam (agents.json + sockets\).
package main

import (
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"camc-win/internal/camcwin"
	"camc-win/internal/cli"
	"camc-win/internal/daemon"
)

func main() {
	args := os.Args[1:]

	// Hidden daemon mode (spawned by camc-win run; same argv shape as
	// wintmux's daemon re-exec).
	for _, a := range args {
		if a == "--daemon" {
			runDaemon(args)
			return
		}
	}

	// Global flags.
	jsonOut := false
	var rest []string
	for _, a := range args {
		switch a {
		case "--json":
			jsonOut = true
		case "-h", "--help":
			usage(0)
		default:
			rest = append(rest, a)
		}
	}

	if len(rest) == 0 {
		usage(1)
	}

	verb := rest[0]
	argv := rest[1:]

	code := 0
	switch verb {
	case "version", "-V":
		code = camcwin.CmdVersion()
	case "list", "ls":
		code = camcwin.CmdList(jsonOut)
	case "status":
		code = camcwin.CmdStatus(positional(argv, 0, "status requires an agent id"), jsonOut)
	case "run":
		code = cmdRun(argv)
	case "capture":
		code = cmdCapture(argv, jsonOut)
	case "send":
		code = cmdSend(argv)
	case "key":
		code = cmdKey(argv)
	case "stop":
		code = camcwin.CmdStop(positional(argv, 0, "stop requires an agent id"))
	case "kill":
		code = camcwin.CmdKill(positional(argv, 0, "kill requires an agent id"))
	case "rm":
		code = cmdRm(argv)
	case "attach":
		code = camcwin.CmdAttach(positional(argv, 0, "attach requires an agent id"))
	case "msg":
		code = cmdMsg(argv)
	case "cron":
		code = cmdCron(argv, jsonOut)
	default:
		fmt.Fprintf(os.Stderr, "camc-win: unknown command '%s'\n", verb)
		usage(1)
	}
	os.Exit(code)
}

// positional extracts argv[i] or exits with a usage error (argparse
// parity: exit 2).
func positional(argv []string, i int, errMsg string) string {
	var pos []string
	for _, a := range argv {
		if !strings.HasPrefix(a, "-") {
			pos = append(pos, a)
		}
	}
	// Note: positional() is only used by verbs whose argv is a bare ref
	// (status/stop/kill/attach); flags with values would confuse it, so
	// those verbs take no value-flags.
	if len(pos) <= i {
		fmt.Fprintf(os.Stderr, "camc-win: error: %s\n", errMsg)
		os.Exit(2)
	}
	return pos[i]
}

func cmdRun(argv []string) int {
	opts := camcwin.RunOptions{}
	var promptParts []string
	for i := 0; i < len(argv); i++ {
		switch argv[i] {
		case "-t", "--tool":
			i++
			opts.Tool = valueArg(argv, i, "-t")
		case "-p", "--path":
			i++
			opts.Path = valueArg(argv, i, "-p")
		case "-n", "--name":
			i++
			opts.Name = valueArg(argv, i, "-n")
		case "--api", "--api-token":
			i++ // accepted, recorded nowhere: no API proxy on Windows v0
			_ = valueArg(argv, i, argv[i-1])
		case "--tool-launcher", "--isolate-tool-home":
			i++ // accepted, ignored: no launcher/isolation layer on Windows v0
			_ = valueArg(argv, i, argv[i-1])
		case "--no-default-api", "--auto-exit":
			if argv[i] == "--auto-exit" {
				opts.AutoExit = true
			}
		case "--":
			promptParts = append(promptParts, argv[i+1:]...)
			i = len(argv)
		default:
			promptParts = append(promptParts, argv[i])
		}
	}
	opts.Prompt = strings.Join(promptParts, " ")
	return camcwin.CmdRun(opts)
}

func cmdCapture(argv []string, jsonOut bool) int {
	ref := ""
	lines := 0
	for i := 0; i < len(argv); i++ {
		switch argv[i] {
		case "--lines":
			i++
			n, err := strconv.Atoi(valueArg(argv, i, "--lines"))
			if err != nil {
				fmt.Fprintf(os.Stderr, "camc-win: error: invalid --lines value\n")
				return 2
			}
			lines = n
		case "--format":
			i++
			f := valueArg(argv, i, "--format")
			if f == "ansi" {
				return camcwin.CmdCaptureAnsiRefused()
			}
			if f != "plain" {
				fmt.Fprintf(os.Stderr, "camc-win: error: --format must be plain or ansi\n")
				return 2
			}
		default:
			if ref == "" {
				ref = argv[i]
			}
		}
	}
	if ref == "" {
		fmt.Fprintln(os.Stderr, "capture requires an agent id")
		return 2
	}
	return camcwin.CmdCapture(ref, lines, jsonOut)
}

func cmdSend(argv []string) int {
	ref := ""
	text := ""
	haveText := false
	noEnter := false
	useStdin := false
	file := ""
	for i := 0; i < len(argv); i++ {
		switch argv[i] {
		case "-t", "--text":
			i++
			text = valueArg(argv, i, "--text")
			haveText = true
		case "--file":
			i++
			file = valueArg(argv, i, "--file")
		case "--stdin":
			useStdin = true
		case "--no-enter":
			noEnter = true
		default:
			if ref == "" {
				ref = argv[i]
			}
		}
	}
	if ref == "" {
		fmt.Fprintln(os.Stderr, "send requires an agent id")
		return 2
	}
	sources := 0
	if haveText {
		sources++
	}
	if file != "" {
		sources++
	}
	if useStdin {
		sources++
	}
	if sources != 1 {
		fmt.Fprintln(os.Stderr, "Provide exactly one of --text, --file, or --stdin")
		return 2
	}
	if file != "" {
		data, err := os.ReadFile(file)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Unable to read --file: %v\n", err)
			return 1
		}
		text = string(data)
	}
	if useStdin {
		data, err := io.ReadAll(os.Stdin)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Unable to read stdin: %v\n", err)
			return 1
		}
		text = string(data)
	}
	return camcwin.CmdSend(ref, text, noEnter)
}

func cmdKey(argv []string) int {
	ref := ""
	key := ""
	for i := 0; i < len(argv); i++ {
		switch argv[i] {
		case "--key":
			i++
			key = valueArg(argv, i, "--key")
		default:
			if ref == "" {
				ref = argv[i]
			} else if key == "" {
				key = argv[i]
			}
		}
	}
	if ref == "" || key == "" {
		fmt.Fprintln(os.Stderr, "usage: camc-win key <id> --key <key-name>")
		return 2
	}
	return camcwin.CmdKey(ref, key)
}

func cmdRm(argv []string) int {
	ref := ""
	archive := false
	for _, a := range argv {
		switch a {
		case "--archive":
			archive = true
		case "--kill":
			// deprecated no-op: rm always kills (camc parity)
		default:
			if ref == "" && !strings.HasPrefix(a, "-") {
				ref = a
			}
		}
	}
	if ref == "" {
		fmt.Fprintln(os.Stderr, "rm requires an agent id")
		return 2
	}
	return camcwin.CmdRm(ref, archive)
}

func cmdMsg(argv []string) int {
	if len(argv) == 0 || argv[0] != "send" {
		fmt.Fprintln(os.Stderr, "usage: camc-win msg send <id> -t <text> [--no-wait] [--timeout N]")
		return 2
	}
	ref := ""
	text := ""
	noWait := false
	timeout := 600
	for i := 1; i < len(argv); i++ {
		switch argv[i] {
		case "-t", "--text":
			i++
			text = valueArg(argv, i, "-t")
		case "--no-wait":
			noWait = true
		case "--timeout":
			i++
			if n, err := strconv.Atoi(valueArg(argv, i, "--timeout")); err == nil {
				timeout = n
			}
		default:
			if ref == "" && !strings.HasPrefix(argv[i], "-") {
				ref = argv[i]
			}
		}
	}
	if ref == "" || text == "" {
		fmt.Fprintln(os.Stderr, "usage: camc-win msg send <id> -t <text> [--no-wait] [--timeout N]")
		return 2
	}
	return camcwin.CmdMsgSend(ref, text, noWait, timeout)
}

func cmdCron(argv []string, jsonOut bool) int {
	if len(argv) > 0 && argv[0] == "list" {
		return camcwin.CmdCronList(jsonOut)
	}
	fmt.Fprintln(os.Stderr, "camc-win: cron is not supported on Windows yet")
	return 1
}

// valueArg returns argv[i] or exits with a usage error when missing.
func valueArg(argv []string, i int, flag string) string {
	if i >= len(argv) {
		fmt.Fprintf(os.Stderr, "camc-win: error: %s requires a value\n", flag)
		os.Exit(2)
	}
	return argv[i]
}

// runDaemon executes the hidden --daemon mode: same argv shape the
// wintmux spawner produces.
func runDaemon(args []string) {
	cmd, err := cli.Parse(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "camc-win daemon: %v\n", err)
		os.Exit(1)
	}
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
		fmt.Fprintf(os.Stderr, "camc-win daemon: %v\n", err)
		os.Exit(1)
	}
}

func usage(code int) {
	fmt.Fprintf(os.Stderr, `camc-win %s — Windows-native camc (wintmux/ConPTY backend)

Usage: camc-win [--json] <command> [args]

Agent commands:
  run -t <tool> -p <path> [-n name] [prompt]   Start an agent session
  list                                         List agents
  status <id>                                  Show one agent
  capture <id> [--lines N] [--format plain]    Capture output (default: full scrollback)
  send <id> (-t text|--file f|--stdin) [--no-enter]  Send input
  key <id> --key <K>                           Send a special key (Enter, C-c, ...)
  attach <id>                                  Attach terminal to the session
  stop <id>                                    Mark stopped (session left alive)
  kill <id>                                    Tear down the session
  rm <id> [--archive]                          Kill + remove the record
  msg send <id> -t <text> [--no-wait]          Message an agent
  cron list                                    (stub: always empty)
  version                                      Print version

Environment:
  CAM_HOME                 state dir override (default ~\.cam)
  CAMC_WIN_STARTUP_WAIT    seconds before prompt delivery (default 2)
`, camcwin.Version)
	os.Exit(code)
}
