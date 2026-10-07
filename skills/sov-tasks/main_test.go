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
	if err := os.WriteFile(file, []byte("title: Work\ntype: development\nexecutor: agent\ndescription: Work\ndepends_on: []\nacceptance_criteria: [Done]\n"), 0600); err != nil {
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

func TestClassificationForNewAndHistoricalCards(t *testing.T) {
	root, file := fixture(t)
	base := "title: Work\ndescription: Work\ndepends_on: []\nacceptance_criteria: [Done]\n"
	for _, tc := range []struct {
		name, fields string
		valid        bool
	}{
		{"agent-development", "type: development\nexecutor: agent\n", true},
		{"agent-research", "type: non_development\nexecutor: agent\n", true},
		{"external-delivery", "type: non_development\nexecutor: external\n", true},
		{"historical-new", "", false},
		{"type-only", "type: development\n", false},
		{"executor-only", "executor: agent\n", false},
		{"wrong-type", "type: unknown\nexecutor: agent\n", false},
		{"wrong-executor", "type: development\nexecutor: vendor\n", false},
		{"non-string-type", "type: 42\nexecutor: agent\n", false},
		{"non-string-executor", "type: development\nexecutor: true\n", false},
		{"invalid-pair", "type: development\nexecutor: external\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := os.WriteFile(file, []byte(base+tc.fields), 0600); err != nil {
				t.Fatal(err)
			}
			_, err := run([]string{"--state-dir", root, "create", "--file", file, "--slug", tc.name})
			if (err == nil) != tc.valid {
				t.Fatalf("create: %v, valid=%v", err, tc.valid)
			}
		})
	}
	// Old cards without both fields stay readable, including after archiving.
	historical := filepath.Join(root, "tasks", "0004-legacy")
	if err := os.Mkdir(historical, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(historical, "task.yaml"), []byte("id: \"0004\"\n"+base), 0600); err != nil {
		t.Fatal(err)
	}
	if _, ok := call(t, root, "show", "0004")["task"].(map[string]any)["type"]; ok {
		t.Fatal("historical type invented")
	}
	rows, err := run([]string{"--state-dir", root, "list"})
	if err != nil || len(rows.([]map[string]any)) != 4 {
		t.Fatal("historical list rejected", rows, err)
	}
	if call(t, root, "validate")["valid"] != true {
		t.Fatal("historical card rejected")
	}
	if err := os.WriteFile(filepath.Join(historical, "task.yaml"), []byte("id: \"0004\"\n"+base+"executor: agent\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, command := range []string{"list", "show"} {
		args := []string{"--state-dir", root, command}
		if command == "show" {
			args = append(args, "0004")
		}
		if _, err := run(args); err == nil {
			t.Fatalf("%s accepted invalid active card", command)
		}
	}
	if err := os.WriteFile(filepath.Join(historical, "task.yaml"), []byte("id: \"0004\"\n"+base), 0600); err != nil {
		t.Fatal(err)
	}
	call(t, root, "claim", "0004")
	call(t, root, "complete", "0004")
	if _, ok := call(t, root, "show-completed", "0004")["task"].(map[string]any)["executor"]; ok {
		t.Fatal("historical executor invented")
	}
	for _, fields := range []string{"type: development\n", "type: development\nexecutor: external\n", "type: 42\nexecutor: agent\n"} {
		if err := os.WriteFile(filepath.Join(root, "archive", "0004-legacy", "task.yaml"), []byte("id: \"0004\"\n"+base+fields), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := run([]string{"--state-dir", root, "show-completed", "0004"}); err == nil {
			t.Fatalf("invalid archive card accepted: %s", fields)
		}
		if _, err := run([]string{"--state-dir", root, "list-completed"}); err == nil {
			t.Fatalf("invalid archived list accepted: %s", fields)
		}
		if call(t, root, "validate")["valid"] != false {
			t.Fatal("invalid archive card validated")
		}
	}
}

func TestExternalDeliveryAndDependentSelection(t *testing.T) {
	root, file := fixture(t)
	write := func(source string) {
		t.Helper()
		if err := os.WriteFile(file, []byte(source), 0600); err != nil {
			t.Fatal(err)
		}
	}
	base := "title: Work\ndescription: Work\nacceptance_criteria: [Received]\n"
	write(base + "type: non_development\nexecutor: external\ndepends_on: []\n")
	ext := call(t, root, "create", "--file", file, "--slug", "assets")
	write(base + "type: development\nexecutor: agent\ndepends_on: [\"0001\"]\n")
	dep := call(t, root, "create", "--file", file, "--slug", "menu")
	write(base + "type: non_development\nexecutor: agent\ndepends_on: []\n")
	other := call(t, root, "create", "--file", file, "--slug", "research")
	rows, err := run([]string{"--state-dir", root, "list-ready"})
	if err != nil || len(rows.([]map[string]any)) != 2 || rows.([]map[string]any)[0]["id"] != ext["id"] || rows.([]map[string]any)[1]["id"] != other["id"] {
		t.Fatal("unexpected ready tasks", rows, err)
	}
	// claim-next has no semantic filter: the agent must skip external delivery
	// and claim the specific suitable ID, leaving the dependent blocked.
	call(t, root, "claim", other["id"].(string))
	if call(t, root, "show", ext["id"].(string))["state"] != "READY" || call(t, root, "show", dep["id"].(string))["state"] != "BLOCKED" {
		t.Fatal("external task was prematurely completed")
	}
	if _, err := os.Stat(filepath.Join(root, "completed", "0001")); !os.IsNotExist(err) {
		t.Fatal("marker before confirmed delivery", err)
	}
	// Simulate the owner's confirmation and criterion check by the agent; the
	// CLI is intentionally unaware of the human decision.
	call(t, root, "claim", ext["id"].(string))
	if err := os.WriteFile(filepath.Join(root, "tasks", ext["name"].(string), "task.md"), []byte("Owner confirmed receipt; criterion checked"), 0600); err != nil {
		t.Fatal(err)
	}
	call(t, root, "complete", ext["id"].(string))
	if call(t, root, "show", dep["id"].(string))["state"] != "READY" {
		t.Fatal("dependent did not unblock")
	}
	if call(t, root, "validate")["valid"] != true {
		t.Fatal("invalid state after external completion")
	}
}

func TestDependenciesAndArchiveValidation(t *testing.T) {
	root, file := fixture(t)
	a := call(t, root, "create", "--file", file, "--slug", "first")
	bfile := filepath.Join(t.TempDir(), "dependent.yaml")
	os.WriteFile(bfile, []byte("title: Second\ntype: development\nexecutor: agent\ndescription: Work\ndepends_on: [\"0001\"]\nacceptance_criteria: [Done]\n"), 0600)
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
