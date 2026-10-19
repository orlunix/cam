//go:build windows

package attach

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

var (
	kKernel32          = syscall.NewLazyDLL("kernel32.dll")
	procGetConsoleMode = kKernel32.NewProc("GetConsoleMode")
	procSetConsoleMode = kKernel32.NewProc("SetConsoleMode")
	procGetConsoleInfo = kKernel32.NewProc("GetConsoleScreenBufferInfo")
)

const (
	enableEchoInput      = 0x0004
	enableLineInput      = 0x0002
	enableProcessedInput = 0x0001
	enableVTInput        = 0x0200
	enableVTProcessing   = 0x0004
)

type consoleCoord struct {
	x, y int16
}

type consoleSmallRect struct {
	left, top, right, bottom int16
}

type consoleScreenBufferInfo struct {
	size       consoleCoord
	cursor     consoleCoord
	attributes uint16
	window     consoleSmallRect
	maxSize    consoleCoord
}

// rawState restores the console on exit.
type rawState struct {
	inMode  uint32
	outMode uint32
}

// makeRaw puts the console into raw input / VT output mode.
func makeRaw() (*rawState, error) {
	in := syscall.Stdin
	out := syscall.Handle(os.Stdout.Fd())

	var st rawState
	r, _, err := procGetConsoleMode.Call(uintptr(in), uintptr(unsafe.Pointer(&st.inMode)))
	if r == 0 {
		return nil, fmt.Errorf("GetConsoleMode(stdin): %v", err)
	}
	newIn := (st.inMode &^ (enableEchoInput | enableLineInput | enableProcessedInput)) | enableVTInput
	if r, _, err := procSetConsoleMode.Call(uintptr(in), uintptr(newIn)); r == 0 {
		return nil, fmt.Errorf("SetConsoleMode(stdin): %v", err)
	}
	if r, _, _ := procGetConsoleMode.Call(uintptr(out), uintptr(unsafe.Pointer(&st.outMode))); r != 0 {
		procSetConsoleMode.Call(uintptr(out), uintptr(st.outMode|enableVTProcessing))
	}
	return &st, nil
}

func (st *rawState) restore() {
	procSetConsoleMode.Call(uintptr(syscall.Stdin), uintptr(st.inMode))
	procSetConsoleMode.Call(uintptr(syscall.Handle(os.Stdout.Fd())), uintptr(st.outMode))
}

// terminalSize returns the console window size in cols/rows.
func terminalSize() (int, int) {
	var info consoleScreenBufferInfo
	r, _, _ := procGetConsoleInfo.Call(uintptr(syscall.Handle(os.Stdout.Fd())), uintptr(unsafe.Pointer(&info)))
	if r == 0 {
		return 0, 0
	}
	cols := int(info.window.right-info.window.left) + 1
	rows := int(info.window.bottom-info.window.top) + 1
	return cols, rows
}
