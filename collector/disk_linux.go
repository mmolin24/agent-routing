package collector

import "syscall"

// statDisk: free (available to a user) and total bytes, as Python's statvfs f_bavail/f_blocks * f_frsize.
func statDisk(path string) (free, total int64, ok bool) {
	var st syscall.Statfs_t
	if syscall.Statfs(path, &st) != nil {
		return 0, 0, false
	}
	return int64(st.Bavail) * int64(st.Frsize), int64(st.Blocks) * int64(st.Frsize), true
}
