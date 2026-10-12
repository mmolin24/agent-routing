package collector

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// readChunk is how much of a session file is read per step when scanning
// backwards; most answers sit in the last few KB.
const readChunk = 64 * 1024

// reverseLines calls fn for each non-empty line of path, newest (last) first,
// until fn returns false or limit bytes have been read.
func reverseLines(path string, limit int64, fn func(line string) bool) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return
	}
	pos, read := st.Size(), int64(0)
	var carry []byte // the possibly partial first line of the previous chunk
	for pos > 0 && read < limit {
		step := min(int64(readChunk), pos)
		pos -= step
		buf := make([]byte, step, step+int64(len(carry)))
		if _, err := f.ReadAt(buf, pos); err != nil && err != io.EOF {
			return
		}
		read += step
		buf = append(buf, carry...)
		lines := bytes.Split(buf, []byte{'\n'})
		carry = lines[0]
		for i := len(lines) - 1; i >= 1; i-- {
			if len(bytes.TrimSpace(lines[i])) > 0 && !fn(string(lines[i])) {
				return
			}
		}
	}
	if pos == 0 && len(bytes.TrimSpace(carry)) > 0 {
		fn(string(carry))
	}
}

// firstLineJSON decodes the first line of path into v.
func firstLineJSON(path string, v any) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	r := bufio.NewReaderSize(f, readChunk)
	line, err := r.ReadBytes('\n')
	if err != nil && err != io.EOF {
		return err
	}
	return json.Unmarshal(line, v)
}

// newestFirst sorts files by modification time, newest first, keeping at most
// limit of them (all when limit is 0).
func newestFirst(files []string, limit int) []string {
	type stamped struct {
		path string
		mod  time.Time
	}
	var all []stamped
	for _, f := range files {
		if st, err := os.Stat(f); err == nil {
			all = append(all, stamped{f, st.ModTime()})
		}
	}
	sort.Slice(all, func(i, j int) bool { return all[i].mod.After(all[j].mod) })
	if limit > 0 && len(all) > limit {
		all = all[:limit]
	}
	out := make([]string, len(all))
	for i, s := range all {
		out[i] = s.path
	}
	return out
}

// findString returns the first string value stored under key anywhere in v.
func findString(v any, key string) (string, bool) {
	switch t := v.(type) {
	case map[string]any:
		if s, ok := t[key].(string); ok {
			return s, true
		}
		for _, x := range t {
			if s, ok := findString(x, key); ok {
				return s, true
			}
		}
	case []any:
		for _, x := range t {
			if s, ok := findString(x, key); ok {
				return s, true
			}
		}
	}
	return "", false
}

func home() string {
	h, _ := os.UserHomeDir()
	return h
}

// expandHome expands a leading "$HOME" or "~/".
func expandHome(p string) string {
	if rest, ok := strings.CutPrefix(p, "$HOME"); ok {
		return home() + rest
	}
	if rest, ok := strings.CutPrefix(p, "~/"); ok {
		return filepath.Join(home(), rest)
	}
	return p
}

func glob(pattern string) []string {
	m, _ := filepath.Glob(pattern)
	return m
}

func globHome(rel string) []string { return glob(filepath.Join(home(), rel)) }
