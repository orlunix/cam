package fileops

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
)

// FileEntry represents a directory entry for JSON serialization.
type FileEntry struct {
	Name  string `json:"name"`
	Type  string `json:"type"` // "file", "dir", "symlink"
	Size  int64  `json:"size"`
	Mtime int64  `json:"mtime"` // Unix epoch
}

// List returns directory entries at the given path.
func List(path string) ([]FileEntry, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, fmt.Errorf("cannot read directory %s: %w", path, err)
	}

	result := make([]FileEntry, 0, len(entries))
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue // skip entries we can't stat
		}

		var ftype string
		var size int64
		if e.Type()&os.ModeSymlink != 0 {
			ftype = "symlink"
		} else if e.IsDir() {
			ftype = "dir"
		} else {
			ftype = "file"
			size = info.Size()
		}

		result = append(result, FileEntry{
			Name:  e.Name(),
			Type:  ftype,
			Size:  size,
			Mtime: info.ModTime().Unix(),
		})
	}

	sort.Slice(result, func(i, j int) bool {
		return result[i].Name < result[j].Name
	})

	return result, nil
}

// Read reads up to maxBytes from the given file.
func Read(path string, maxBytes int) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("cannot open %s: %w", path, err)
	}
	defer f.Close()

	buf := make([]byte, maxBytes)
	n, err := io.ReadFull(f, buf)
	if err == io.ErrUnexpectedEOF || err == io.EOF {
		return buf[:n], nil
	}
	if err != nil {
		return nil, err
	}
	return buf[:n], nil
}

// Write writes data to the given file, creating parent directories as needed.
func Write(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("cannot create directory %s: %w", dir, err)
	}
	return os.WriteFile(path, data, 0644)
}
