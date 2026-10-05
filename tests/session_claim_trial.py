"""Isolated four-session task-claim trial; never touches a connected project.

Run: python3 tests/session_claim_trial.py /absolute/path/to/sov-state
The CLI transports bytes; this fixture also checks the sov-tasks claim rules.
"""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def call(binary, *args):
    result = subprocess.run([binary, *map(str, args)], capture_output=True, text=True)
    return result.returncode, json.loads(result.stdout)


def read(binary, state, resource="read"):
    code, r = call(binary, resource, "--state-dir", state)
    assert code == 0 and r["status"] == "ok", r
    return base64.b64decode(r["content_base64"]).decode(), r["revision"]


def write(binary, state, cwd, text, expected, name, command="commit", archive=None):
    path = cwd / name
    path.write_text(text)
    args = [command, "--state-dir", state, "--expected", expected, "--input", path]
    if archive is not None:
        archive_text, archive_expected = archive
        archive_path = cwd / (name + ".archive")
        archive_path.write_text(archive_text)
        args += ["--archive-expected", archive_expected, "--archive-input", archive_path]
    return call(binary, *args)


def session(binary):
    code, r = call(binary, "session-id")
    assert code == 0 and r["v"] == 1 and r["resource"] == "SESSION", r
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", r["session_id"])
    return r["session_id"]


def row(number, status="planned", owner="—", holder="—"):
    return f"| TASK-{number:03d} | Job {number} | {status} | {owner} | {holder} | — | no |"


def worker(binary, state, cwd, ready, gate, number):
    sid = session(binary)
    text, rev = read(binary, state)
    ready.touch()
    while not gate.exists():
        time.sleep(0.01)
    for attempt in range(4):
        old = row(number)
        assert old in text, (number, text)
        new = row(number, "in_progress", "aleksei", sid)
        result, payload = write(binary, state, cwd, text.replace(old, new), rev, f"worker-{number}-{attempt}.md")
        if result == 0:
            assert payload["changed"]
            return
        assert result == 10 and payload["code"] == "revision_conflict", payload
        time.sleep(0.005 * number)
        text, rev = read(binary, state)
    raise AssertionError("conflict retry budget exhausted")


def main(binary):
    code, version = call(binary, "--version")
    assert code == 0 and version["message"] == "sov-state 0.3.0 (protocol v:1)"
    with tempfile.TemporaryDirectory(prefix="sov-sessions-") as temp:
        root = Path(temp)
        state = root / ".sov"
        state.mkdir()
        (state / ".roadmap.lock").touch(mode=0o600)
        header = "# Tasks\n\nСледующий ID: `TASK-005`\n\n| ID | Название | Статус | Исполнитель | Координатор | Зависит от | Публикация |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        road = state / "ROADMAP.md"
        road.write_text(header + "\n".join(row(i) for i in range(1, 5)) + "\n")
        (state / "ARCHIVE.md").write_text("# Archive\n\n| ID | Название | Статус | Исполнитель | Координатор | Зависит от | Публикация |\n| --- | --- | --- | --- | --- | --- | --- |\n")
        mutex = (state / ".roadmap.lock").stat().st_ino
        gate = root / "gate"
        ready = [root / f"ready-{i}" for i in range(1, 5)]
        processes = [subprocess.Popen([sys.executable, __file__, "--worker", binary, str(state),
                                       str(root), str(ready[i - 1]), str(gate), str(i)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for i in range(1, 5)]
        try:
            deadline = time.monotonic() + 10
            while not all(p.exists() for p in ready):
                assert time.monotonic() < deadline, "workers failed to reach barrier"
                time.sleep(0.01)
            gate.touch()
            for p in processes:
                out, err = p.communicate(timeout=15)
                assert p.returncode == 0, (p.returncode, out, err)
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
        text, rev = read(binary, state)
        claims = re.findall(r"\| TASK-00[1-4] \| Job [1-4] \| in_progress \| aleksei \| ([a-f0-9-]{36}) \|", text)
        assert len(claims) == len(set(claims)) == 4 and "Следующий ID: `TASK-005`" in text
        print("PASS: four processes claim distinct tasks for one stable owner")

        # Two new sessions race for the same unclaimed job. A matching owner
        # alone does not grant permission to work on an already held job.
        released = text.replace(row(1, "in_progress", "aleksei", claims[0]), row(1, "in_progress", "aleksei"))
        assert write(binary, state, root, released, rev, "release.md")[0] == 0
        text, rev = read(binary, state)
        contenders = [session(binary), session(binary)]
        outcomes = [write(binary, state, root, text.replace(row(1, "in_progress", "aleksei"),
                                                  row(1, "in_progress", "aleksei", sid)),
                          rev, f"contender-{i}.md") for i, sid in enumerate(contenders)]
        assert [code for code, _ in outcomes].count(0) == 1
        assert [code for code, _ in outcomes].count(10) == 1
        text, rev = read(binary, state)
        winner = contenders[0]
        assert row(1, "in_progress", "aleksei", winner) in text
        assert row(1, "in_progress", "aleksei", contenders[1]) not in text
        print("PASS: one claim succeeds; stale revision cannot take the same task")

        # Recovery after a lost reply: fresh read confirms winner and no
        # second claim is made. A later session may take over only after the
        # old one is confirmed stopped (confirmation is external to CLI).
        next_id = session(binary)
        assert next_id not in text
        assert row(1, "in_progress", "aleksei", winner) in text
        handed = text.replace(row(1, "in_progress", "aleksei", winner),
                              row(1, "in_progress", "aleksei", next_id))
        assert write(binary, state, root, handed, rev, "handoff.md")[0] == 0
        text, rev = read(binary, state)
        assert row(1, "in_progress", "aleksei", next_id) in text
        print("PASS: explicit handoff preserves owner and prevents blind retry")

        blocked = text.replace(row(2, "in_progress", "aleksei", claims[1]), row(2, "blocked", "aleksei"))
        assert write(binary, state, root, blocked, rev, "blocked.md")[0] == 0
        text, rev = read(binary, state)
        resumed_id = session(binary)
        resumed = text.replace(row(2, "blocked", "aleksei"), row(2, "in_progress", "aleksei", resumed_id))
        assert write(binary, state, root, resumed, rev, "resume.md")[0] == 0
        text, rev = read(binary, state)
        assert row(2, "in_progress", "aleksei", resumed_id) in text
        print("PASS: blocked task releases old claim and resumes with a new session")

        # Complete task 1 and archive it without changing the other claims.
        done = text.replace(row(1, "in_progress", "aleksei", next_id), row(1, "done", "aleksei"))
        assert write(binary, state, root, done, rev, "done.md")[0] == 0
        text, rev = read(binary, state)
        archived, ar = read(binary, state, "read-archive")
        assert write(binary, state, root, text.replace(row(1, "done", "aleksei") + "\n", ""),
                     rev, "short.md", "archive", (archived + row(1, "done", "aleksei") + "\n", ar))[0] == 0
        text, _ = read(binary, state)
        archived, _ = read(binary, state, "read-archive")
        assert "TASK-001" not in text and archived.count("TASK-001") == 1
        assert row(2, "in_progress", "aleksei", resumed_id) in text
        assert all(row(i, "in_progress", "aleksei", claims[i - 1]) in text for i in range(3, 5))
        assert (state / ".roadmap.lock").stat().st_ino == mutex
        print("PASS: archive retains terminal task and other live claims")

        # Offline migration of a pre-claim project keeps its task IDs, owner,
        # next ID, and archive history. No writers run during this transfer.
        legacy = root / "legacy"
        legacy.mkdir()
        (legacy / ".roadmap.lock").touch(mode=0o600)
        old_header = header.replace(" | Координатор |", " |").replace("| --- | --- | --- | --- | --- | --- | --- |",
                                                               "| --- | --- | --- | --- | --- | --- |")
        old_road = old_header.replace("TASK-005", "TASK-003") + "| TASK-002 | Job 2 | planned | — | — | no |\n"
        old_arch = "# Archive\n\n| ID | Название | Статус | Исполнитель | Зависит от | Публикация |\n| --- | --- | --- | --- | --- | --- |\n| TASK-001 | Job 1 | done | aleksei | — | no |\n"
        (legacy / "ROADMAP.md").write_text(old_road)
        (legacy / "ARCHIVE.md").write_text(old_arch)
        new_road = old_header.replace("TASK-005", "TASK-003").replace(" | Зависит от |", " | Координатор | Зависит от |")
        new_road = new_road.replace("| --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |")
        new_road += row(2) + "\n"
        new_arch = old_arch.replace(" | Зависит от |", " | Координатор | Зависит от |")
        new_arch = new_arch.replace("| --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |")
        new_arch = new_arch.replace("| done | aleksei | — |", "| done | aleksei | — | — |")
        ar = read(binary, legacy, "read-archive")[1]
        result, payload = write(binary, legacy, root, new_road, read(binary, legacy)[1],
                                "migrate.md", "archive", (new_arch, ar))
        assert result == 0 and payload["changed"], payload
        assert read(binary, legacy)[0] == new_road
        assert read(binary, legacy, "read-archive")[0] == new_arch
        assert "Следующий ID: `TASK-003`" in new_road and "TASK-002" in new_road and "TASK-001" in new_arch
        print("PASS: offline two-file migration preserves archived and planned IDs")


if __name__ == "__main__":
    if len(sys.argv) == 8 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]),
               Path(sys.argv[6]), int(sys.argv[7]))
    elif len(sys.argv) == 2 and os.path.isabs(sys.argv[1]):
        main(sys.argv[1])
    else:
        sys.exit("usage: python3 tests/session_claim_trial.py /absolute/path/to/sov-state")
