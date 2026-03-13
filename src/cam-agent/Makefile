BINARY=cam-agent
VERSION?=0.1.0
LDFLAGS=-ldflags "-s -w -X main.version=$(VERSION)"
OUT=dist

.PHONY: all clean linux darwin windows

all: linux darwin windows

linux:
	@mkdir -p $(OUT)
	GOOS=linux GOARCH=amd64 go build $(LDFLAGS) -o $(OUT)/$(BINARY)-linux-amd64 ./cmd/cam-agent
	GOOS=linux GOARCH=arm64 go build $(LDFLAGS) -o $(OUT)/$(BINARY)-linux-arm64 ./cmd/cam-agent

darwin:
	@mkdir -p $(OUT)
	GOOS=darwin GOARCH=amd64 go build $(LDFLAGS) -o $(OUT)/$(BINARY)-darwin-amd64 ./cmd/cam-agent
	GOOS=darwin GOARCH=arm64 go build $(LDFLAGS) -o $(OUT)/$(BINARY)-darwin-arm64 ./cmd/cam-agent

windows:
	@mkdir -p $(OUT)
	GOOS=windows GOARCH=amd64 go build $(LDFLAGS) -o $(OUT)/$(BINARY)-windows-amd64.exe ./cmd/cam-agent

install:
	go build $(LDFLAGS) -o /usr/local/bin/$(BINARY) ./cmd/cam-agent

test:
	go test ./...

clean:
	rm -rf $(OUT)
