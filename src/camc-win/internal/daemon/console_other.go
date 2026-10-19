//go:build !windows

package daemon

import "log"

func freeConsole() {}

func logConsoleState() {
	log.Println("daemon: console state not available on this platform")
}
