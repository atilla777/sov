#!/usr/bin/env python3
"""Local filesystem task operations for one shared SOV project directory."""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sys

import yaml


ID = re.compile(r"[0-9]{4}\Z")
NAME = re.compile(r"([0-9]{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\Z")
FIELDS = ("id", "title", "description", "depends_on", "acceptance_criteria")
RUNTIME = {"status", "claimed_by", "claimed_at", "completed_at"}


class TaskError(Exception):
    pass


class StrictLoader(yaml.SafeLoader):
    pass


def mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise TaskError("duplicate or non-string YAML key")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def parse(data):
    try:
        return yaml.load(data, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise TaskError(f"invalid YAML: {exc}") from exc


def valid_id(value):
    return isinstance(value, str) and bool(ID.fullmatch(value)) and value != "0000"


def check_card(card, name):
    if not isinstance(card, dict):
        raise TaskError(f"{name}: expected YAML mapping")
    if not NAME.fullmatch(name):
        raise TaskError(f"invalid task directory name: {name}")
    if any(field not in card for field in FIELDS):
        raise TaskError(f"{name}: missing fields: {', '.join(f for f in FIELDS if f not in card)}")
    if not valid_id(card["id"]) or card["id"] != name[:4]:
        raise TaskError(f"{name}: id must be a matching four-digit string (0001–9999)")
    for field in ("title", "description"):
        if not isinstance(card[field], str) or not card[field].strip():
            raise TaskError(f"{name}: {field} must be a nonempty string")
    for field in ("depends_on", "acceptance_criteria"):
        if not isinstance(card[field], list):
            raise TaskError(f"{name}: {field} must be a list")
    if not card["acceptance_criteria"] or any(not isinstance(c, str) or not c.strip() for c in card["acceptance_criteria"]):
        raise TaskError(f"{name}: acceptance_criteria must contain nonempty strings")
    if any(not valid_id(dep) or dep == card["id"] for dep in card["depends_on"]):
        raise TaskError(f"{name}: depends_on contains an invalid or self ID")
    if len(set(card["depends_on"])) != len(card["depends_on"]):
        raise TaskError(f"{name}: duplicate dependencies")
    if RUNTIME.intersection(card):
        raise TaskError(f"{name}: runtime fields belong to the filesystem")


def directory(path):
    if path.is_symlink() or not path.is_dir():
        raise TaskError(f"expected directory without symlink: {path}")


def regular(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise TaskError(f"expected regular file without links: {path}")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        directory(self.root)
        self.paths = {kind: self.root / kind for kind in ("tasks", "claims", "completed", "archive")}
        for path in self.paths.values():
            directory(path)

    def entries(self, kind):
        return sorted(self.paths[kind].iterdir(), key=lambda p: p.name)

    def names(self, kind):
        names = {}
        for path in self.entries(kind):
            match = NAME.fullmatch(path.name)
            if not match:
                raise TaskError(f"unexpected entry in {kind}: {path.name}")
            directory(path)
            id_ = match.group(1)
            if id_ in names:
                raise TaskError(f"duplicate ID in {kind}: {id_}")
            names[id_] = path
        return names

    def markers(self):
        result = set()
        for path in self.entries("completed"):
            if not valid_id(path.name):
                raise TaskError(f"invalid completion marker: {path.name}")
            regular(path)
            result.add(path.name)
        return result

    def card(self, path):
        directory(path)
        file = path / "task.yaml"
        regular(file)
        card = parse(file.read_text(encoding="utf-8"))
        check_card(card, path.name)
        return card

    def locate(self, id_):
        if not valid_id(id_):
            raise TaskError("ID must be 0001–9999")
        path = self.names("tasks").get(id_)
        if path is None:
            raise TaskError(f"task not found: {id_}")
        return path

    def state(self, path, card, markers):
        id_ = card["id"]
        if id_ in markers:
            return "INCOMPLETE_COMPLETION"
        if (self.paths["claims"] / path.name).exists():
            return "IN PROGRESS"
        if all(dep in markers for dep in card["depends_on"]):
            return "READY"
        return "BLOCKED"

    def row(self, path, markers):
        card = self.card(path)
        return {"id": card["id"], "name": path.name, "state": self.state(path, card, markers), "task": card}

    def completed_rows(self, id_=None):
        if id_ is not None and not valid_id(id_):
            raise TaskError("ID must be 0001–9999")
        tasks = self.names("tasks")
        archive = self.names("archive")
        claims = self.names("claims")
        markers = self.markers()
        ids = sorted(set(archive) | markers) if id_ is None else [id_]
        rows = []
        for key in ids:
            path = archive.get(key)
            if key in tasks and path is not None:
                raise TaskError(f"{key}: task and archive both present")
            if key in tasks and key in markers:
                raise TaskError(f"{key}: marker present, task not yet archived (incomplete completion)")
            if path is None:
                if key in markers:
                    raise TaskError(f"{key}: marker without archive (incomplete completion)")
                raise TaskError(f"completed task not found: {key}")
            if key not in markers:
                raise TaskError(f"{key}: archive without marker")
            if key in claims:
                raise TaskError(f"{key}: archive still has claim (incomplete completion)")
            card = self.card(path)
            rows.append({"id": key, "name": path.name, "state": "COMPLETED", "task": card})
        return rows

    @contextlib.contextmanager
    def create_lock(self):
        lock = self.root / ".tasks.lock"
        if lock.exists() or lock.is_symlink():
            regular(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def create(self, args):
        data = parse(Path(args.file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TaskError("expected YAML mapping")
        with self.create_lock():
            tasks = self.names("tasks")
            archive = self.names("archive")
            claims = self.names("claims")
            markers = self.markers()
            used = set(tasks) | set(archive) | set(claims) | markers
            if "id" in data:
                if not valid_id(data["id"]):
                    raise TaskError("id must be a four-digit YAML string")
                id_ = data["id"]
                if id_ in used:
                    raise TaskError(f"ID already used: {id_}")
            else:
                next_id = max((int(i) for i in used), default=0) + 1
                if next_id > 9999:
                    raise TaskError("ID space exhausted")
                id_ = f"{next_id:04d}"
                data["id"] = id_
            slug = args.slug
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                raise TaskError("slug must be lowercase ASCII words separated by hyphens")
            name = id_ + "-" + slug
            check_card(data, name)
            path = self.paths["tasks"] / name
            path.mkdir()
            try:
                # A crash after mkdir leaves an invalid entry for validate to report.
                with (path / "task.yaml").open("x", encoding="utf-8") as output:
                    yaml.safe_dump(data, output, allow_unicode=True, sort_keys=False)
            except Exception:
                (path / "task.yaml").unlink(missing_ok=True)
                path.rmdir()
                raise
            return {"id": id_, "name": name}

    def claim(self, path, owner):
        markers = self.markers()
        card = self.card(path)
        if self.state(path, card, markers) != "READY":
            return None
        claim = self.paths["claims"] / path.name
        try:
            claim.mkdir()
        except FileExistsError:
            return None
        try:
            # Check again after acquiring the lock, including a concurrent completion.
            if self.names("tasks").get(card["id"]) != path or self.state(path, card, self.markers()) != "IN PROGRESS" or not all(d in self.markers() for d in card["depends_on"]):
                claim.rmdir()
                return None
            if owner:
                (claim / "owner").write_text(owner + "\n", encoding="utf-8")
            (claim / "claimed_at").write_text(datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n", encoding="utf-8")
        except Exception:
            for file in claim.iterdir():
                file.unlink()
            claim.rmdir()
            raise
        return {"id": card["id"], "name": path.name, "state": "IN PROGRESS"}

    def check_claim(self, path, owner):
        directory(path)
        allowed = {"owner", "claimed_at"}
        for file in path.iterdir():
            if file.name not in allowed:
                raise TaskError(f"unexpected claim entry: {file}")
            regular(file)
        file = path / "owner"
        if file.exists():
            if not owner or file.read_text(encoding="utf-8").strip() != owner:
                raise TaskError("claim owner mismatch; inspect before manual recovery")

    def release(self, id_, owner):
        with self.create_lock():
            return self._release_locked(id_, owner)

    def _release_locked(self, id_, owner):
        path = self.locate(id_)
        if (self.paths["completed"] / id_).exists():
            raise TaskError("completion started; use complete to finish the transfer")
        claim = self.paths["claims"] / path.name
        self.check_claim(claim, owner)
        for file in claim.iterdir():
            file.unlink()
        claim.rmdir()
        return {"id": id_, "state": "released"}

    def complete(self, id_, owner):
        with self.create_lock():
            return self._complete_locked(id_, owner)

    def _complete_locked(self, id_, owner):
        if not valid_id(id_):
            raise TaskError("ID must be 0001–9999")
        tasks = self.names("tasks")
        archive = self.names("archive")
        source, dest = tasks.get(id_), archive.get(id_)
        marker = self.paths["completed"] / id_
        if source and dest:
            raise TaskError("task and archive both exist; manual inspection required")
        if not source and not dest:
            raise TaskError("task and archive absent; manual inspection required")
        if not source and not marker.exists():
            raise TaskError("archive without marker; manual inspection required")
        path = source or dest
        self.card(path)
        claim = self.paths["claims"] / path.name
        self.check_claim(claim, owner)
        if marker.exists():
            regular(marker)
        elif not source:
            raise TaskError("cannot create marker without active task")
        else:
            # Only the caller may declare publication confirmed; no automatic Git check.
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        if source:
            source.rename(self.paths["archive"] / source.name)
        for file in claim.iterdir():
            file.unlink()
        claim.rmdir()
        return {"id": id_, "state": "completed"}

    def validate(self, id_=None):
        tasks = self.names("tasks")
        archive = self.names("archive")
        claims = self.names("claims")
        markers = self.markers()
        errors = []
        ids = set(tasks) | set(archive) | set(claims) | markers
        if id_ is not None:
            if not valid_id(id_):
                raise TaskError("ID must be 0001–9999")
            if id_ not in ids:
                raise TaskError(f"task not found: {id_}")
            ids = {id_}
        for key in sorted(ids):
            for kind, entries in (("tasks", tasks), ("archive", archive)):
                if key in entries:
                    try:
                        self.card(entries[key])
                    except (TaskError, OSError, UnicodeError) as exc:
                        errors.append(str(exc))
            if key in tasks and key in archive:
                errors.append(f"{key}: task and archive both present")
            if key in tasks and key in markers:
                errors.append(f"{key}: marker present, task not yet archived (incomplete completion)")
            if key in archive and key not in markers:
                errors.append(f"{key}: archive without marker")
            if key in archive and key in claims:
                errors.append(f"{key}: archive still has claim (incomplete completion)")
            if key in claims and key not in tasks and key not in archive:
                errors.append(f"{key}: orphaned claim")
            if key in markers and key not in tasks and key not in archive:
                errors.append(f"{key}: marker without task or archive")
            if key in claims:
                try:
                    owner_file = claims[key] / "owner"
                    owner = owner_file.read_text(encoding="utf-8").strip() if owner_file.exists() else None
                    self.check_claim(claims[key], owner)
                except (TaskError, OSError, UnicodeError) as exc:
                    errors.append(str(exc))
        return {"valid": not errors, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path, help="absolute path to the shared .sov directory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "list-ready", "list-completed", "claim-next", "validate", "show", "show-completed", "claim", "release", "complete", "create"):
        sub = commands.add_parser(name)
        if name in ("show", "show-completed", "claim", "release", "complete"):
            sub.add_argument("id")
        if name == "validate":
            sub.add_argument("id", nargs="?")
        if name in ("claim", "claim-next", "release", "complete"):
            sub.add_argument("--owner", help="diagnostic owner; required to match if stored in claim")
        if name == "create":
            sub.add_argument("--file", required=True, help="YAML task card (id optional; allocated if absent)")
            sub.add_argument("--slug", required=True)
    args = parser.parse_args(argv)
    try:
        if not args.state_dir.is_absolute():
            raise TaskError("--state-dir must be absolute")
        store = Store(args.state_dir)
        cmd = args.command
        if cmd == "create":
            result = store.create(args)
        elif cmd == "validate":
            result = store.validate(args.id)
        elif cmd in ("list", "list-ready"):
            markers = store.markers()
            result = [store.row(path, markers) for path in store.names("tasks").values()]
            if cmd == "list-ready":
                result = [row for row in result if row["state"] == "READY"]
        elif cmd == "show":
            result = store.row(store.locate(args.id), store.markers())
        elif cmd == "list-completed":
            result = store.completed_rows()
        elif cmd == "show-completed":
            result = store.completed_rows(args.id)[0]
        elif cmd == "claim":
            result = store.claim(store.locate(args.id), args.owner) or {"status": "not_ready", "id": args.id}
        elif cmd == "claim-next":
            result = {"status": "no_ready_tasks"}
            for path in store.names("tasks").values():
                claimed = store.claim(path, args.owner)
                if claimed:
                    result = claimed
                    break
        elif cmd == "release":
            result = store.release(args.id, args.owner)
        else:
            result = store.complete(args.id, args.owner)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if cmd == "validate" and not result["valid"] else 0
    except (TaskError, OSError, UnicodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
