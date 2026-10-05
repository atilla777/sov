import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("view", HERE / "view.py")
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)
ROOT = HERE.parents[1]
ROAD = (ROOT / "templates/ROADMAP.md").read_text()
ARCH = (ROOT / "templates/ARCHIVE.md").read_text()


def row(id, status="planned", owner="—", claim="—", deps="—", pub="yes"):
    return f"| {id} | Test {id} | {status} | {owner} | {claim} | {deps} | {pub} |"


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cli = str(Path(cls.temp.name) / "sov-state")
        subprocess.run(["go", "build", "-o", cls.cli, "."], cwd=ROOT / "cli/sov-state", check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / ".sov"
        self.state.mkdir()
        self.write(ROAD, ARCH)
        (self.state / ".roadmap.lock").touch()

    def write(self, road, archive):
        (self.state / "ROADMAP.md").write_text(road)
        (self.state / "ARCHIVE.md").write_text(archive)

    def invoke(self, command, *extra):
        return view.execute([command, "--state-dir", str(self.state), "--cli", self.cli, *extra])

    def test_valid_views_and_external_boundary(self):
        self.write(ROAD.replace("TASK-001`", "TASK-004`").replace("| --- | --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |\n" + row("TASK-001", "done", "aleksei") + "\n" + row("TASK-002", deps="TASK-001") + "\n" + row("TASK-003", "in_progress", "aleksei", "10e59101-d8f0-4435-af73-cbe6e38599bc")), ARCH)
        self.assertEqual(self.invoke("validate")["queue_count"], 3)
        self.assertEqual([x["id"] for x in self.invoke("queue")["tasks"]], ["TASK-001", "TASK-002", "TASK-003"])
        self.assertTrue(self.invoke("get", "--id", "TASK-002")["task"]["structurally_available"])
        self.assertFalse(self.invoke("get", "--id", "TASK-002")["task"]["external_start_conditions_checked"])
        self.assertEqual([x["id"] for x in self.invoke("owner", "--owner", "aleksei")["tasks"]], ["TASK-003"])

    def test_invalid_list_and_archive(self):
        self.write(ROAD.replace("TASK-001`", "TASK-002`").replace("| --- | --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |\n" + row("TASK-001", deps="TASK-999")), ARCH.replace("| --- | --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |\n" + row("TASK-001", "planned")))
        with self.assertRaises(view.ViewError) as cm:
            self.invoke("validate")
        self.assertEqual(cm.exception.code, "invalid_state")
        self.assertEqual({d["file"] for d in cm.exception.diagnostics}, {"ROADMAP.md", "ARCHIVE.md"})

    def test_interrupted_archive_and_different_duplicate(self):
        terminal = row("TASK-001", "done", "aleksei")
        road = ROAD.replace("TASK-001`", "TASK-002`").replace("| --- | --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |\n" + terminal)
        archive = ARCH.replace("| --- | --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |\n" + terminal)
        self.write(road, archive)
        with self.assertRaises(view.ViewError) as cm: self.invoke("queue")
        self.assertEqual(cm.exception.code, "recovery_required")
        self.write(road, archive.replace("Test TASK-001", "Different"))
        with self.assertRaises(view.ViewError) as cm: self.invoke("queue")
        self.assertEqual(cm.exception.code, "invalid_state")
        self.write(road, archive.replace("| done |", "|  done |"))
        with self.assertRaises(view.ViewError) as cm: self.invoke("queue")
        self.assertEqual(cm.exception.code, "invalid_state")

    def test_claim_conflict_and_lost_response_are_transport_responsibilities(self):
        initial = self.invoke("validate")["roadmap_revision"]
        new = ROAD.replace("TASK-001`", "TASK-002`").replace("| --- | --- | --- | --- | --- | --- | --- |", "| --- | --- | --- | --- | --- | --- | --- |\n" + row("TASK-001", "in_progress", "aleksei", "10e59101-d8f0-4435-af73-cbe6e38599bc"))
        candidate = Path(self.tmp.name) / "candidate"
        candidate.write_text(new)
        results = []
        def commit():
            result = subprocess.run([self.cli, "commit", "--state-dir", str(self.state), "--expected", initial, "--input", str(candidate)], capture_output=True)
            results.append(result.returncode)
        workers = [threading.Thread(target=commit) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(sorted(results), [0, 10])
        # A dropped success response is resolved by a fresh semantic read, not a second commit.
        self.assertEqual(self.invoke("get", "--id", "TASK-001")["task"]["coordinator"], "10e59101-d8f0-4435-af73-cbe6e38599bc")
        self.assertEqual(self.invoke("owner", "--owner", "aleksei")["tasks"][0]["id"], "TASK-001")

    def test_unstable_pair_and_missing_id(self):
        original = view.transport
        calls = [0]
        def moving(cli, command, state):
            raw, rev = original(cli, command, state)
            calls[0] += 1
            return raw, rev + str(calls[0])
        view.transport = moving
        try:
            with self.assertRaises(view.ViewError) as cm: self.invoke("validate")
            self.assertEqual(cm.exception.code, "unstable_snapshot")
        finally:
            view.transport = original
        with self.assertRaises(view.ViewError) as cm: self.invoke("get", "--id", "TASK-001")
        self.assertEqual(cm.exception.code, "not_found")

    def test_cancelled_dependency_and_cycle(self):
        table = "| --- | --- | --- | --- | --- | --- | --- |"
        content = ROAD.replace("TASK-001`", "TASK-003`").replace(table, table + "\n" + row("TASK-001", "cancelled", "aleksei") + "\n" + row("TASK-002", deps="TASK-001"))
        self.write(content, ARCH)
        task = self.invoke("get", "--id", "TASK-002")["task"]
        self.assertEqual(task["reasons"], ["dependency:TASK-001:cancelled"])
        self.write(content.replace(row("TASK-001", "cancelled", "aleksei"), row("TASK-001", "done", "aleksei", deps="TASK-002")), ARCH)
        with self.assertRaises(view.ViewError) as cm: self.invoke("validate")
        self.assertIn("cycle", [item["code"] for item in cm.exception.diagnostics])

    def test_cli_error_contract_and_missing_archive(self):
        result = subprocess.run(["python3", str(HERE / "view.py"), "get", "--state-dir", str(self.state), "--cli", self.cli], capture_output=True, check=False)
        self.assertEqual(result.returncode, 12)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_input")
        (self.state / "ARCHIVE.md").unlink()
        with self.assertRaises(view.ViewError) as cm: self.invoke("validate")
        self.assertEqual(cm.exception.code, "transport_error")


if __name__ == "__main__":
    unittest.main()
