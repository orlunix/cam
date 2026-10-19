package ipc

import (
	"bytes"
	"testing"
)

func TestWriteReadRequest(t *testing.T) {
	var buf bytes.Buffer
	req := Request{
		Action:  ActionSendKeys,
		Text:    "hello world",
		Literal: true,
	}
	if err := WriteMessage(&buf, &req); err != nil {
		t.Fatalf("WriteMessage: %v", err)
	}

	var got Request
	if err := ReadMessage(&buf, &got); err != nil {
		t.Fatalf("ReadMessage: %v", err)
	}

	if got.Action != ActionSendKeys {
		t.Errorf("expected action %q, got %q", ActionSendKeys, got.Action)
	}
	if got.Text != "hello world" {
		t.Errorf("expected text 'hello world', got %q", got.Text)
	}
	if !got.Literal {
		t.Error("expected literal=true")
	}
}

func TestWriteReadResponse(t *testing.T) {
	var buf bytes.Buffer
	resp := Response{
		OK:     true,
		Output: "captured output\nline 2",
	}
	if err := WriteMessage(&buf, &resp); err != nil {
		t.Fatalf("WriteMessage: %v", err)
	}

	var got Response
	if err := ReadMessage(&buf, &got); err != nil {
		t.Fatalf("ReadMessage: %v", err)
	}

	if !got.OK {
		t.Error("expected OK=true")
	}
	if got.Output != "captured output\nline 2" {
		t.Errorf("expected output, got %q", got.Output)
	}
}

func TestMessageTooLarge(t *testing.T) {
	header := []byte{0x01, 0x00, 0x00, 0x00} // 16 MB
	buf := bytes.NewReader(header)
	var req Request
	err := ReadMessage(buf, &req)
	if err == nil {
		t.Fatal("expected error for oversized message")
	}
}

func TestEmptyInput(t *testing.T) {
	buf := bytes.NewReader([]byte{})
	var req Request
	err := ReadMessage(buf, &req)
	if err == nil {
		t.Fatal("expected error for empty input")
	}
}

func TestTruncatedBody(t *testing.T) {
	header := []byte{0x00, 0x00, 0x00, 0x10} // claims 16 bytes
	body := []byte("{}")                       // only 2 bytes
	buf := bytes.NewReader(append(header, body...))
	var req Request
	err := ReadMessage(buf, &req)
	if err == nil {
		t.Fatal("expected error for truncated body")
	}
}

func TestRoundTripAllActions(t *testing.T) {
	actions := []Action{
		ActionSendKeys,
		ActionSendKey,
		ActionCapture,
		ActionHasSession,
		ActionKillSession,
		ActionSetOption,
		ActionPipePane,
		ActionPing,
	}

	for _, action := range actions {
		var buf bytes.Buffer
		req := Request{Action: action}
		if err := WriteMessage(&buf, &req); err != nil {
			t.Fatalf("WriteMessage(%s): %v", action, err)
		}
		var got Request
		if err := ReadMessage(&buf, &got); err != nil {
			t.Fatalf("ReadMessage(%s): %v", action, err)
		}
		if got.Action != action {
			t.Errorf("expected %s, got %s", action, got.Action)
		}
	}
}

func TestMultipleMessages(t *testing.T) {
	var buf bytes.Buffer

	for i := 0; i < 10; i++ {
		req := Request{Action: ActionPing, Text: "ping"}
		if err := WriteMessage(&buf, &req); err != nil {
			t.Fatalf("WriteMessage %d: %v", i, err)
		}
	}

	for i := 0; i < 10; i++ {
		var got Request
		if err := ReadMessage(&buf, &got); err != nil {
			t.Fatalf("ReadMessage %d: %v", i, err)
		}
		if got.Action != ActionPing {
			t.Errorf("message %d: expected ping, got %s", i, got.Action)
		}
	}
}

func TestResponseWithError(t *testing.T) {
	var buf bytes.Buffer
	resp := Response{
		OK:    false,
		Error: "session not found",
	}
	if err := WriteMessage(&buf, &resp); err != nil {
		t.Fatalf("WriteMessage: %v", err)
	}

	var got Response
	if err := ReadMessage(&buf, &got); err != nil {
		t.Fatalf("ReadMessage: %v", err)
	}

	if got.OK {
		t.Error("expected OK=false")
	}
	if got.Error != "session not found" {
		t.Errorf("expected error message, got %q", got.Error)
	}
}

func TestCaptureRequest(t *testing.T) {
	var buf bytes.Buffer
	req := Request{
		Action:    ActionCapture,
		Lines:     100,
		Alternate: false,
		Join:      true,
	}
	if err := WriteMessage(&buf, &req); err != nil {
		t.Fatalf("WriteMessage: %v", err)
	}

	var got Request
	if err := ReadMessage(&buf, &got); err != nil {
		t.Fatalf("ReadMessage: %v", err)
	}

	if got.Lines != 100 {
		t.Errorf("expected lines=100, got %d", got.Lines)
	}
	if got.Alternate {
		t.Error("expected alternate=false")
	}
	if !got.Join {
		t.Error("expected join=true")
	}
}
