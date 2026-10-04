package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func fixture(t *testing.T) (string, string) {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, ".sov")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "ROADMAP.md"), []byte("old\n"), 0640); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".roadmap.lock"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	input := filepath.Join(root, "next.md")
	if err := os.WriteFile(input, []byte("new\n"), 0600); err != nil {
		t.Fatal(err)
	}
	return dir, input
}

func call(t *testing.T, args ...string) (response, int) {
	t.Helper()
	r, code := run(args)
	return r, code
}

func TestReadCommitAndValidation(t *testing.T) {
	dir, input := fixture(t)
	read, code := call(t, "read", "--state-dir", dir)
	if code != 0 || read.Revision != revision([]byte("old\n")) {
		t.Fatalf("read: %+v %d", read, code)
	}
	decoded, err := base64.StdEncoding.DecodeString(*read.Content)
	if err != nil || string(decoded) != "old\n" {
		t.Fatalf("content: %q %v", decoded, err)
	}
	args := []string{"commit", "--state-dir", dir, "--expected", read.Revision, "--input", input}
	first, code := call(t, args...)
	if code != 0 || first.Changed == nil || !*first.Changed || first.Revision != revision([]byte("new\n")) {
		t.Fatalf("commit: %+v %d", first, code)
	}
	conflict, code := call(t, args...)
	if code != 10 || conflict.CurrentRevision != first.Revision || conflict.MayHaveCommitted {
		t.Fatalf("conflict: %+v %d", conflict, code)
	}
	args[4] = first.Revision
	noop, code := call(t, args...)
	if code != 0 || noop.Changed == nil || *noop.Changed {
		t.Fatalf("noop: %+v %d", noop, code)
	}
	info, err := os.Stat(filepath.Join(dir, "ROADMAP.md"))
	if err != nil || info.Mode().Perm() != 0640 {
		t.Fatalf("mode: %v %v", info, err)
	}
	for _, tc := range [][]string{
		{"read", "--state-dir", "relative"},
		{"commit", "--state-dir", dir, "--expected", "sha256:INVALID", "--input", input},
		{"commit", "--state-dir", dir, "--expected", first.Revision, "--input", filepath.Join(dir, "ROADMAP.md")},
		{"commit", "--state-dir", dir, "--expected", first.Revision, "--input", input, "--lock-timeout", "61s"},
		{"read", "--state-dir", dir, "surplus"},
	} {
		if r, code := call(t, tc...); code != 12 {
			t.Fatalf("%v: %+v %d", tc, r, code)
		}
	}
}

func TestAliasesAndMissingState(t *testing.T) {
	dir, input := fixture(t)
	alias := filepath.Join(filepath.Dir(dir), "alias")
	if err := os.Symlink(dir, alias); err != nil {
		t.Fatal(err)
	}
	r, _ := call(t, "read", "--state-dir", alias)
	if r.Revision != revision([]byte("old\n")) {
		t.Fatal(r)
	}
	if c, code := call(t, "commit", "--state-dir", alias, "--expected", r.Revision, "--input", input); code != 0 || c.Revision != revision([]byte("new\n")) {
		t.Fatalf("alias: %+v %d", c, code)
	}
	if err := os.Remove(filepath.Join(dir, ".roadmap.lock")); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "commit", "--state-dir", dir, "--expected", revision([]byte("new\n")), "--input", input); code != 13 {
		t.Fatal(code)
	}
	if _, err := os.Stat(filepath.Join(dir, ".roadmap.lock")); !os.IsNotExist(err) {
		t.Fatal("mutex recreated")
	}
	if err := os.Remove(filepath.Join(dir, "ROADMAP.md")); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "read", "--state-dir", dir); code != 13 {
		t.Fatal(code)
	}
	if err := os.Symlink(input, filepath.Join(dir, "ROADMAP.md")); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "read", "--state-dir", dir); code != 13 {
		t.Fatal(code)
	}
}

func TestEmptyReadAndVersion(t *testing.T) {
	dir, _ := fixture(t)
	if err := os.WriteFile(filepath.Join(dir, "ROADMAP.md"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	r, code := call(t, "read", "--state-dir", dir)
	if code != 0 || r.Content == nil || *r.Content != "" || r.Revision != revision(nil) {
		t.Fatalf("empty read: %+v %d", r, code)
	}
	v, code := call(t, "--version")
	if code != 0 || !strings.Contains(v.Message, "v:1") {
		t.Fatalf("version: %+v %d", v, code)
	}
}

func TestMutexSubstitution(t *testing.T) {
	dir, input := fixture(t)
	lockPath := filepath.Join(dir, ".roadmap.lock")
	foreign := filepath.Join(filepath.Dir(input), "foreign")
	if err := os.WriteFile(foreign, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(lockPath); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(foreign, lockPath); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input); code != 13 {
		t.Fatal("symlink mutex accepted")
	}
	if err := os.Remove(lockPath); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(lockPath, nil, 0600); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(dir, "lock-alias")
	if err := os.Link(lockPath, alias); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input); code != 13 {
		t.Fatal("hardlink mutex accepted")
	}
}

func TestHardlinksAndInputSize(t *testing.T) {
	dir, input := fixture(t)
	link := filepath.Join(filepath.Dir(input), "linked.md")
	if err := os.Link(filepath.Join(dir, "ROADMAP.md"), link); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "read", "--state-dir", dir); code != 13 {
		t.Fatal("hardlinked state accepted")
	}
	if _, code := call(t, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", link); code != 12 {
		t.Fatal("input alias accepted")
	}
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(input, make([]byte, limit+1), 0600); err != nil {
		t.Fatal(err)
	}
	if _, code := call(t, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input); code != 12 {
		t.Fatal("oversized input accepted")
	}
}

func TestPublicationFailureBoundaries(t *testing.T) {
	dir, input := fixture(t)
	target := filepath.Join(dir, "ROADMAP.md")
	args := []string{"commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input}
	for _, stage := range []string{"before_rename", "after_rename"} {
		publishHook = func(s string) error {
			if s == stage {
				return errors.New("injected failure")
			}
			return nil
		}
		r, code := call(t, args...)
		publishHook = nil
		if code != 13 || r.MayHaveCommitted != (stage == "after_rename") {
			t.Fatalf("%s: %+v %d", stage, r, code)
		}
		data, err := os.ReadFile(target)
		if err != nil {
			t.Fatal(err)
		}
		want := "old\n"
		if stage == "after_rename" {
			want = "new\n"
		}
		if string(data) != want {
			t.Fatalf("%s: %q", stage, data)
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), ".ROADMAP.tmp-") {
				t.Fatal("temp left behind")
			}
		}
	}
	read, code := call(t, "read", "--state-dir", dir)
	if code != 0 || read.Revision != revision([]byte("new\n")) {
		t.Fatalf("lost response recovery: %+v %d", read, code)
	}
}

func TestLockTimeout(t *testing.T) {
	dir, input := fixture(t)
	f, err := os.Open(filepath.Join(dir, ".roadmap.lock"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	r, code := call(t, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input, "--lock-timeout", "0s")
	if code != 11 || r.MayHaveCommitted {
		t.Fatalf("timeout: %+v %d", r, code)
	}
}

// A real CLI subprocess tests the JSON/exit protocol and independent-process CAS.
func binary(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "sov-state")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build: %v %s", err, out)
	}
	return bin
}

func process(bin string, args ...string) (response, int, error) {
	out, err := exec.Command(bin, args...).Output()
	code := 0
	if e, ok := err.(*exec.ExitError); ok {
		code = e.ExitCode()
		err = nil
	}
	var r response
	if err == nil {
		err = json.Unmarshal(out, &r)
	}
	return r, code, err
}

func TestConcurrentProcesses(t *testing.T) {
	bin := binary(t)
	dir, input := fixture(t)
	other := filepath.Join(filepath.Dir(input), "other.md")
	if err := os.WriteFile(other, []byte("third\n"), 0600); err != nil {
		t.Fatal(err)
	}
	rev := revision([]byte("old\n"))
	var wg sync.WaitGroup
	results := make([]int, 2)
	for i, file := range []string{input, other} {
		wg.Add(1)
		go func(i int, file string) {
			defer wg.Done()
			_, code, err := process(bin, "commit", "--state-dir", dir, "--expected", rev, "--input", file)
			if err != nil {
				results[i] = -1
			} else {
				results[i] = code
			}
		}(i, file)
	}
	wg.Wait()
	if !((results[0] == 0 && results[1] == 10) || (results[0] == 10 && results[1] == 0)) {
		t.Fatal(results)
	}
	read, code, err := process(bin, "read", "--state-dir", dir)
	if err != nil || code != 0 {
		t.Fatalf("read: %+v %d %v", read, code, err)
	}
	if read.Revision != revision([]byte("new\n")) && read.Revision != revision([]byte("third\n")) {
		t.Fatal(read)
	}
	// A fresh read permits rebuilding a different change instead of reusing the stale revision.
	loser := input
	if results[0] == 0 {
		loser = other
	}
	if r, code, err := process(bin, "commit", "--state-dir", dir, "--expected", read.Revision, "--input", loser); err != nil || code != 0 || r.Revision == read.Revision {
		t.Fatalf("retry: %+v %d %v", r, code, err)
	}
}

func TestKilledLockOwner(t *testing.T) {
	bin := binary(t)
	dir, input := fixture(t)
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(self, "-test.run=^TestLockHelper$")
	cmd.Env = append(os.Environ(), "SOV_LOCK_HELPER="+filepath.Join(dir, ".roadmap.lock"))
	ready, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer cmd.Process.Kill()
	b := make([]byte, 1)
	if _, err := io.ReadFull(ready, b); err != nil || b[0] != '!' {
		t.Fatalf("lock helper: %v %q", err, b)
	}
	if r, code, _ := process(bin, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input, "--lock-timeout", "0s"); code != 11 {
		t.Fatalf("expected lock: %+v %d", r, code)
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	cmd.Wait()
	if r, code, err := process(bin, "commit", "--state-dir", dir, "--expected", revision([]byte("old\n")), "--input", input); err != nil || code != 0 {
		t.Fatalf("after kill: %+v %d %v", r, code, err)
	}
}

func TestLockHelper(t *testing.T) {
	path := os.Getenv("SOV_LOCK_HELPER")
	if path == "" {
		return
	}
	f, err := os.Open(path)
	if err != nil {
		os.Exit(2)
	}
	if syscall.Flock(int(f.Fd()), syscall.LOCK_EX) != nil {
		os.Exit(2)
	}
	fmt.Print("!")
	for {
		time.Sleep(time.Hour)
	}
}

func TestReadWholeVersions(t *testing.T) {
	dir, input := fixture(t)
	large := []byte(strings.Repeat("x", 2<<20))
	if err := os.WriteFile(input, large, 0600); err != nil {
		t.Fatal(err)
	}
	// Readers overlap repeated atomic replacements.
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 6; i++ {
			prev, _ := run([]string{"read", "--state-dir", dir})
			if r, c := run([]string{"commit", "--state-dir", dir, "--expected", prev.Revision, "--input", input}); c != 0 {
				t.Errorf("publish: %+v %d", r, c)
			}
		}
	}()
	for i := 0; i < 25; i++ {
		r, c := call(t, "read", "--state-dir", dir)
		data, err := base64.StdEncoding.DecodeString(*r.Content)
		if c != 0 || err != nil || (string(data) != "old\n" && string(data) != string(large)) || revision(data) != r.Revision {
			t.Fatalf("partial read: %+v %d %v", r, c, err)
		}
	}
	wg.Wait()
}
