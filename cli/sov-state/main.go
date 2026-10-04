// sov-state provides byte-level conditional publication of an existing ROADMAP.md.
// Linux local filesystems only; every writer must use the same persistent mutex.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"syscall"
	"time"
)

const limit = 16 << 20
const version = "0.1.0"

// Test-only seam; never exposed through CLI flags or environment variables.
var publishHook func(stage string) error

type response struct {
	V                int     `json:"v"`
	Status           string  `json:"status"`
	Resource         string  `json:"resource"`
	Content          *string `json:"content_base64,omitempty"`
	Revision         string  `json:"revision,omitempty"`
	Changed          *bool   `json:"changed,omitempty"`
	Code             string  `json:"code,omitempty"`
	CurrentRevision  string  `json:"current_revision,omitempty"`
	Message          string  `json:"message,omitempty"`
	MayHaveCommitted bool    `json:"may_have_committed,omitempty"`
}

type failure struct {
	code    string
	message string
	current string
	maybe   bool
}

func (e *failure) Error() string           { return e.message }
func fail(code string, err error) *failure { return &failure{code: code, message: err.Error()} }
func revision(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func regularSingle(info os.FileInfo) bool {
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && info.Mode().IsRegular() && st.Nlink == 1
}

func stateDir(path string) (string, error) {
	if !filepath.IsAbs(path) {
		return "", fail("invalid_input", errors.New("--state-dir must be absolute"))
	}
	physical, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", fail("io_error", err)
	}
	info, err := os.Stat(physical)
	if err != nil {
		return "", fail("io_error", err)
	}
	if !info.IsDir() {
		return "", fail("invalid_input", errors.New("state-dir is not a directory"))
	}
	return physical, nil
}

func openRegular(path string) (*os.File, os.FileInfo, error) {
	// O_NOFOLLOW prevents a symlink being substituted between lstat and open.
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, nil, err
	}
	f := os.NewFile(uintptr(fd), path)
	info, err := f.Stat()
	if err == nil && !regularSingle(info) {
		err = errors.New("expected a regular file with one link: " + path)
	}
	if err == nil {
		var named os.FileInfo
		named, err = os.Lstat(path)
		if err == nil && (!regularSingle(named) || !os.SameFile(info, named)) {
			err = errors.New("file changed during open: " + path)
		}
	}
	if err != nil {
		f.Close()
		return nil, nil, err
	}
	return f, info, nil
}

func readTarget(dir string) ([]byte, os.FileInfo, error) {
	f, info, err := openRegular(filepath.Join(dir, "ROADMAP.md"))
	if err != nil {
		return nil, nil, err
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err == nil && len(data) > limit {
		err = errors.New("ROADMAP.md exceeds 16 MiB")
	}
	return data, info, err
}

func readInput(path, dir string) ([]byte, error) {
	if !filepath.IsAbs(path) {
		return nil, fail("invalid_input", errors.New("--input must be absolute"))
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, fail("invalid_input", err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, fail("invalid_input", err)
	}
	if !info.Mode().IsRegular() {
		return nil, fail("invalid_input", errors.New("input must be a regular file"))
	}
	for _, name := range []string{"ROADMAP.md", ".roadmap.lock"} {
		other, err := os.Stat(filepath.Join(dir, name))
		if err == nil && os.SameFile(info, other) {
			return nil, fail("invalid_input", errors.New("input aliases state file"))
		}
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || len(data) > limit {
		return nil, fail("invalid_input", errors.New("input unreadable or exceeds 16 MiB"))
	}
	return data, nil
}

func lock(dir string, timeout time.Duration) (*os.File, error) {
	path := filepath.Join(dir, ".roadmap.lock")
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, fail("io_error", err)
	}
	f := os.NewFile(uintptr(fd), path)
	info, err := f.Stat()
	if err == nil && !regularSingle(info) {
		err = errors.New("invalid mutex type")
	}
	if err != nil {
		f.Close()
		return nil, fail("io_error", err)
	}
	deadline := time.Now().Add(timeout)
	for {
		err = syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EAGAIN && err != syscall.EINTR {
			f.Close()
			return nil, fail("io_error", err)
		}
		if !time.Now().Before(deadline) {
			f.Close()
			return nil, fail("lock_timeout", errors.New("mutex wait timed out"))
		}
		time.Sleep(min(10*time.Millisecond, time.Until(deadline)))
	}
	named, err := os.Lstat(path)
	if err == nil && (!regularSingle(named) || !os.SameFile(info, named)) {
		err = errors.New("mutex changed during lock")
	}
	if err != nil {
		f.Close()
		return nil, fail("io_error", err)
	}
	return f, nil
}

func publish(dir string, data []byte, mode os.FileMode) error {
	f, err := os.CreateTemp(dir, ".ROADMAP.tmp-")
	if err != nil {
		return fail("io_error", err)
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if err = f.Chmod(mode.Perm()); err != nil {
		return fail("io_error", err)
	}
	if _, err = f.Write(data); err != nil {
		return fail("io_error", err)
	}
	if err = f.Sync(); err != nil {
		return fail("io_error", err)
	}
	if err = f.Close(); err != nil {
		return fail("io_error", err)
	}
	if publishHook != nil {
		if err = publishHook("before_rename"); err != nil {
			return fail("io_error", err)
		}
	}
	if err = os.Rename(f.Name(), filepath.Join(dir, "ROADMAP.md")); err != nil {
		return fail("io_error", err)
	}
	if publishHook != nil {
		if err = publishHook("after_rename"); err != nil {
			return &failure{code: "io_error", message: err.Error(), maybe: true}
		}
	}
	d, err := os.Open(dir)
	if err == nil {
		err = d.Sync()
		if closeErr := d.Close(); err == nil {
			err = closeErr
		}
	}
	if err != nil {
		return &failure{code: "io_error", message: err.Error(), maybe: true}
	}
	return nil
}

func run(args []string) (response, int) {
	r := response{V: 1, Status: "ok", Resource: "ROADMAP.md"}
	if len(args) == 1 && args[0] == "--version" {
		r.Resource = "ROADMAP.md"
		r.Message = "sov-state " + version + " (protocol v:1)"
		return r, 0
	}
	if len(args) == 0 || (args[0] != "read" && args[0] != "commit") {
		return errorResponse(fail("invalid_input", errors.New("expected read or commit")))
	}
	command := args[0]
	fs := flag.NewFlagSet(command, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dirArg := fs.String("state-dir", "", "existing state directory")
	var expected, input, timeout string
	if command == "commit" {
		fs.StringVar(&expected, "expected", "", "expected revision")
		fs.StringVar(&input, "input", "", "replacement file")
		fs.StringVar(&timeout, "lock-timeout", "5s", "mutex wait")
	}
	if err := fs.Parse(args[1:]); err != nil {
		return errorResponse(fail("invalid_input", err))
	}
	if fs.NArg() != 0 {
		return errorResponse(fail("invalid_input", errors.New("unexpected argument")))
	}
	if command == "commit" {
		if !regexp.MustCompile(`^sha256:[0-9a-f]{64}$`).MatchString(expected) {
			return errorResponse(fail("invalid_input", errors.New("invalid revision")))
		}
	}
	dir, err := stateDir(*dirArg)
	if err != nil {
		return errorResponse(err)
	}
	if command == "read" {
		data, _, err := readTarget(dir)
		if err != nil {
			return errorResponse(fail("io_error", err))
		}
		content := base64.StdEncoding.EncodeToString(data)
		r.Content = &content
		r.Revision = revision(data)
		return r, 0
	}
	if input == "" {
		return errorResponse(fail("invalid_input", errors.New("--input required")))
	}
	wait, err := time.ParseDuration(timeout)
	if err != nil || wait < 0 || wait > time.Minute {
		return errorResponse(fail("invalid_input", errors.New("invalid lock-timeout (0s..60s)")))
	}
	data, err := readInput(input, dir)
	if err != nil {
		return errorResponse(err)
	}
	mutex, err := lock(dir, wait)
	if err != nil {
		return errorResponse(err)
	}
	defer mutex.Close()
	current, info, err := readTarget(dir)
	if err != nil {
		return errorResponse(fail("io_error", err))
	}
	if rev := revision(current); expected != rev {
		return errorResponse(&failure{code: "revision_conflict", message: "revision changed", current: rev})
	}
	changed := !bytes.Equal(current, data)
	r.Changed = &changed
	if !changed {
		r.Revision = expected
		return r, 0
	}
	if err = publish(dir, data, info.Mode()); err != nil {
		return errorResponse(err)
	}
	r.Revision = revision(data)
	return r, 0
}

func errorResponse(err error) (response, int) {
	var f *failure
	if !errors.As(err, &f) {
		f = fail("io_error", err)
	}
	codes := map[string]int{"revision_conflict": 10, "lock_timeout": 11, "invalid_input": 12, "io_error": 13}
	return response{V: 1, Status: "error", Resource: "ROADMAP.md", Code: f.code, Message: f.message, CurrentRevision: f.current, MayHaveCommitted: f.maybe}, codes[f.code]
}

func main() {
	if runtime.GOOS != "linux" {
		fmt.Fprintln(os.Stderr, "Linux required")
		os.Exit(12)
	}
	r, code := run(os.Args[1:])
	if err := json.NewEncoder(os.Stdout).Encode(r); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(13)
	}
	os.Exit(code)
}
