//go:build windows

package daemon

import (
	"log"
	"syscall"
)

var (
	kernel32             = syscall.NewLazyDLL("kernel32.dll")
	procGetConsoleWindow = kernel32.NewProc("GetConsoleWindow")
	procGetStdHandle     = kernel32.NewProc("GetStdHandle")
)

func freeConsole() {
	kernel32.NewProc("FreeConsole").Call()
}

func logConsoleState() {
	hwnd, _, _ := procGetConsoleWindow.Call()
	log.Printf("daemon: ConsoleWindow=0x%x", hwnd)

	for _, h := range []struct {
		name string
		id   uintptr
	}{
		{"STDIN", uintptr(0xFFFFFFF6)},
		{"STDOUT", uintptr(0xFFFFFFF5)},
		{"STDERR", uintptr(0xFFFFFFF4)},
	} {
		handle, _, _ := procGetStdHandle.Call(h.id)
		log.Printf("daemon: %s handle=0x%x", h.name, handle)
	}
}
