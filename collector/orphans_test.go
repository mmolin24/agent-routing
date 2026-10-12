package collector

import (
	"os"
	"reflect"
	"testing"
)

func TestFindOrphans(t *testing.T) {
	ps, err := os.ReadFile("testdata/ps_orphans.txt")
	if err != nil {
		t.Fatal(err)
	}
	got := findOrphans(string(ps), func(pid int) string { return "/w/" + map[int]string{500: "a", 600: "b"}[pid] })
	want := []Orphan{
		{PID: 500, AgeS: 377484, Ports: []int{8851}, RSS: (2560 + 1552 + 6464 + 90000) * 1024, Cwd: "/w/a", Cmd: "expo start"},
		{PID: 600, AgeS: 4953, Ports: []int{5173}, RSS: 3000 * 1024, Cwd: "/w/b", Cmd: "vite"},
		{PID: 901, AgeS: 60, Ports: []int{9000}, RSS: 1000 * 1024, Cwd: "/w/", Cmd: "expo start"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v\nwant %+v", got, want)
	}
	if got := findOrphans("", nil); got == nil || len(got) != 0 {
		t.Fatalf("empty ps: want [], got %#v", got)
	}
}
