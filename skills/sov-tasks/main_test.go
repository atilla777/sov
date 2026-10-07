package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func fixture(t *testing.T) (string, string) {
	t.Helper()
	root := filepath.Join(t.TempDir(), ".sov")
	for _, kind := range []string{"tasks", "claims", "completed", "archive"} {
		if err := os.MkdirAll(filepath.Join(root, kind), 0700); err != nil {
			t.Fatal(err)
		}
	}
	file := filepath.Join(t.TempDir(), "card.yaml")
	if err := os.WriteFile(file, []byte("title: Work\ndescription: Work\ndepends_on: []\nacceptance_criteria: [Done]\n"), 0600); err != nil {
		t.Fatal(err)
	}
	return root, file
}

func call(t *testing.T, root string, args ...string) map[string]any {
	t.Helper()
	v, err := run(append([]string{"--state-dir", root}, args...))
	if err != nil {
		t.Fatalf("%v: %v", args, err)
	}
	return v.(map[string]any)
}

func TestLifecycleAndInterruptedCompletion(t *testing.T) {
	root, file := fixture(t)
	first := call(t, root, "create", "--file", file, "--slug", "first")
	id, name := first["id"].(string), first["name"].(string)
	if id != "0001" {
		t.Fatal(first)
	}
	if call(t, root, "claim", id, "--owner", "a")["state"] != "IN PROGRESS" {
		t.Fatal("claim")
	}
	if call(t, root, "claim", id)["status"] != "not_ready" {
		t.Fatal("duplicate claim")
	}
	if _, err := run([]string{"--state-dir", root, "complete", id, "--owner", "b"}); err == nil {
		t.Fatal("wrong owner")
	}
	if err := os.WriteFile(filepath.Join(root, "tasks", name, "task.md"), []byte("saved"), 0600); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(root, "completed", id)
	if err := os.WriteFile(marker, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if call(t, root, "show", id)["state"] != "INCOMPLETE_COMPLETION" {
		t.Fatal("marker state")
	}
	if call(t, root, "validate")["valid"] != false {
		t.Fatal("incomplete transfer accepted")
	}
	if _, err := run([]string{"--state-dir", root, "release", id, "--owner", "a"}); err == nil {
		t.Fatal("release after marker")
	}
	call(t, root, "complete", id, "--owner", "a")
	if call(t, root, "validate")["valid"] != true {
		t.Fatal("invalid transfer")
	}
	data, err := os.ReadFile(filepath.Join(root, "archive", name, "task.md"))
	if err != nil || string(data) != "saved" {
		t.Fatal("lost artifact", err)
	}
	rows, err := run([]string{"--state-dir", root, "list-completed"})
	if err != nil || len(rows.([]map[string]any)) != 1 {
		t.Fatal(rows, err)
	}
	if call(t, root, "show-completed", id)["state"] != "COMPLETED" {
		t.Fatal("archive lookup")
	}
	second := call(t, root, "create", "--file", file, "--slug", "second")
	if second["id"] != "0002" {
		t.Fatal(second)
	}
	call(t, root, "claim", "0002")
	name2 := second["name"].(string)
	os.WriteFile(filepath.Join(root, "completed", "0002"), nil, 0600)
	os.Rename(filepath.Join(root, "tasks", name2), filepath.Join(root, "archive", name2))
	if call(t, root, "validate")["valid"] != false {
		t.Fatal("claim in archive accepted")
	}
	call(t, root, "complete", "0002")
	if call(t, root, "validate")["valid"] != true {
		t.Fatal("recovery failed")
	}
}

func TestStrictYAML(t *testing.T) {
	for _, source := range []string{
		"id: 0001\ntitle: x\ndescription: x\ndepends_on: []\nacceptance_criteria: [ok]\n",
		"title: x\ntitle: y\n",
		"title: [\n",
		"title: x\ndescription: x\ndepends_on: [0002]\nacceptance_criteria: [ok]\n",
		"title: x\ndescription: x\ndepends_on: []\nacceptance_criteria: [ok]\nstatus: planned\n",
		"title: x\ndescription: x\ndepends_on: []\nacceptance_criteria: [ok]\n---\nid: \"0001\"\n",
	} {
		root, file := fixture(t)
		os.WriteFile(file, []byte(source), 0600)
		if _, err := run([]string{"--state-dir", root, "create", "--file", file, "--slug", "bad"}); err == nil {
			t.Fatalf("accepted %q", source)
		}
	}
}

func TestDependenciesAndArchiveValidation(t *testing.T) {
	root, file := fixture(t)
	a := call(t, root, "create", "--file", file, "--slug", "first")
	bfile := filepath.Join(t.TempDir(), "dependent.yaml")
	os.WriteFile(bfile, []byte("title: Second\ndescription: Work\ndepends_on: [\"0001\"]\nacceptance_criteria: [Done]\n"), 0600)
	b := call(t, root, "create", "--file", bfile, "--slug", "second")
	if call(t, root, "show", b["id"].(string))["state"] != "BLOCKED" {
		t.Fatal("dependency")
	}
	call(t, root, "claim", a["id"].(string))
	call(t, root, "complete", a["id"].(string))
	if call(t, root, "show", b["id"].(string))["state"] != "READY" {
		t.Fatal("marker did not unblock")
	}
	os.Remove(filepath.Join(root, "completed", "0001"))
	if _, err := run([]string{"--state-dir", root, "list-completed"}); err == nil || !strings.Contains(err.Error(), "without marker") {
		t.Fatal(err)
	}
}

func TestConcurrentProcesses(t *testing.T) {
	root, file := fixture(t)
	bin := filepath.Join(t.TempDir(), "sov-task")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build: %s: %v", output, err)
	}
	var wg sync.WaitGroup
	ids := make(chan string, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			cmd := exec.Command(bin, "--state-dir", root, "create", "--file", file, "--slug", "parallel")
			output, err := cmd.CombinedOutput()
			if err != nil {
				t.Errorf("create: %v %s", err, output)
				return
			}
			var row map[string]string
			if err := json.Unmarshal(output, &row); err != nil {
				t.Error(err)
				return
			}
			ids <- row["id"]
		}(i)
	}
	wg.Wait()
	close(ids)
	seen := map[string]bool{}
	for id := range ids {
		if seen[id] {
			t.Fatal("duplicate ID", id)
		}
		seen[id] = true
	}
	if len(seen) != 12 {
		t.Fatal("lost IDs", seen)
	}
	claim := make(chan string, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			cmd := exec.Command(bin, "--state-dir", root, "claim", "0001")
			output, err := cmd.CombinedOutput()
			if err != nil {
				t.Errorf("claim: %v %s", err, output)
				return
			}
			var row map[string]any
			if err := json.Unmarshal(output, &row); err != nil {
				t.Error(err)
				return
			}
			if row["state"] != nil {
				claim <- row["state"].(string)
			} else {
				claim <- row["status"].(string)
			}
		}()
	}
	wg.Wait()
	close(claim)
	winners := 0
	for result := range claim {
		if result == "IN PROGRESS" {
			winners++
		} else if result != "not_ready" {
			t.Fatal(result)
		}
	}
	if winners != 1 {
		t.Fatal("expected one claimant", winners)
	}
}

func TestBinaryUsesOneStateFromMainAndWorktree(t *testing.T) {
	root, file := fixture(t)
	bin := filepath.Join(t.TempDir(), "sov-task")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build: %s: %v", output, err)
	}
	main := t.TempDir()
	worktree := t.TempDir()
	invoke := func(cwd string, args ...string) any {
		t.Helper()
		cmd := exec.Command(bin, append([]string{"--state-dir", root}, args...)...)
		cmd.Dir = cwd
		output, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("%v from %s: %s: %v", args, cwd, output, err)
		}
		var result any
		if err := json.Unmarshal(output, &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	item := invoke(main, "create", "--file", file, "--slug", "shared").(map[string]any)
	if invoke(worktree, "show", item["id"].(string)).(map[string]any)["state"] != "READY" {
		t.Fatal("state not shared")
	}
	invoke(worktree, "claim", item["id"].(string))
	if invoke(main, "show", item["id"].(string)).(map[string]any)["state"] != "IN PROGRESS" {
		t.Fatal("claim not shared")
	}
}
