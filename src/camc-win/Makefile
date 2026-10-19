.PHONY: build build-windows test test-verbose clean fmt vet lint

BINARY  = wintmux
CAMCWIN = camc-win
VERSION = 0.2.0

# Build for current platform (Linux/macOS — for running unit tests)
build:
	go build -ldflags "-X main.version=$(VERSION)" -o $(BINARY) ./cmd/wintmux/
	go build -o $(CAMCWIN) ./cmd/camc-win/

# Cross-compile for Windows (produces wintmux.exe + camc-win.exe)
build-windows:
	GOOS=windows GOARCH=amd64 go build -ldflags "-X main.version=$(VERSION)" -o $(BINARY).exe ./cmd/wintmux/
	GOOS=windows GOARCH=amd64 go build -o $(CAMCWIN).exe ./cmd/camc-win/

# Run all unit tests
test:
	go test ./internal/...

# Run tests with verbose output
test-verbose:
	go test -v ./internal/...

# Run tests with race detector
test-race:
	go test -race ./internal/...

clean:
	rm -f $(BINARY) $(BINARY).exe $(CAMCWIN) $(CAMCWIN).exe

fmt:
	go fmt ./...

vet:
	go vet ./...

lint: fmt vet
