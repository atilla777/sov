#!/usr/bin/env python3
"""Read-only, whole-pair semantic view of the SOV 0.3.0 file task source."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HEADER = ["ID", "Название", "Статус", "Исполнитель", "Координатор", "Зависит от", "Публикация"]
STATUSES = {"planned", "in_progress", "blocked", "done", "cancelled"}
TERMINAL = {"done", "cancelled"}
ID = re.compile(r"^([A-Z][A-Z0-9]*-)([0-9]+)$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class ViewError(Exception):
    def __init__(self, code, message, diagnostics=None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []


def transport(cli, command, state_dir):
    try:
        result = subprocess.run([cli, command, "--state-dir", state_dir], capture_output=True, timeout=10, check=False)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ViewError("transport_error", str(exc)) from exc
    resource = "ROADMAP.md" if command == "read" else "ARCHIVE.md"
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("resource") != resource:
        raise ViewError("transport_error", "unexpected sov-state response")
    if result.returncode or payload.get("status") != "ok":
        raise ViewError("transport_error", f"{resource}: {payload.get('code', 'unknown')}: {payload.get('message', '')}")
    try:
        raw = base64.b64decode(payload["content_base64"], validate=True)
        if payload["revision"] != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("revision does not match bytes")
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ViewError("transport_error", f"{resource}: invalid bytes or revision: {exc}") from exc
    return raw, payload["revision"]


def snapshot(cli, state_dir):
    # Reads are individually atomic but NOT a two-file transaction. Recheck both.
    for attempt in range(3):
        first = (transport(cli, "read", state_dir), transport(cli, "read-archive", state_dir))
        second = (transport(cli, "read", state_dir), transport(cli, "read-archive", state_dir))
        if first == second:
            return first
        if attempt < 2:
            time.sleep(0.01 * (attempt + 1))
    raise ViewError("unstable_snapshot", "ROADMAP/ARCHIVE changed during three snapshot attempts")


def cells(line):
    if not line.startswith("|") or not line.endswith("|"):
        return None
    return [part.strip() for part in line[1:-1].split("|")]


def parse(raw, name, diagnostics):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        diagnostics.append({"file": name, "line": 1, "code": "encoding", "message": "expected UTF-8"})
        return [], None
    lines = text.splitlines()
    title = "# Список задач SOV" if name == "ROADMAP.md" else "# Архив задач SOV"
    if not lines or lines[0] != title:
        diagnostics.append({"file": name, "line": 1, "code": "title", "message": "unexpected title"})
    counter = None
    counters = [(i, re.fullmatch(r"Следующий ID: `([^`]+)`", line)) for i, line in enumerate(lines, 1) if line.startswith("Следующий ID:")]
    if name == "ROADMAP.md":
        if len(counters) != 1 or counters[0][1] is None or not ID.fullmatch(counters[0][1].group(1)):
            diagnostics.append({"file": name, "line": counters[0][0] if counters else 1, "code": "counter", "message": "expected one valid Следующий ID"})
        else:
            counter = counters[0][1].group(1)
    elif counters:
        diagnostics.append({"file": name, "line": counters[0][0], "code": "counter", "message": "archive must not have a counter"})
    tables = [i for i, line in enumerate(lines) if cells(line) == HEADER]
    if len(tables) != 1 or tables[0] + 1 >= len(lines) or cells(lines[tables[0] + 1]) != ["---"] * 7:
        diagnostics.append({"file": name, "line": tables[0] + 1 if tables else 1, "code": "table", "message": "expected one seven-column table and separator"})
        return [], counter
    start = tables[0] + 2
    rows = []
    end = start
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip():
            break
        end = i + 1
        values = cells(line)
        if values is None or len(values) != 7:
            diagnostics.append({"file": name, "line": i + 1, "code": "row", "message": "expected seven cells"})
            continue
        row = dict(zip(("id", "title", "status", "owner", "coordinator", "dependencies", "publication"), values))
        row.update(line=i + 1, location="queue" if name == "ROADMAP.md" else "archive", position=len(rows) + 1 if name == "ROADMAP.md" else None, _raw_line=line)
        row["depends_on"] = [] if row.pop("dependencies") == "—" else [x.strip() for x in values[5].split(",")]
        rows.append(row)
        issues = []
        if not ID.fullmatch(row["id"]): issues.append("invalid ID")
        if not row["title"] or row["title"] == "—": issues.append("empty title")
        if row["status"] not in STATUSES or (name == "ARCHIVE.md" and row["status"] not in TERMINAL): issues.append("invalid status")
        if not row["owner"] or "|" in row["owner"]: issues.append("invalid owner")
        if row["status"] == "planned" and (row["owner"] != "—" or row["coordinator"] != "—"): issues.append("planned task has owner or claim")
        if row["status"] == "in_progress" and row["owner"] == "—": issues.append("in_progress without owner")
        if row["status"] != "in_progress" and row["coordinator"] != "—": issues.append("claim on non-active task")
        if row["coordinator"] != "—" and not UUID.fullmatch(row["coordinator"]): issues.append("invalid session UUID")
        if row["publication"] not in {"yes", "no"}: issues.append("invalid publication")
        if len(set(row["depends_on"])) != len(row["depends_on"]) or any(not ID.fullmatch(x) for x in row["depends_on"]): issues.append("invalid dependencies")
        for issue in issues:
            diagnostics.append({"file": name, "line": i + 1, "id": row["id"], "code": "field", "message": issue})
    # A second table or a stray row after the first table cannot be ignored.
    for i in range(end, len(lines)):
        if lines[i].startswith("|"):
            diagnostics.append({"file": name, "line": i + 1, "code": "table", "message": "unexpected table row outside task table"})
    return rows, counter


def validate(road, archive, counter, diagnostics):
    queue = {r["id"]: r for r in road}
    stored = {r["id"]: r for r in archive}
    for name, rows in (("ROADMAP.md", road), ("ARCHIVE.md", archive)):
        seen = set()
        for row in rows:
            if row["id"] in seen:
                diagnostics.append({"file": name, "line": row["line"], "id": row["id"], "code": "duplicate_id", "message": "ID repeated within file"})
            seen.add(row["id"])
    if counter and ID.fullmatch(counter):
        prefix, number = ID.fullmatch(counter).groups()
        for row in road + archive:
            match = ID.fullmatch(row["id"])
            if match and (match[1] != prefix or int(match[2]) >= int(number)):
                diagnostics.append({"file": "ROADMAP.md" if row["location"] == "queue" else "ARCHIVE.md", "line": row["line"], "id": row["id"], "code": "counter", "message": "ID prefix or next number inconsistent"})
    for row in road + archive:
        for dep in row["depends_on"]:
            if (row["location"] == "queue" and dep not in queue) or (row["location"] == "archive" and dep not in queue and dep not in stored):
                diagnostics.append({"file": "ROADMAP.md" if row["location"] == "queue" else "ARCHIVE.md", "line": row["line"], "id": row["id"], "code": "dependency", "message": f"unresolved dependency {dep}"})
            if row["id"] == dep:
                diagnostics.append({"file": "ROADMAP.md" if row["location"] == "queue" else "ARCHIVE.md", "line": row["line"], "id": row["id"], "code": "cycle", "message": "self dependency"})
    visiting, visited = set(), set()
    for root in queue:
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            key, leaving = stack.pop()
            if leaving:
                visiting.remove(key)
                visited.add(key)
            elif key in visiting:
                diagnostics.append({"file": "ROADMAP.md", "line": queue[key]["line"], "id": key, "code": "cycle", "message": "dependency cycle"})
            elif key not in visited:
                visiting.add(key)
                stack.append((key, True))
                stack.extend((dep, False) for dep in queue[key]["depends_on"] if dep in queue)
    recovering = []
    for key in queue.keys() & stored.keys():
        a, b = queue[key], stored[key]
        same = a["_raw_line"] == b["_raw_line"]
        if same and a["status"] in TERMINAL and not any(key in row["depends_on"] for row in road if row["id"] != key):
            recovering.append({"file": "ROADMAP.md", "line": a["line"], "id": key, "code": "archive_duplicate", "message": "identical terminal row in both files; finish archive transfer before further operations"})
        else:
            diagnostics.append({"file": "ROADMAP.md", "line": a["line"], "id": key, "code": "duplicate_id", "message": "different or still referenced archive row"})
    return recovering


def project_row(row, state_dir, queue):
    result = dict(row)
    del result["_raw_line"]
    result["artifact_dir"] = os.path.join(state_dir, "tasks", row["id"])
    reasons = []
    if row["location"] != "queue": reasons.append("archived")
    elif row["status"] != "planned": reasons.append("status:" + row["status"])
    elif row["owner"] != "—": reasons.append("assigned")
    for dep in row["depends_on"]:
        if dep in queue and queue[dep]["status"] != "done": reasons.append("dependency:" + dep + ":" + queue[dep]["status"])
    result["structurally_available"] = not reasons
    result["reasons"] = reasons
    result["external_start_conditions_checked"] = False
    return result


def execute(argv):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ViewError("invalid_input", message)
    parser = Parser(description=__doc__)
    parser.add_argument("command", choices=("validate", "get", "owner", "queue"))
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--cli", required=True, help="absolute path to sov-state 0.3.0")
    parser.add_argument("--id")
    parser.add_argument("--owner")
    args = parser.parse_args(argv)
    if not os.path.isabs(args.state_dir) or not os.path.isabs(args.cli) or (args.command == "get" and (not args.id or not ID.fullmatch(args.id))) or (args.command == "owner" and (not args.owner or args.owner == "—")) or (args.command != "get" and args.id) or (args.command != "owner" and args.owner):
        raise ViewError("invalid_input", "absolute paths and command-specific --id/--owner required")
    try:
        version = subprocess.run([args.cli, "--version"], capture_output=True, timeout=10, check=False)
        info = json.loads(version.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ViewError("transport_error", str(exc)) from exc
    if version.returncode or not isinstance(info, dict) or info.get("v") != 1 or info.get("status") != "ok" or info.get("resource") != "ROADMAP.md" or info.get("message") != "sov-state 0.3.0 (protocol v:1)":
        raise ViewError("transport_error", "expected sov-state 0.3.0 (protocol v:1)")
    (raw, road_rev), (archived, archive_rev) = snapshot(args.cli, args.state_dir)
    diagnostics = []
    road, counter = parse(raw, "ROADMAP.md", diagnostics)
    archive, _ = parse(archived, "ARCHIVE.md", diagnostics)
    recovering = validate(road, archive, counter, diagnostics)
    revisions = {"roadmap_revision": road_rev, "archive_revision": archive_rev}
    if diagnostics:
        raise ViewError("invalid_state", "ROADMAP/ARCHIVE validation failed", diagnostics)
    if recovering:
        raise ViewError("recovery_required", "complete interrupted archive transfer before using tasks", recovering)
    queue = {r["id"]: r for r in road}
    state_dir = os.path.realpath(args.state_dir)
    if args.command == "validate":
        data = {"valid": True, "queue_count": len(road), "archive_count": len(archive), "next_id": counter, "diagnostics": []}
    elif args.command == "get":
        row = queue.get(args.id) or next((r for r in archive if r["id"] == args.id), None)
        if row is None: raise ViewError("not_found", f"task {args.id} not found")
        data = {"task": project_row(row, state_dir, queue)}
    else:
        selected = road if args.command == "queue" else [r for r in road if r["owner"] == args.owner and r["status"] in {"in_progress", "blocked"}]
        data = {"tasks": [project_row(row, state_dir, queue) for row in selected]}
    return {"v": 1, "status": "ok", "command": args.command, **revisions, **data}


if __name__ == "__main__":
    try:
        response = execute(sys.argv[1:])
        code = 0
    except ViewError as exc:
        response = {"v": 1, "status": "error", "code": exc.code, "message": str(exc), "diagnostics": exc.diagnostics}
        code = {"invalid_input": 12, "transport_error": 13, "invalid_state": 14, "unstable_snapshot": 15, "recovery_required": 16, "not_found": 17}[exc.code]
    print(json.dumps(response, ensure_ascii=False))
    sys.exit(code)
