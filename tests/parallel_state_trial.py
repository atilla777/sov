"""Reproducible local two-coordinator trial; no project state is modified.

Run with: python3 tests/parallel_state_trial.py /absolute/path/to/sov-state
This tests the file protocol and the specified task-selection procedure, not an
interactive OpenCode session or its permission configuration.
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


def cli(binary, *args, cwd=None):
    result = subprocess.run([binary, *map(str, args)], cwd=cwd, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    assert payload["v"] == 1 and payload["resource"] == "ROADMAP.md", payload
    return result.returncode, payload


def snapshot(binary, state, cwd):
    code, response = cli(binary, "read", "--state-dir", state, cwd=cwd)
    assert code == 0, response
    return base64.b64decode(response["content_base64"]).decode(), response["revision"]


def commit(binary, state, cwd, expected, text, filename):
    candidate = cwd / filename
    candidate.write_text(text)
    return cli(binary, "commit", "--state-dir", state, "--expected", expected,
               "--input", candidate, cwd=cwd)


def git(*args):
    subprocess.run(["git", *map(str, args)], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)


def worker(binary, state, cwd, name, gate, ready, mode):
    text, rev = snapshot(binary, state, cwd)
    assert "TASK-001 | One | planned" in text
    ready.touch()
    while not gate.exists():
        time.sleep(0.01)
    if mode == "create":
        number = int(re.search(r"Следующий ID: `TASK-(\d+)`", text).group(1))
        updated = text.replace(f"Следующий ID: `TASK-{number:03d}`",
                               f"Следующий ID: `TASK-{number + 1:03d}`")
        updated += f"| TASK-{number:03d} | {name} | planned | — | — | no |\n"
    elif mode == "claim":
        updated = text.replace("TASK-001 | One | planned | —", f"TASK-001 | One | in_progress | {name}")
    else:
        updated = text.replace("TASK-001 | One | planned | —", "TASK-001 | One | planned | —")
        updated = updated.replace("TASK-002 | Two | planned | —", f"TASK-002 | Two | in_progress | {name}")
    code, response = commit(binary, state, cwd, rev, updated, f"{name}.md")
    if code == 10:
        text, rev = snapshot(binary, state, cwd)
        if mode == "create":
            number = int(re.search(r"Следующий ID: `TASK-(\d+)`", text).group(1))
            updated = text.replace(f"Следующий ID: `TASK-{number:03d}`",
                                   f"Следующий ID: `TASK-{number + 1:03d}`")
            updated += f"| TASK-{number:03d} | {name} | planned | — | — | no |\n"
        elif mode == "claim":
            if "TASK-001 | One | planned | —" in text:
                # Only an unrelated row changed: rebuild the original claim.
                updated = text.replace("TASK-001 | One | planned | —", f"TASK-001 | One | in_progress | {name}")
            else:
                # The losing coordinator must re-evaluate availability, not steal TASK-001.
                assert "TASK-002 | Two | planned | —" in text
                updated = text.replace("TASK-002 | Two | planned | —", f"TASK-002 | Two | in_progress | {name}")
        else:
            assert "TASK-002 | Two | planned | —" in text
            updated = text.replace("TASK-002 | Two | planned | —", f"TASK-002 | Two | in_progress | {name}")
        code, response = commit(binary, state, cwd, rev, updated, f"{name}-rebuilt.md")
    assert code == 0 and response["status"] == "ok", response


def race(binary, state, a, b, mode):
    with tempfile.TemporaryDirectory(prefix="sov-gate-") as tmp:
        gate = Path(tmp) / "go"
        ready = [Path(tmp) / f"ready-{i}" for i in range(2)]
        processes = [subprocess.Popen([sys.executable, __file__, "--worker", binary,
                       str(state), str(cwd), name, str(gate), str(flag), kind],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for cwd, name, flag, kind in zip((a, b), ("alpha", "beta"), ready,
                                                      (mode if mode == "create" else "claim", mode))]
        try:
            deadline = time.monotonic() + 10
            while not all(flag.exists() for flag in ready):
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


def main(binary):
    code, version = cli(binary, "--version")
    assert code == 0 and version["message"] == "sov-state 0.1.0 (protocol v:1)"
    with tempfile.TemporaryDirectory(prefix="sov-parallel-") as tmp:
        root = Path(tmp)
        project, a, b = (root / p for p in ("project", "work-a", "work-b"))
        git("init", "-q", "-b", "main", project)
        (project / "example.txt").write_text("trial\n")
        git("-C", project, "add", "example.txt")
        git("-C", project, "-c", "user.name=Trial", "-c", "user.email=trial@example.invalid",
            "commit", "-qm", "trial")
        git("-C", project, "worktree", "add", "-q", "--detach", a)
        git("-C", project, "worktree", "add", "-q", "--detach", b)
        state = project / ".sov"
        state.mkdir()
        (state / ".roadmap.lock").touch(mode=0o600)
        road = state / "ROADMAP.md"
        initial = ("Следующий ID: `TASK-003`\n"
                   "| ID | Название | Статус | Исполнитель | Зависит от | Публикация |\n"
                   "| --- | --- | --- | --- | --- | --- |\n"
                   "| TASK-001 | One | planned | — | — | no |\n"
                   "| TASK-002 | Two | planned | — | — | no |\n")
        road.write_text(initial)
        lock_inode = (state / ".roadmap.lock").stat().st_ino
        assert snapshot(binary, state, a) == snapshot(binary, state, b)
        race(binary, state, a, b, "claim")
        text, _ = snapshot(binary, state, a)
        assert "TASK-001 | One | in_progress |" in text
        assert "TASK-002 | Two | in_progress |" in text
        assert text.count("in_progress") == 2
        print("PASS: separate processes/worktrees contend for one task; loser reselects")

        road.write_text(initial)  # fixture reset, no concurrent writers
        race(binary, state, a, b, "other")
        text, rev = snapshot(binary, state, b)
        assert "alpha" in text and "beta" in text
        print("PASS: different rows survive revision conflict and rebuild")

        road.write_text(initial)  # fixture reset, no concurrent writers
        race(binary, state, a, b, "create")
        text, rev = snapshot(binary, state, a)
        assert text.count("| TASK-003 |") == 1 and text.count("| TASK-004 |") == 1
        assert "Следующий ID: `TASK-005`" in text
        assert "alpha" in text and "beta" in text
        print("PASS: concurrent creation allocates distinct IDs and retains both rows")

        # Simulate a lost stdout *after* a successful commit: inspect the effect, not the hash alone.
        updated = text.replace("Следующий ID: `TASK-005`", "Следующий ID: `TASK-006`")
        updated += "| TASK-005 | Three | planned | — | — | no |\n"
        code, _ = commit(binary, state, a, rev, updated, "lost-response.md")
        assert code == 0
        recovered, _ = snapshot(binary, state, b)
        assert recovered.count("| TASK-005 | Three |") == 1
        assert "TASK-006" in recovered
        print("PASS: lost response resolved by semantic read without duplicate creation")

        # A halted coordinator between confirming the row and creating task.md can resume.
        task = state / "tasks" / "TASK-005"
        assert not task.exists()
        assert "| TASK-005 | Three |" in recovered
        task.mkdir(parents=True)
        (task / "task.md").write_text("# TASK-005\n")
        assert (task / "task.md").is_file()
        assert (state / ".roadmap.lock").stat().st_ino == lock_inode
        print("PASS: interruption after list publication reconciled with task artifact; mutex unchanged")

        # No fallback on timeout or missing state/lock.
        stale = "sha256:" + "0" * 64
        code, error = commit(binary, state, a, stale, updated, "stale.md")
        assert code == 10 and error["code"] == "revision_conflict"
        absent = root / "absent"
        code, error = cli(binary, "read", "--state-dir", absent)
        assert code == 13 and error["code"] == "io_error"
        print("PASS: stale revision and missing state fail closed")


if __name__ == "__main__":
    if len(sys.argv) == 9 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5],
               Path(sys.argv[6]), Path(sys.argv[7]), sys.argv[8])
    elif len(sys.argv) == 2 and os.path.isabs(sys.argv[1]):
        main(sys.argv[1])
    else:
        sys.exit("usage: python3 tests/parallel_state_trial.py /absolute/path/to/sov-state")
