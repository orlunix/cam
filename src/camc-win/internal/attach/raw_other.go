//go:build !windows

package attach

import (
	"os"
	"syscall"
	"unsafe"
)

// rawState restores the terminal on exit (POSIX termios path, used for
// development/testing on Linux and macOS).
type rawState struct {
	termios syscall.Termios
}

const (
	ioctlGetTermios = syscall.TCGETS
	ioctlSetTermios = syscall.TCSETS
)

// makeRaw puts stdin into raw mode (cfmakeraw equivalent).
func makeRaw() (*rawState, error) {
	fd := os.Stdin.Fd()
	var old syscall.Termios
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, fd, ioctlGetTermios, uintptr(unsafe.Pointer(&old))); errno != 0 {
		return nil, errno
	}
	raw := old
	raw.Iflag &^= syscall.IXON | syscall.ICRNL | syscall.BRKINT | syscall.INPCK | syscall.ISTRIP
	raw.Lflag &^= syscall.ECHO | syscall.ICANON | syscall.ISIG | syscall.IEXTEN
	raw.Oflag &^= syscall.OPOST
	raw.Cc[syscall.VMIN] = 1
	raw.Cc[syscall.VTIME] = 0
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, fd, ioctlSetTermios, uintptr(unsafe.Pointer(&raw))); errno != 0 {
		return nil, errno
	}
	return &rawState{termios: old}, nil
}

func (st *rawState) restore() {
	syscall.Syscall(syscall.SYS_IOCTL, os.Stdin.Fd(), ioctlSetTermios, uintptr(unsafe.Pointer(&st.termios)))
}

type winsize struct {
	rows, cols uint16
	x, y       uint16
}

// terminalSize returns the terminal window size in cols/rows.
func terminalSize() (int, int) {
	var ws winsize
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, os.Stdout.Fd(), syscall.TIOCGWINSZ, uintptr(unsafe.Pointer(&ws))); errno != 0 || ws.cols == 0 {
		return 0, 0
	}
	return int(ws.cols), int(ws.rows)
}
