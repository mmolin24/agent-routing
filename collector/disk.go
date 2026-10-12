package collector

import (
	"context"
	"os"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

var bootSecRE = regexp.MustCompile(`sec = (\d+)`)

// bootAt mirrors host_load.boot_at: /proc/stat btime on Linux, kern.boottime on macOS.
func bootAt(ctx context.Context) *int64 {
	if b, err := os.ReadFile("/proc/stat"); err == nil {
		for _, ln := range strings.Split(string(b), "\n") {
			if f := strings.Fields(ln); len(f) == 2 && f[0] == "btime" {
				if v, err := strconv.ParseInt(f[1], 10, 64); err == nil {
					return &v
				}
			}
		}
	}
	if m := bootSecRE.FindStringSubmatch(command(ctx, 6*time.Second, "sysctl", "-n", "kern.boottime")); m != nil {
		if v, err := strconv.ParseInt(m[1], 10, 64); err == nil {
			return &v
		}
	}
	return nil
}

// bootID mirrors host_load.boot_id.
func bootID(ctx context.Context) *string {
	s := ""
	if b, err := os.ReadFile("/proc/sys/kernel/random/boot_id"); err == nil {
		// A WSL distro restart keeps the VM's kernel boot id: PID 1's start time is part of it.
		st, err := os.ReadFile("/proc/1/stat")
		if err != nil {
			return nil
		}
		i := strings.LastIndex(string(st), ")")
		f := strings.Fields(string(st)[i+1:])
		if i < 0 || len(f) < 20 {
			return nil
		}
		s = strings.TrimSpace(string(b)) + "/" + f[19]
	} else {
		s = strings.TrimSpace(command(ctx, 6*time.Second, "sysctl", "-n", "kern.bootsessionuuid"))
	}
	if s == "" {
		return nil
	}
	return &s
}

// diskPath mirrors host_load.disk_path: Windows' C: on WSL (it backs the VHDX files),
// the Data volume on macOS, else /.
func diskPath() string {
	if b, err := os.ReadFile("/proc/sys/kernel/osrelease"); err == nil && strings.Contains(strings.ToLower(string(b)), "microsoft") {
		if st, err := os.Stat("/mnt/c"); err == nil && st.IsDir() {
			return "/mnt/c"
		}
	}
	if runtime.GOOS == "darwin" {
		return "/System/Volumes/Data"
	}
	return "/"
}
