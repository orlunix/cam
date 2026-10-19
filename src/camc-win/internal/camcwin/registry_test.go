package camcwin

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// testStore returns a Store rooted in a temp CAM_HOME.
func testStore(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("CAM_HOME", dir)
	return NewStore()
}

func sample(id, name string) Agent {
	return Agent{
		ID:           id,
		SessionID:    "uuid-" + id,
		Task:         Task{Name: name, Tool: "claude", Tags: []string{}},
		ContextPath:  `C:\work\` + name,
		Status:       "running",
		TmuxSession:  "cam-" + id,
		TmuxSocket:   SocketPath("cam-" + id),
		Hostname:     "testbox",
		StartedAt:    NowISO(),
		FilesChanged: []string{},
	}
}

func TestStoreRoundtrip(t *testing.T) {
	s := testStore(t)
	if err := s.Save(sample("ab12cd34", "demo")); err != nil {
		t.Fatal(err)
	}
	agents := s.List()
	if len(agents) != 1 || agents[0].ID != "ab12cd34" {
		t.Fatalf("List = %+v", agents)
	}

	// File is valid JSON at the CAM_HOME location.
	data, err := os.ReadFile(filepath.Join(Home(), "agents.json"))
	if err != nil {
		t.Fatal(err)
	}
	var raw []map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("registry file not valid JSON: %v", err)
	}
	// Fields the hub's _normalizeAgent reads must exist.
	for _, k := range []string{"id", "task", "context_path", "status", "tmux_session", "hostname", "started_at"} {
		if _, ok := raw[0][k]; !ok {
			t.Fatalf("record missing field %q", k)
		}
	}
}

func TestResolveIdiom(t *testing.T) {
	s := testStore(t)
	_ = s.Save(sample("aaaa1111", "alpha"))
	_ = s.Save(sample("bbbb2222", "beta"))

	cases := map[string]string{
		"aaaa1111":     "aaaa1111", // exact id
		"cam-bbbb2222": "bbbb2222", // tmux session name
		"aaaa":         "aaaa1111", // unique id prefix
		"beta":         "bbbb2222", // exact name (ci)
		"ALPHA":        "aaaa1111", // exact name, case-insensitive
		"alp":          "aaaa1111", // unique name prefix
		"2":            "bbbb2222", // 1-based index
	}
	for ref, want := range cases {
		got := s.Resolve(ref)
		if got == nil || got.ID != want {
			t.Fatalf("Resolve(%q) = %+v, want id %s", ref, got, want)
		}
	}
	if s.Resolve("nope") != nil {
		t.Fatal("Resolve(nope) should be nil")
	}
}

func TestUpdateAndRemove(t *testing.T) {
	s := testStore(t)
	_ = s.Save(sample("aaaa1111", "alpha"))

	now := NowISO()
	reason := "Stopped by user"
	err := s.UpdateFields("aaaa1111", func(rec *Agent) {
		rec.Status = "stopped"
		rec.CompletedAt = &now
		rec.ExitReason = &reason
	})
	if err != nil {
		t.Fatal(err)
	}
	got := s.Resolve("aaaa1111")
	if got.Status != "stopped" || got.ExitReason == nil || *got.ExitReason != reason {
		t.Fatalf("after update: %+v", got)
	}

	if err := s.Remove("aaaa1111"); err != nil {
		t.Fatal(err)
	}
	if len(s.List()) != 0 {
		t.Fatal("remove failed")
	}
}

func TestAgentJSONShape(t *testing.T) {
	a := sample("ab12cd34", "demo")
	m := agentJSON(a)

	// updated_at is computed, not persisted.
	if m["updated_at"] != a.StartedAt {
		t.Fatalf("updated_at = %v", m["updated_at"])
	}
	now := NowISO()
	a.CompletedAt = &now
	if m2 := agentJSON(a); m2["updated_at"] != now {
		t.Fatalf("updated_at for terminal agent = %v", m2["updated_at"])
	}

	// task must be a nested object (hub hoists task.name/tool).
	task, ok := m["task"].(map[string]any)
	if !ok {
		t.Fatalf("task is %T, want object", m["task"])
	}
	if task["name"] != "demo" || task["tool"] != "claude" {
		t.Fatalf("task = %+v", task)
	}
}
