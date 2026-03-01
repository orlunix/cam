package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"runtime"
	"strconv"
	"strings"

	"github.com/openclaw/cam-agent/internal/fileops"
	"github.com/openclaw/cam-agent/internal/session"
)

var version = "0.1.0"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(1)
	}

	switch os.Args[1] {
	case "ping":
		cmdPing()
	case "session":
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "usage: cam-agent session <create|exists|kill|capture|send|key|log-start|log-read>")
			os.Exit(1)
		}
		cmdSession(os.Args[2], os.Args[3:])
	case "file":
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "usage: cam-agent file <list|read|write>")
			os.Exit(1)
		}
		cmdFile(os.Args[2], os.Args[3:])
	case "--version", "version":
		fmt.Println(version)
	case "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		usage()
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "cam-agent — CAM target agent")
	fmt.Fprintln(os.Stderr, "")
	fmt.Fprintln(os.Stderr, "Commands:")
	fmt.Fprintln(os.Stderr, "  ping                          Test connection + version")
	fmt.Fprintln(os.Stderr, "  session create --id X --workdir /path [--env-setup '...'] -- cmd args")
	fmt.Fprintln(os.Stderr, "  session exists --id X")
	fmt.Fprintln(os.Stderr, "  session kill --id X")
	fmt.Fprintln(os.Stderr, "  session capture --id X [--lines 100]")
	fmt.Fprintln(os.Stderr, "  session send --id X --text 'hello' [--no-enter]")
	fmt.Fprintln(os.Stderr, "  session key --id X --key Enter")
	fmt.Fprintln(os.Stderr, "  session log-start --id X")
	fmt.Fprintln(os.Stderr, "  session log-read --id X [--offset N] [--max-bytes N]")
	fmt.Fprintln(os.Stderr, "  file list --path /dir")
	fmt.Fprintln(os.Stderr, "  file read --path /file [--max-bytes N]")
	fmt.Fprintln(os.Stderr, "  file write --path /file  (reads stdin)")
}

// --- ping ---

func cmdPing() {
	writeJSON(map[string]any{
		"ok":       true,
		"version":  version,
		"platform": runtime.GOOS + "/" + runtime.GOARCH,
	})
}

// --- session ---

func cmdSession(sub string, args []string) {
	switch sub {
	case "create":
		sessionCreate(args)
	case "exists":
		sessionExists(args)
	case "kill":
		sessionKill(args)
	case "capture":
		sessionCapture(args)
	case "send":
		sessionSend(args)
	case "key":
		sessionKey(args)
	case "log-start":
		sessionLogStart(args)
	case "log-read":
		sessionLogRead(args)
	default:
		fmt.Fprintf(os.Stderr, "unknown session subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func sessionCreate(args []string) {
	var id, workdir, envSetup string
	var cmdArgs []string

	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--id":
			i++
			id = args[i]
		case "--workdir":
			i++
			workdir = args[i]
		case "--env-setup":
			i++
			envSetup = args[i]
		case "--":
			cmdArgs = args[i+1:]
			i = len(args) // break loop
		}
	}

	if id == "" || workdir == "" || len(cmdArgs) == 0 {
		fatal("usage: cam-agent session create --id X --workdir /path -- cmd args")
	}

	mgr := session.NewManager()
	if err := mgr.Create(id, cmdArgs, workdir, envSetup); err != nil {
		fatalErr("session create", err)
	}
	writeJSON(map[string]any{"ok": true})
}

func sessionExists(args []string) {
	id := flagStr(args, "--id", "")
	if id == "" {
		fatal("usage: cam-agent session exists --id X")
	}

	mgr := session.NewManager()
	if mgr.Exists(id) {
		os.Exit(0)
	} else {
		os.Exit(1)
	}
}

func sessionKill(args []string) {
	id := flagStr(args, "--id", "")
	if id == "" {
		fatal("usage: cam-agent session kill --id X")
	}

	mgr := session.NewManager()
	if err := mgr.Kill(id); err != nil {
		fatalErr("session kill", err)
	}
	writeJSON(map[string]any{"ok": true})
}

func sessionCapture(args []string) {
	id := flagStr(args, "--id", "")
	lines := flagInt(args, "--lines", 100)
	if id == "" {
		fatal("usage: cam-agent session capture --id X [--lines N]")
	}

	mgr := session.NewManager()
	output, err := mgr.Capture(id, lines)
	if err != nil {
		fatalErr("session capture", err)
	}
	// Plain text output (hot path — no JSON encoding overhead)
	fmt.Print(output)
}

func sessionSend(args []string) {
	id := flagStr(args, "--id", "")
	text := flagStr(args, "--text", "")
	noEnter := flagBool(args, "--no-enter")
	if id == "" {
		fatal("usage: cam-agent session send --id X --text 'hello' [--no-enter]")
	}

	mgr := session.NewManager()
	if err := mgr.SendText(id, text, !noEnter); err != nil {
		fatalErr("session send", err)
	}
	writeJSON(map[string]any{"ok": true})
}

func sessionKey(args []string) {
	id := flagStr(args, "--id", "")
	key := flagStr(args, "--key", "")
	if id == "" || key == "" {
		fatal("usage: cam-agent session key --id X --key Enter")
	}

	mgr := session.NewManager()
	if err := mgr.SendKey(id, key); err != nil {
		fatalErr("session key", err)
	}
	writeJSON(map[string]any{"ok": true})
}

func sessionLogStart(args []string) {
	id := flagStr(args, "--id", "")
	if id == "" {
		fatal("usage: cam-agent session log-start --id X")
	}

	mgr := session.NewManager()
	path, err := mgr.StartLogging(id)
	if err != nil {
		fatalErr("session log-start", err)
	}
	writeJSON(map[string]any{"ok": true, "path": path})
}

func sessionLogRead(args []string) {
	id := flagStr(args, "--id", "")
	offset := flagInt64(args, "--offset", 0)
	maxBytes := flagInt(args, "--max-bytes", 256000)
	if id == "" {
		fatal("usage: cam-agent session log-read --id X [--offset N] [--max-bytes N]")
	}

	mgr := session.NewManager()
	data, newOffset, err := mgr.ReadLog(id, offset, maxBytes)
	if err != nil {
		fatalErr("session log-read", err)
	}
	// Plain text output with X-Offset header on first line for incremental reads
	fmt.Printf("offset:%d\n", newOffset)
	os.Stdout.Write(data)
}

// --- file ---

func cmdFile(sub string, args []string) {
	switch sub {
	case "list":
		fileList(args)
	case "read":
		fileRead(args)
	case "write":
		fileWrite(args)
	default:
		fmt.Fprintf(os.Stderr, "unknown file subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func fileList(args []string) {
	path := flagStr(args, "--path", "")
	if path == "" {
		fatal("usage: cam-agent file list --path /dir")
	}

	entries, err := fileops.List(path)
	if err != nil {
		fatalErr("file list", err)
	}
	writeJSON(map[string]any{"entries": entries})
}

func fileRead(args []string) {
	path := flagStr(args, "--path", "")
	maxBytes := flagInt(args, "--max-bytes", 512000)
	if path == "" {
		fatal("usage: cam-agent file read --path /file [--max-bytes N]")
	}

	data, err := fileops.Read(path, maxBytes)
	if err != nil {
		fatalErr("file read", err)
	}
	os.Stdout.Write(data)
}

func fileWrite(args []string) {
	path := flagStr(args, "--path", "")
	if path == "" {
		fatal("usage: cam-agent file write --path /file")
	}

	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		fatalErr("file write (reading stdin)", err)
	}

	if err := fileops.Write(path, data); err != nil {
		fatalErr("file write", err)
	}
	writeJSON(map[string]any{"ok": true})
}

// --- helpers ---

func writeJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.Encode(v)
}

func fatal(msg string) {
	fmt.Fprintln(os.Stderr, msg)
	os.Exit(1)
}

func fatalErr(context string, err error) {
	fmt.Fprintf(os.Stderr, "%s: %s\n", context, err)
	os.Exit(1)
}

func flagStr(args []string, name, def string) string {
	for i := 0; i < len(args)-1; i++ {
		if args[i] == name {
			return args[i+1]
		}
	}
	return def
}

func flagInt(args []string, name string, def int) int {
	s := flagStr(args, name, "")
	if s == "" {
		return def
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return v
}

func flagInt64(args []string, name string, def int64) int64 {
	s := flagStr(args, name, "")
	if s == "" {
		return def
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return def
	}
	return v
}

func flagBool(args []string, name string) bool {
	for _, a := range args {
		if a == name {
			return true
		}
	}
	return false
}

// stripFlagValue removes --flag and its value from args
func stripFlagValue(args []string, name string) []string {
	_ = strings.TrimSpace // avoid unused import
	var out []string
	for i := 0; i < len(args); i++ {
		if args[i] == name && i+1 < len(args) {
			i++ // skip value too
			continue
		}
		out = append(out, args[i])
	}
	return out
}
