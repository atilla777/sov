// sov-state provides byte-level conditional publication of task state files.
// Linux local filesystems only; every writer must use the same persistent mutex.
package main

import (
	"bytes"
	"crypto/rand"
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
const version = "0.3.0"

// Test-only seam; never exposed through CLI flags or environment variables.
var publishHook func(stage string) error

type response struct {
	V                int     `json:"v"`
	Status           string  `json:"status"`
	Resource         string  `json:"resource"`
	Content          *string `json:"content_base64,omitempty"`
	Revision         string  `json:"revision,omitempty"`
	ArchiveRevision  string  `json:"archive_revision,omitempty"`
	SessionID        string  `json:"session_id,omitempty"`
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

func newSessionID() (string, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "", err
	}
	id[6] = (id[6] & 0x0f) | 0x40
	id[8] = (id[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:]), nil
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

func readTarget(dir, name string) ([]byte, os.FileInfo, error) {
	f, info, err := openRegular(filepath.Join(dir, name))
	if err != nil {
		return nil, nil, err
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err == nil && len(data) > limit {
		err = fmt.Errorf("%s exceeds 16 MiB", name)
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
	for _, name := range []string{"ROADMAP.md", "ARCHIVE.md", ".roadmap.lock"} {
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

func publish(dir, name string, data []byte, mode os.FileMode) error {
	f, err := os.CreateTemp(dir, "."+name+".tmp-")
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
	if err = os.Rename(f.Name(), filepath.Join(dir, name)); err != nil {
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
	if len(args) == 1 && args[0] == "session-id" {
		r.Resource = "SESSION"
		id, err := newSessionID()
		if err != nil {
			return errorResponseFor(fail("io_error", err), "SESSION")
		}
		r.SessionID = id
		return r, 0
	}
	if len(args) == 0 || (args[0] != "read" && args[0] != "read-archive" && args[0] != "commit" && args[0] != "archive") {
		return errorResponse(fail("invalid_input", errors.New("expected session-id, read, read-archive, commit or archive")))
	}
	command := args[0]
	fs := flag.NewFlagSet(command, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dirArg := fs.String("state-dir", "", "existing state directory")
	var expected, input, timeout, archiveExpected, archiveInput string
	if command == "commit" || command == "archive" {
		fs.StringVar(&expected, "expected", "", "expected revision")
		fs.StringVar(&input, "input", "", "replacement file")
		fs.StringVar(&timeout, "lock-timeout", "5s", "mutex wait")
		if command == "archive" {
			fs.StringVar(&archiveExpected, "archive-expected", "", "expected archive revision")
			fs.StringVar(&archiveInput, "archive-input", "", "replacement archive file")
		}
	}
	if err := fs.Parse(args[1:]); err != nil {
		return errorResponse(fail("invalid_input", err))
	}
	if fs.NArg() != 0 {
		return errorResponse(fail("invalid_input", errors.New("unexpected argument")))
	}
	if command == "commit" || command == "archive" {
		validRevision := regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
		if !validRevision.MatchString(expected) || (command == "archive" && !validRevision.MatchString(archiveExpected)) {
			return errorResponse(fail("invalid_input", errors.New("invalid revision")))
		}
	}
	dir, err := stateDir(*dirArg)
	if err != nil {
		return errorResponse(err)
	}
	if command == "read" || command == "read-archive" {
		name := "ROADMAP.md"
		if command == "read-archive" {
			name = "ARCHIVE.md"
		}
		r.Resource = name
		data, _, err := readTarget(dir, name)
		if err != nil {
			return errorResponseFor(fail("io_error", err), name)
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
	var archiveData []byte
	if command == "archive" {
		if archiveInput == "" || archiveInput == input {
			return errorResponse(fail("invalid_input", errors.New("distinct --archive-input required")))
		}
		first, firstErr := os.Stat(input)
		second, secondErr := os.Stat(archiveInput)
		if firstErr != nil || secondErr != nil {
			return errorResponse(fail("invalid_input", errors.New("unable to stat archive inputs")))
		}
		if os.SameFile(first, second) {
			return errorResponse(fail("invalid_input", errors.New("archive inputs alias each other")))
		}
		archiveData, err = readInput(archiveInput, dir)
		if err != nil {
			return errorResponse(err)
		}
	}
	mutex, err := lock(dir, wait)
	if err != nil {
		return errorResponse(err)
	}
	defer mutex.Close()
	current, info, err := readTarget(dir, "ROADMAP.md")
	if err != nil {
		return errorResponse(fail("io_error", err))
	}
	if rev := revision(current); expected != rev {
		return errorResponse(&failure{code: "revision_conflict", message: "revision changed", current: rev})
	}
	archiveChanged := false
	if command == "archive" {
		archived, archiveInfo, err := readTarget(dir, "ARCHIVE.md")
		if err != nil {
			return errorResponseFor(fail("io_error", err), "ARCHIVE.md")
		}
		if rev := revision(archived); archiveExpected != rev {
			return errorResponseFor(&failure{code: "revision_conflict", message: "archive revision changed", current: rev}, "ARCHIVE.md")
		}
		// Publish the archive first. A crash may leave the same row in both files,
		// but must never leave the row absent from both. A retry can use the
		// already-published archive bytes and finish shortening the roadmap.
		archiveChanged = !bytes.Equal(archived, archiveData)
		if archiveChanged {
			if err = publish(dir, "ARCHIVE.md", archiveData, archiveInfo.Mode()); err != nil {
				return errorResponseFor(err, "ARCHIVE.md")
			}
		}
		r.ArchiveRevision = revision(archiveData)
	}
	changed := archiveChanged || !bytes.Equal(current, data)
	r.Changed = &changed
	if bytes.Equal(current, data) {
		r.Revision = expected
		return r, 0
	}
	if err = publish(dir, "ROADMAP.md", data, info.Mode()); err != nil {
		if command == "archive" {
			f, code := errorResponse(err)
			f.MayHaveCommitted = true // archive may already have been published
			f.ArchiveRevision = r.ArchiveRevision
			return f, code
		}
		return errorResponse(err)
	}
	r.Revision = revision(data)
	return r, 0
}

func errorResponse(err error) (response, int) {
	return errorResponseFor(err, "ROADMAP.md")
}

func errorResponseFor(err error, name string) (response, int) {
	var f *failure
	if !errors.As(err, &f) {
		f = fail("io_error", err)
	}
	codes := map[string]int{"revision_conflict": 10, "lock_timeout": 11, "invalid_input": 12, "io_error": 13}
	return response{V: 1, Status: "error", Resource: name, Code: f.code, Message: f.message, CurrentRevision: f.current, MayHaveCommitted: f.maybe}, codes[f.code]
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
