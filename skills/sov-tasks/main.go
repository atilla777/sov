// sov-task implements the filesystem operations used by the sov-tasks skill.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"gopkg.in/yaml.v3"
)

var idPattern = regexp.MustCompile(`^[0-9]{4}$`)
var namePattern = regexp.MustCompile(`^([0-9]{4})-([a-z0-9]+(?:-[a-z0-9]+)*)$`)
var slugPattern = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)

type store struct {
	root string
}

func validID(id string) bool { return idPattern.MatchString(id) && id != "0000" }

func dir(path string) error {
	st, err := os.Lstat(path)
	if err != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("expected directory without symlink: %s", path)
	}
	return nil
}

func regular(path string) error {
	st, err := os.Lstat(path)
	if err != nil || !st.Mode().IsRegular() || st.Sys().(*syscall.Stat_t).Nlink != 1 {
		return fmt.Errorf("expected regular file without links: %s", path)
	}
	return nil
}

func newStore(root string) (store, error) {
	if !filepath.IsAbs(root) {
		return store{}, errors.New("--state-dir must be absolute")
	}
	s := store{root}
	for _, path := range []string{root, s.path("tasks"), s.path("claims"), s.path("completed"), s.path("archive")} {
		if err := dir(path); err != nil {
			return s, err
		}
	}
	return s, nil
}

func (s store) path(parts ...string) string {
	return filepath.Join(append([]string{s.root}, parts...)...)
}

func (s store) entries(kind string) ([]os.DirEntry, error) { return os.ReadDir(s.path(kind)) }

func (s store) names(kind string) (map[string]string, error) {
	entries, err := s.entries(kind)
	if err != nil {
		return nil, err
	}
	names := make(map[string]string)
	for _, entry := range entries {
		name := entry.Name()
		match := namePattern.FindStringSubmatch(name)
		if match == nil {
			return nil, fmt.Errorf("unexpected entry in %s: %s", kind, name)
		}
		path := s.path(kind, name)
		if err := dir(path); err != nil {
			return nil, err
		}
		if _, ok := names[match[1]]; ok {
			return nil, fmt.Errorf("duplicate ID in %s: %s", kind, match[1])
		}
		names[match[1]] = path
	}
	return names, nil
}

func (s store) markers() (map[string]bool, error) {
	entries, err := s.entries("completed")
	if err != nil {
		return nil, err
	}
	markers := make(map[string]bool)
	for _, entry := range entries {
		name := entry.Name()
		if !validID(name) {
			return nil, fmt.Errorf("invalid completion marker: %s", name)
		}
		if err := regular(s.path("completed", name)); err != nil {
			return nil, err
		}
		markers[name] = true
	}
	return markers, nil
}

// Decode through yaml.Node so a numeric ID and duplicate or non-string keys
// cannot silently turn into a valid string ID or lose an earlier field.
func parse(data []byte) (map[string]any, error) {
	var doc yaml.Node
	decoder := yaml.NewDecoder(strings.NewReader(string(data)))
	if err := decoder.Decode(&doc); err != nil {
		return nil, fmt.Errorf("invalid YAML: %w", err)
	}
	var extra yaml.Node
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return nil, errors.New("invalid YAML: multiple documents")
		}
		return nil, fmt.Errorf("invalid YAML: %w", err)
	}
	if len(doc.Content) == 0 || doc.Content[0].Kind != yaml.MappingNode {
		return nil, errors.New("expected YAML mapping")
	}
	var decode func(*yaml.Node) (any, error)
	decode = func(n *yaml.Node) (any, error) {
		if n.Kind == yaml.ScalarNode {
			if n.Tag == "!!str" {
				return n.Value, nil
			}
			var v any
			if err := n.Decode(&v); err != nil {
				return nil, err
			}
			return v, nil
		}
		if n.Kind == yaml.SequenceNode {
			v := make([]any, 0, len(n.Content))
			for _, child := range n.Content {
				x, err := decode(child)
				if err != nil {
					return nil, err
				}
				v = append(v, x)
			}
			return v, nil
		}
		if n.Kind == yaml.MappingNode {
			m := make(map[string]any)
			for i := 0; i < len(n.Content); i += 2 {
				k := n.Content[i]
				if k.Tag != "!!str" || k.Value == "<<" {
					return nil, errors.New("duplicate or non-string YAML key")
				}
				if _, ok := m[k.Value]; ok {
					return nil, errors.New("duplicate or non-string YAML key")
				}
				v, err := decode(n.Content[i+1])
				if err != nil {
					return nil, err
				}
				m[k.Value] = v
			}
			return m, nil
		}
		return nil, errors.New("unsupported YAML node")
	}
	v, err := decode(doc.Content[0])
	if err != nil {
		return nil, err
	}
	return v.(map[string]any), nil
}

func checkCard(card map[string]any, name string, creating bool) error {
	if !namePattern.MatchString(name) {
		return fmt.Errorf("invalid task directory name: %s", name)
	}
	for _, field := range []string{"id", "title", "description", "depends_on", "acceptance_criteria"} {
		if _, ok := card[field]; !ok {
			return fmt.Errorf("%s: missing fields: %s", name, field)
		}
	}
	id, ok := card["id"].(string)
	if !ok || !validID(id) || id != name[:4] {
		return fmt.Errorf("%s: id must be a matching four-digit string (0001–9999)", name)
	}
	for _, field := range []string{"title", "description"} {
		v, ok := card[field].(string)
		if !ok || strings.TrimSpace(v) == "" {
			return fmt.Errorf("%s: %s must be a nonempty string", name, field)
		}
	}
	taskType, hasType := card["type"]
	executor, hasExecutor := card["executor"]
	if hasType != hasExecutor || (creating && !hasType) {
		return fmt.Errorf("%s: type and executor must both be present for new tasks or both absent for historical tasks", name)
	}
	if hasType {
		t, ok := taskType.(string)
		if !ok || (t != "development" && t != "non_development") {
			return fmt.Errorf("%s: type must be development or non_development", name)
		}
		e, ok := executor.(string)
		if !ok || (e != "agent" && e != "external") {
			return fmt.Errorf("%s: executor must be agent or external", name)
		}
		if t == "development" && e == "external" {
			return fmt.Errorf("%s: development + external is not allowed", name)
		}
	}
	deps, ok := card["depends_on"].([]any)
	if !ok {
		return fmt.Errorf("%s: depends_on must be a list", name)
	}
	seen := make(map[string]bool)
	for _, dep := range deps {
		v, ok := dep.(string)
		if !ok || !validID(v) || v == id {
			return fmt.Errorf("%s: depends_on contains an invalid or self ID", name)
		}
		if seen[v] {
			return fmt.Errorf("%s: duplicate dependencies", name)
		}
		seen[v] = true
	}
	criteria, ok := card["acceptance_criteria"].([]any)
	if !ok || len(criteria) == 0 {
		return fmt.Errorf("%s: acceptance_criteria must contain nonempty strings", name)
	}
	for _, item := range criteria {
		v, ok := item.(string)
		if !ok || strings.TrimSpace(v) == "" {
			return fmt.Errorf("%s: acceptance_criteria must contain nonempty strings", name)
		}
	}
	for _, field := range []string{"status", "claimed_by", "claimed_at", "completed_at"} {
		if _, ok := card[field]; ok {
			return fmt.Errorf("%s: runtime fields belong to the filesystem", name)
		}
	}
	return nil
}

func (s store) card(path string) (map[string]any, error) {
	if err := dir(path); err != nil {
		return nil, err
	}
	file := filepath.Join(path, "task.yaml")
	if err := regular(file); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, err
	}
	card, err := parse(data)
	if err != nil {
		return nil, err
	}
	if err := checkCard(card, filepath.Base(path), false); err != nil {
		return nil, err
	}
	return card, nil
}

func (s store) locate(id string) (string, error) {
	if !validID(id) {
		return "", errors.New("ID must be 0001–9999")
	}
	names, err := s.names("tasks")
	if err != nil {
		return "", err
	}
	if path := names[id]; path != "" {
		return path, nil
	}
	return "", fmt.Errorf("task not found: %s", id)
}

func (s store) state(path string, card map[string]any, markers map[string]bool) string {
	id := card["id"].(string)
	if markers[id] {
		return "INCOMPLETE_COMPLETION"
	}
	if _, err := os.Lstat(s.path("claims", filepath.Base(path))); err == nil {
		return "IN PROGRESS"
	}
	for _, dep := range card["depends_on"].([]any) {
		if !markers[dep.(string)] {
			return "BLOCKED"
		}
	}
	return "READY"
}

func (s store) row(path string, markers map[string]bool) (map[string]any, error) {
	card, err := s.card(path)
	if err != nil {
		return nil, err
	}
	return map[string]any{"id": card["id"], "name": filepath.Base(path), "state": s.state(path, card, markers), "task": card}, nil
}

func keys(m map[string]string) []string {
	result := make([]string, 0, len(m))
	for k := range m {
		result = append(result, k)
	}
	sort.Strings(result)
	return result
}

func (s store) completedRows(id string) ([]map[string]any, error) {
	if id != "" && !validID(id) {
		return nil, errors.New("ID must be 0001–9999")
	}
	tasks, err := s.names("tasks")
	if err != nil {
		return nil, err
	}
	archive, err := s.names("archive")
	if err != nil {
		return nil, err
	}
	claims, err := s.names("claims")
	if err != nil {
		return nil, err
	}
	markers, err := s.markers()
	if err != nil {
		return nil, err
	}
	ids := map[string]string{}
	for k := range archive {
		ids[k] = ""
	}
	for k := range markers {
		ids[k] = ""
	}
	if id != "" {
		ids = map[string]string{id: ""}
	}
	rows := make([]map[string]any, 0, len(ids))
	for _, key := range keys(ids) {
		path := archive[key]
		if tasks[key] != "" && path != "" {
			return nil, fmt.Errorf("%s: task and archive both present", key)
		}
		if tasks[key] != "" && markers[key] {
			return nil, fmt.Errorf("%s: marker present, task not yet archived (incomplete completion)", key)
		}
		if path == "" {
			if markers[key] {
				return nil, fmt.Errorf("%s: marker without archive (incomplete completion)", key)
			}
			return nil, fmt.Errorf("completed task not found: %s", key)
		}
		if !markers[key] {
			return nil, fmt.Errorf("%s: archive without marker", key)
		}
		if claims[key] != "" {
			return nil, fmt.Errorf("%s: archive still has claim (incomplete completion)", key)
		}
		card, err := s.card(path)
		if err != nil {
			return nil, err
		}
		rows = append(rows, map[string]any{"id": key, "name": filepath.Base(path), "state": "COMPLETED", "task": card})
	}
	return rows, nil
}

func (s store) locked(action func() (any, error)) (any, error) {
	path := s.path(".tasks.lock")
	if _, err := os.Lstat(path); err == nil {
		if err := regular(path); err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	defer syscall.Close(fd)
	if err := syscall.Flock(fd, syscall.LOCK_EX); err != nil {
		return nil, err
	}
	defer syscall.Flock(fd, syscall.LOCK_UN)
	return action()
}

func (s store) create(file, slug string) (any, error) {
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, err
	}
	card, err := parse(data)
	if err != nil {
		return nil, err
	}
	return s.locked(func() (any, error) {
		tasks, err := s.names("tasks")
		if err != nil {
			return nil, err
		}
		archive, err := s.names("archive")
		if err != nil {
			return nil, err
		}
		claims, err := s.names("claims")
		if err != nil {
			return nil, err
		}
		markers, err := s.markers()
		if err != nil {
			return nil, err
		}
		used := map[string]bool{}
		maxID := 0
		for _, names := range []map[string]string{tasks, archive, claims} {
			for id := range names {
				used[id] = true
			}
		}
		for id := range markers {
			used[id] = true
		}
		for id := range used {
			n, _ := strconv.Atoi(id)
			if n > maxID {
				maxID = n
			}
		}
		id, explicit := card["id"].(string)
		if _, present := card["id"]; present {
			if !explicit || !validID(id) {
				return nil, errors.New("id must be a four-digit YAML string")
			}
			if used[id] {
				return nil, fmt.Errorf("ID already used: %s", id)
			}
		} else {
			if maxID == 9999 {
				return nil, errors.New("ID space exhausted")
			}
			id = fmt.Sprintf("%04d", maxID+1)
			card["id"] = id
		}
		if !slugPattern.MatchString(slug) {
			return nil, errors.New("slug must be lowercase ASCII words separated by hyphens")
		}
		name := id + "-" + slug
		if err := checkCard(card, name, true); err != nil {
			return nil, err
		}
		path := s.path("tasks", name)
		if err := os.Mkdir(path, 0755); err != nil {
			return nil, err
		}
		output, err := yaml.Marshal(card)
		if err == nil {
			var f *os.File
			f, err = os.OpenFile(filepath.Join(path, "task.yaml"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
			if err == nil {
				_, err = f.Write(output)
				closeErr := f.Close()
				if err == nil {
					err = closeErr
				}
			}
		}
		if err != nil {
			os.Remove(filepath.Join(path, "task.yaml"))
			os.Remove(path)
			return nil, err
		}
		return map[string]any{"id": id, "name": name}, nil
	})
}

func (s store) claim(path, owner string) (any, error) {
	markers, err := s.markers()
	if err != nil {
		return nil, err
	}
	card, err := s.card(path)
	if err != nil {
		return nil, err
	}
	if s.state(path, card, markers) != "READY" {
		return nil, nil
	}
	claim := s.path("claims", filepath.Base(path))
	if err := os.Mkdir(claim, 0755); err != nil {
		if os.IsExist(err) {
			return nil, nil
		}
		return nil, err
	}
	clean := func() {
		os.Remove(filepath.Join(claim, "owner"))
		os.Remove(filepath.Join(claim, "claimed_at"))
		os.Remove(claim)
	}
	tasks, err := s.names("tasks")
	if err != nil {
		clean()
		return nil, err
	}
	markers, err = s.markers()
	if err != nil {
		clean()
		return nil, err
	}
	if tasks[card["id"].(string)] != path || s.state(path, card, markers) != "IN PROGRESS" {
		clean()
		return nil, nil
	}
	for _, d := range card["depends_on"].([]any) {
		if !markers[d.(string)] {
			clean()
			return nil, nil
		}
	}
	if owner != "" {
		if err := os.WriteFile(filepath.Join(claim, "owner"), []byte(owner+"\n"), 0644); err != nil {
			clean()
			return nil, err
		}
	}
	if err := os.WriteFile(filepath.Join(claim, "claimed_at"), []byte(time.Now().UTC().Format(time.RFC3339Nano)+"\n"), 0644); err != nil {
		clean()
		return nil, err
	}
	return map[string]any{"id": card["id"], "name": filepath.Base(path), "state": "IN PROGRESS"}, nil
}

func (s store) checkClaim(path, owner string) error {
	if err := dir(path); err != nil {
		return err
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.Name() != "owner" && entry.Name() != "claimed_at" {
			return fmt.Errorf("unexpected claim entry: %s", entry.Name())
		}
		if err := regular(filepath.Join(path, entry.Name())); err != nil {
			return err
		}
	}
	file := filepath.Join(path, "owner")
	if _, err := os.Lstat(file); err == nil {
		data, err := os.ReadFile(file)
		if err != nil {
			return err
		}
		if owner == "" || strings.TrimSpace(string(data)) != owner {
			return errors.New("claim owner mismatch; inspect before manual recovery")
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	return nil
}

func removeClaim(path string) error {
	for _, name := range []string{"owner", "claimed_at"} {
		if err := os.Remove(filepath.Join(path, name)); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return os.Remove(path)
}

func (s store) release(id, owner string) (any, error) {
	return s.locked(func() (any, error) {
		path, err := s.locate(id)
		if err != nil {
			return nil, err
		}
		if _, err := os.Lstat(s.path("completed", id)); err == nil {
			return nil, errors.New("completion started; use complete to finish the transfer")
		} else if !os.IsNotExist(err) {
			return nil, err
		}
		claim := s.path("claims", filepath.Base(path))
		if err := s.checkClaim(claim, owner); err != nil {
			return nil, err
		}
		if err := removeClaim(claim); err != nil {
			return nil, err
		}
		return map[string]any{"id": id, "state": "released"}, nil
	})
}

func (s store) complete(id, owner string) (any, error) {
	return s.locked(func() (any, error) {
		if !validID(id) {
			return nil, errors.New("ID must be 0001–9999")
		}
		tasks, err := s.names("tasks")
		if err != nil {
			return nil, err
		}
		archive, err := s.names("archive")
		if err != nil {
			return nil, err
		}
		source, dest := tasks[id], archive[id]
		if source != "" && dest != "" {
			return nil, errors.New("task and archive both exist; manual inspection required")
		}
		if source == "" && dest == "" {
			return nil, errors.New("task and archive absent; manual inspection required")
		}
		marker := s.path("completed", id)
		_, markerErr := os.Lstat(marker)
		if source == "" && os.IsNotExist(markerErr) {
			return nil, errors.New("archive without marker; manual inspection required")
		}
		path := source
		if path == "" {
			path = dest
		}
		if _, err := s.card(path); err != nil {
			return nil, err
		}
		claim := s.path("claims", filepath.Base(path))
		if err := s.checkClaim(claim, owner); err != nil {
			return nil, err
		}
		if markerErr == nil {
			if err := regular(marker); err != nil {
				return nil, err
			}
		} else if !os.IsNotExist(markerErr) {
			return nil, markerErr
		} else {
			fd, err := syscall.Open(marker, syscall.O_CREAT|syscall.O_EXCL|syscall.O_WRONLY|syscall.O_NOFOLLOW, 0600)
			if err != nil {
				return nil, err
			}
			syscall.Close(fd)
		}
		if source != "" {
			if err := os.Rename(source, s.path("archive", filepath.Base(source))); err != nil {
				return nil, err
			}
		}
		if err := removeClaim(claim); err != nil {
			return nil, err
		}
		return map[string]any{"id": id, "state": "completed"}, nil
	})
}

func (s store) validate(id string) (map[string]any, error) {
	tasks, err := s.names("tasks")
	if err != nil {
		return nil, err
	}
	archive, err := s.names("archive")
	if err != nil {
		return nil, err
	}
	claims, err := s.names("claims")
	if err != nil {
		return nil, err
	}
	markers, err := s.markers()
	if err != nil {
		return nil, err
	}
	ids := map[string]string{}
	for _, m := range []map[string]string{tasks, archive, claims} {
		for key := range m {
			ids[key] = ""
		}
	}
	for key := range markers {
		ids[key] = ""
	}
	if id != "" {
		if !validID(id) {
			return nil, errors.New("ID must be 0001–9999")
		}
		if _, ok := ids[id]; !ok {
			return nil, fmt.Errorf("task not found: %s", id)
		}
		ids = map[string]string{id: ""}
	}
	errs := []string{}
	for _, key := range keys(ids) {
		for _, path := range []string{tasks[key], archive[key]} {
			if path != "" {
				if _, err := s.card(path); err != nil {
					errs = append(errs, err.Error())
				}
			}
		}
		if tasks[key] != "" && archive[key] != "" {
			errs = append(errs, key+": task and archive both present")
		}
		if tasks[key] != "" && markers[key] {
			errs = append(errs, key+": marker present, task not yet archived (incomplete completion)")
		}
		if archive[key] != "" && !markers[key] {
			errs = append(errs, key+": archive without marker")
		}
		if archive[key] != "" && claims[key] != "" {
			errs = append(errs, key+": archive still has claim (incomplete completion)")
		}
		if claims[key] != "" && tasks[key] == "" && archive[key] == "" {
			errs = append(errs, key+": orphaned claim")
		}
		if markers[key] && tasks[key] == "" && archive[key] == "" {
			errs = append(errs, key+": marker without task or archive")
		}
		if claims[key] != "" {
			data, err := os.ReadFile(filepath.Join(claims[key], "owner"))
			owner := strings.TrimSpace(string(data))
			if os.IsNotExist(err) {
				owner = ""
			} else if err != nil {
				errs = append(errs, err.Error())
				continue
			}
			if err := s.checkClaim(claims[key], owner); err != nil {
				errs = append(errs, err.Error())
			}
		}
	}
	return map[string]any{"valid": len(errs) == 0, "errors": errs}, nil
}

func run(args []string) (any, error) {
	global := flag.NewFlagSet("sov-task", flag.ContinueOnError)
	global.SetOutput(io.Discard)
	root := global.String("state-dir", "", "absolute shared .sov directory")
	if err := global.Parse(args); err != nil {
		return nil, err
	}
	if global.NArg() == 0 {
		return nil, errors.New("command required")
	}
	s, err := newStore(*root)
	if err != nil {
		return nil, err
	}
	command := global.Arg(0)
	rest := global.Args()[1:]
	switch command {
	case "create":
		f := flag.NewFlagSet(command, flag.ContinueOnError)
		f.SetOutput(io.Discard)
		file := f.String("file", "", "")
		slug := f.String("slug", "", "")
		if err := f.Parse(rest); err != nil {
			return nil, err
		}
		if *file == "" || *slug == "" || f.NArg() != 0 {
			return nil, errors.New("create requires --file and --slug")
		}
		return s.create(*file, *slug)
	case "claim", "claim-next", "release", "complete":
		id := ""
		if command != "claim-next" {
			if len(rest) == 0 || strings.HasPrefix(rest[0], "-") {
				return nil, errors.New("ID required")
			}
			id, rest = rest[0], rest[1:]
		}
		f := flag.NewFlagSet(command, flag.ContinueOnError)
		f.SetOutput(io.Discard)
		owner := f.String("owner", "", "")
		if err := f.Parse(rest); err != nil {
			return nil, err
		}
		if f.NArg() != 0 {
			return nil, errors.New("unexpected arguments")
		}
		if command == "release" {
			return s.release(id, *owner)
		}
		if command == "complete" {
			return s.complete(id, *owner)
		}
		if command == "claim" {
			path, err := s.locate(id)
			if err != nil {
				return nil, err
			}
			result, err := s.claim(path, *owner)
			if result == nil && err == nil {
				return map[string]any{"status": "not_ready", "id": id}, nil
			}
			return result, err
		}
		names, err := s.names("tasks")
		if err != nil {
			return nil, err
		}
		for _, key := range keys(names) {
			result, err := s.claim(names[key], *owner)
			if err != nil {
				return nil, err
			}
			if result != nil {
				return result, nil
			}
		}
		return map[string]any{"status": "no_ready_tasks"}, nil
	case "list", "list-ready", "show", "list-completed", "show-completed", "validate":
		id := ""
		if command == "show" || command == "show-completed" {
			if len(rest) != 1 {
				return nil, errors.New("ID required")
			}
			id = rest[0]
		} else if command == "validate" {
			if len(rest) > 1 {
				return nil, errors.New("unexpected arguments")
			}
			if len(rest) == 1 {
				id = rest[0]
			}
		} else if len(rest) != 0 {
			return nil, errors.New("unexpected arguments")
		}
		if command == "validate" {
			return s.validate(id)
		}
		if command == "list-completed" || command == "show-completed" {
			rows, err := s.completedRows(id)
			if err != nil {
				return nil, err
			}
			if id != "" {
				return rows[0], nil
			}
			return rows, nil
		}
		markers, err := s.markers()
		if err != nil {
			return nil, err
		}
		if command == "show" {
			path, err := s.locate(id)
			if err != nil {
				return nil, err
			}
			return s.row(path, markers)
		}
		names, err := s.names("tasks")
		if err != nil {
			return nil, err
		}
		rows := make([]map[string]any, 0, len(names))
		for _, key := range keys(names) {
			row, err := s.row(names[key], markers)
			if err != nil {
				return nil, err
			}
			if command == "list" || row["state"] == "READY" {
				rows = append(rows, row)
			}
		}
		return rows, nil
	default:
		return nil, fmt.Errorf("unknown command: %s", command)
	}
}

func main() {
	result, err := run(os.Args[1:])
	if err != nil {
		json.NewEncoder(os.Stderr).Encode(map[string]any{"error": err.Error()})
		os.Exit(2)
	}
	json.NewEncoder(os.Stdout).Encode(result)
	if value, ok := result.(map[string]any); ok && value["valid"] == false {
		os.Exit(2)
	}
}
