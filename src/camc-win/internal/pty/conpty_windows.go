//go:build windows

package pty

import (
	"fmt"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

var (
	kernel32                         = syscall.NewLazyDLL("kernel32.dll")
	procCreatePseudoConsole          = kernel32.NewProc("CreatePseudoConsole")
	procResizePseudoConsole          = kernel32.NewProc("ResizePseudoConsole")
	procClosePseudoConsole           = kernel32.NewProc("ClosePseudoConsole")
	procInitializeProcThreadAttrList = kernel32.NewProc("InitializeProcThreadAttributeList")
	procUpdateProcThreadAttribute    = kernel32.NewProc("UpdateProcThreadAttribute")
	procDeleteProcThreadAttrList     = kernel32.NewProc("DeleteProcThreadAttributeList")
	procTerminateProcess             = kernel32.NewProc("TerminateProcess")
)

const (
	_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
	_EXTENDED_STARTUPINFO_PRESENT        = 0x00080000
)

type startupInfoEx struct {
	StartupInfo   syscall.StartupInfo
	AttributeList uintptr
}

// ConPTY wraps a Windows pseudo console and the child process attached to it.
// Uses raw syscall.Handle I/O instead of os.File to avoid Go runtime's async
// I/O layer, which doesn't work correctly with anonymous pipe handles.
type ConPTY struct {
	hPC       uintptr
	hPipeIn   syscall.Handle // write end → child stdin
	hPipeOut  syscall.Handle // read end ← child stdout
	process   syscall.Handle
	exited    chan struct{}
	exitCode  uint32
	closeOnce sync.Once
	killed    bool
}

func makeCoord(cols, rows int) uintptr {
	return uintptr(uint16(cols)) | (uintptr(uint16(rows)) << 16)
}

func New(cols, rows int, command string, workdir string, env []string) (Terminal, error) {
	var ptyInRead, ptyInWrite syscall.Handle
	var ptyOutRead, ptyOutWrite syscall.Handle

	if err := syscall.CreatePipe(&ptyInRead, &ptyInWrite, nil, 0); err != nil {
		return nil, fmt.Errorf("create input pipe: %w", err)
	}
	if err := syscall.CreatePipe(&ptyOutRead, &ptyOutWrite, nil, 0); err != nil {
		syscall.CloseHandle(ptyInRead)
		syscall.CloseHandle(ptyInWrite)
		return nil, fmt.Errorf("create output pipe: %w", err)
	}

	size := makeCoord(cols, rows)
	var hPC uintptr
	r1, _, _ := procCreatePseudoConsole.Call(
		size,
		uintptr(ptyInRead),
		uintptr(ptyOutWrite),
		0,
		uintptr(unsafe.Pointer(&hPC)),
	)
	if r1 != 0 {
		syscall.CloseHandle(ptyInRead)
		syscall.CloseHandle(ptyInWrite)
		syscall.CloseHandle(ptyOutRead)
		syscall.CloseHandle(ptyOutWrite)
		return nil, fmt.Errorf("CreatePseudoConsole failed: HRESULT 0x%08x", r1)
	}

	syscall.CloseHandle(ptyInRead)
	syscall.CloseHandle(ptyOutWrite)

	process, err := startProcessWithPTY(hPC, command, workdir)
	if err != nil {
		procClosePseudoConsole.Call(hPC)
		syscall.CloseHandle(ptyInWrite)
		syscall.CloseHandle(ptyOutRead)
		return nil, fmt.Errorf("start process: %w", err)
	}

	c := &ConPTY{
		hPC:      hPC,
		hPipeIn:  ptyInWrite,
		hPipeOut: ptyOutRead,
		process:  process,
		exited:   make(chan struct{}),
	}
	go c.watchProcess()
	return c, nil
}

func startProcessWithPTY(hPC uintptr, command string, workdir string) (syscall.Handle, error) {
	var attrListSize uintptr
	procInitializeProcThreadAttrList.Call(0, 1, 0, uintptr(unsafe.Pointer(&attrListSize)))

	attrListBuf := make([]byte, attrListSize)
	attrList := uintptr(unsafe.Pointer(&attrListBuf[0]))

	r1, _, err := procInitializeProcThreadAttrList.Call(
		attrList, 1, 0,
		uintptr(unsafe.Pointer(&attrListSize)),
	)
	if r1 == 0 {
		return 0, fmt.Errorf("InitializeProcThreadAttributeList: %v", err)
	}
	defer procDeleteProcThreadAttrList.Call(attrList)

	// lpValue must be the HPCON value itself, not a pointer to it.
	// HPCON is an opaque handle (void*); the API reads from this address.
	r1, _, err = procUpdateProcThreadAttribute.Call(
		attrList, 0,
		_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
		hPC,
		unsafe.Sizeof(hPC),
		0, 0,
	)
	if r1 == 0 {
		return 0, fmt.Errorf("UpdateProcThreadAttribute: %v", err)
	}

	si := startupInfoEx{AttributeList: attrList}
	si.StartupInfo.Cb = uint32(unsafe.Sizeof(si))

	cmdLine, sysErr := syscall.UTF16PtrFromString(command)
	if sysErr != nil {
		return 0, sysErr
	}

	var workdirPtr *uint16
	if workdir != "" {
		workdirPtr, sysErr = syscall.UTF16PtrFromString(workdir)
		if sysErr != nil {
			return 0, sysErr
		}
	}

	var pi syscall.ProcessInformation
	createErr := syscall.CreateProcess(
		nil, cmdLine, nil, nil, false,
		_EXTENDED_STARTUPINFO_PRESENT,
		nil, workdirPtr,
		&si.StartupInfo, &pi,
	)
	if createErr != nil {
		return 0, fmt.Errorf("CreateProcess: %v", createErr)
	}

	syscall.CloseHandle(pi.Thread)
	return pi.Process, nil
}

func (c *ConPTY) watchProcess() {
	syscall.WaitForSingleObject(c.process, syscall.INFINITE)
	var code uint32
	syscall.GetExitCodeProcess(c.process, &code)
	c.exitCode = code
	close(c.exited)
}

var procPeekNamedPipe = kernel32.NewProc("PeekNamedPipe")

// Read polls for available data with PeekNamedPipe then reads with ReadFile.
// This avoids permanently blocking an OS thread when no data is available,
// which can prevent ConPTY from flushing output on some Windows versions.
func (c *ConPTY) Read(buf []byte) (int, error) {
	if len(buf) == 0 {
		return 0, nil
	}
	for {
		select {
		case <-c.exited:
			// Final read: drain anything left in the pipe
			var avail uint32
			r1, _, _ := procPeekNamedPipe.Call(uintptr(c.hPipeOut), 0, 0, 0, uintptr(unsafe.Pointer(&avail)), 0)
			if r1 == 0 || avail == 0 {
				return 0, fmt.Errorf("ReadFile: %w", syscall.ERROR_BROKEN_PIPE)
			}
			var n uint32
			if err := syscall.ReadFile(c.hPipeOut, buf, &n, nil); err != nil {
				return int(n), fmt.Errorf("ReadFile: %w", err)
			}
			return int(n), nil
		default:
		}

		var avail uint32
		r1, _, _ := procPeekNamedPipe.Call(uintptr(c.hPipeOut), 0, 0, 0, uintptr(unsafe.Pointer(&avail)), 0)
		if r1 == 0 {
			return 0, fmt.Errorf("PeekNamedPipe failed")
		}
		if avail > 0 {
			var n uint32
			if err := syscall.ReadFile(c.hPipeOut, buf, &n, nil); err != nil {
				return int(n), fmt.Errorf("ReadFile: %w", err)
			}
			return int(n), nil
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// Write uses synchronous WriteFile via syscall.
func (c *ConPTY) Write(data []byte) (int, error) {
	if len(data) == 0 {
		return 0, nil
	}
	var n uint32
	err := syscall.WriteFile(c.hPipeIn, data, &n, nil)
	if err != nil {
		return int(n), fmt.Errorf("WriteFile: %w", err)
	}
	return int(n), nil
}

func (c *ConPTY) Resize(cols, rows int) error {
	r1, _, err := procResizePseudoConsole.Call(c.hPC, makeCoord(cols, rows))
	if r1 != 0 {
		return fmt.Errorf("ResizePseudoConsole: %v", err)
	}
	return nil
}

func (c *ConPTY) Wait() error {
	<-c.exited
	return nil
}

func (c *ConPTY) ExitCode() int { return int(c.exitCode) }

// Close terminates the child process and releases all handles.
// Safe to call multiple times.
func (c *ConPTY) Close() error {
	c.closeOnce.Do(func() {
		c.killed = true

		// 1. Close the pseudo console — signals child its console is gone.
		procClosePseudoConsole.Call(c.hPC)

		// 2. Forcefully terminate the child process tree.
		procTerminateProcess.Call(uintptr(c.process), 1)

		// 3. Wait for watchProcess to detect exit (with timeout).
		select {
		case <-c.exited:
		default:
		}

		// 4. Close pipe handles.
		syscall.CloseHandle(c.hPipeIn)
		syscall.CloseHandle(c.hPipeOut)

		// 5. Close process handle last (after watchProcess is done with it).
		syscall.CloseHandle(c.process)
	})
	return nil
}
