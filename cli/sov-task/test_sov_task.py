import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import sov_task


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".sov"
        self.root.mkdir()
        for name in ("tasks", "claims", "completed", "archive"):
            (self.root / name).mkdir()

    def run_cli(self, *args, ok=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sov_task.main(["--state-dir", str(self.root), *args])
        self.assertEqual(code == 0, ok, stderr.getvalue())
        return json.loads((stdout if code == 0 else stderr if not stdout.getvalue() else stdout).getvalue())

    def create(self, slug, depends=(), id_=None):
        card = {"title": slug, "description": "Work", "depends_on": list(depends), "acceptance_criteria": ["Done"]}
        if id_ is not None:
            card["id"] = id_
        path = Path(self.temp.name) / (slug + ".yaml")
        path.write_text(sov_task.yaml.safe_dump(card), encoding="utf-8")
        return self.run_cli("create", "--file", str(path), "--slug", slug)

    def test_order_dependencies_and_completion(self):
        first = self.create("first")
        second = self.create("second", [first["id"]])
        third = self.create("third")
        self.assertEqual([row["id"] for row in self.run_cli("list-ready")], ["0001", "0003"])
        self.assertEqual(self.run_cli("claim-next", "--owner", "agent")["id"], "0001")
        self.assertEqual(self.run_cli("claim-next")["id"], "0003")
        self.assertEqual(self.run_cli("claim-next"), {"status": "no_ready_tasks"})
        self.assertEqual(self.run_cli("release", third["id"])["state"], "released")
        (self.root / "tasks" / first["name"] / "checks.md").write_text("checked")
        self.run_cli("complete", first["id"], "--owner", "agent")
        self.assertEqual((self.root / "archive" / first["name"] / "checks.md").read_text(), "checked")
        self.assertTrue((self.root / "completed" / first["id"]).exists())
        self.assertEqual([row["id"] for row in self.run_cli("list-ready")], ["0002", "0003"])
        self.assertTrue(self.run_cli("validate")["valid"])
        self.assertEqual(self.create("fourth")["id"], "0004")
        self.assertEqual(second["id"], "0002")

    def test_claim_race_and_loser_claims_next(self):
        self.create("one")
        self.create("two")

        def claim(_):
            proc = subprocess.run([sys.executable, str(Path(sov_task.__file__)), "--state-dir", str(self.root), "claim-next"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual({r["id"] for r in results}, {"0001", "0002"})
        self.assertTrue(self.run_cli("validate")["valid"])

    def test_competing_claim_same_id_and_manual_resume(self):
        item = self.create("one")
        other = self.create("two", [item["id"]])

        def claim(owner):
            proc = subprocess.run([sys.executable, str(Path(sov_task.__file__)), "--state-dir", str(self.root), "claim", item["id"], "--owner", owner], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return owner, json.loads(proc.stdout)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(pool.map(claim, ("alice", "bob")))
        winners = [name for name, result in outcomes.items() if result.get("state") == "IN PROGRESS"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(outcomes["bob" if winners[0] == "alice" else "alice"]["status"], "not_ready")
        self.assertEqual(self.run_cli("claim-next"), {"status": "no_ready_tasks"})
        (self.root / "tasks" / item["name"] / "task.md").write_text("resume at checks", encoding="utf-8")
        self.run_cli("release", item["id"], "--owner", winners[0])
        self.assertEqual(self.run_cli("show", item["id"])["state"], "READY")
        self.assertEqual(self.run_cli("claim", item["id"], "--owner", "next-agent")["state"], "IN PROGRESS")
        self.assertEqual(self.run_cli("claim-next"), {"status": "no_ready_tasks"})
        self.run_cli("complete", item["id"], "--owner", "next-agent")
        self.assertEqual((self.root / "archive" / item["name"] / "task.md").read_text(), "resume at checks")
        self.assertEqual(self.run_cli("show", other["id"])["state"], "READY")
        self.assertTrue(self.run_cli("validate")["valid"])

    def test_interrupted_transfer_and_recovery(self):
        item = self.create("one")
        self.run_cli("claim", item["id"], "--owner", "alice")
        marker = self.root / "completed" / item["id"]
        marker.touch()
        self.assertEqual(self.run_cli("show", item["id"])["state"], "INCOMPLETE_COMPLETION")
        self.assertIn("incomplete completion", " ".join(self.run_cli("validate", ok=False)["errors"]))
        self.run_cli("release", item["id"], "--owner", "alice", ok=False)
        self.run_cli("complete", item["id"], "--owner", "alice")
        self.assertTrue(self.run_cli("validate")["valid"])
        item2 = self.create("two")
        self.run_cli("claim", item2["id"])
        (self.root / "completed" / item2["id"]).touch()
        (self.root / "tasks" / item2["name"]).rename(self.root / "archive" / item2["name"])
        self.assertFalse(self.run_cli("validate", ok=False)["valid"])
        self.run_cli("complete", item2["id"])
        self.assertTrue(self.run_cli("validate")["valid"])

    def test_completed_read_empty_order_and_full_cards(self):
        self.assertEqual(self.run_cli("list-completed"), [])
        self.assertIn("not found", self.run_cli("show-completed", "0001", ok=False)["error"])
        first = self.create("first")
        second = self.create("second", [first["id"]])
        self.run_cli("claim", first["id"])
        self.run_cli("complete", first["id"])
        self.run_cli("claim", second["id"])
        self.run_cli("complete", second["id"])
        rows = self.run_cli("list-completed")
        self.assertEqual([row["id"] for row in rows], ["0001", "0002"])
        self.assertEqual([row["state"] for row in rows], ["COMPLETED", "COMPLETED"])
        self.assertEqual(rows[1]["task"]["depends_on"], ["0001"])
        self.assertEqual(rows[1]["task"]["description"], "Work")
        self.assertEqual(rows[1]["task"]["acceptance_criteria"], ["Done"])
        self.assertEqual(self.run_cli("show-completed", "0002"), rows[1])
        self.assertEqual(self.run_cli("list"), [])
        self.assertIn("not found", self.run_cli("show", "0002", ok=False)["error"])

    def test_completed_read_rejects_incomplete_and_invalid_archive(self):
        item = self.create("first")
        self.assertIn("not found", self.run_cli("show-completed", item["id"], ok=False)["error"])
        self.run_cli("claim", item["id"])
        marker = self.root / "completed" / item["id"]
        marker.touch()
        self.assertIn("incomplete completion", self.run_cli("list-completed", ok=False)["error"])
        self.assertIn("incomplete completion", self.run_cli("show-completed", item["id"], ok=False)["error"])
        (self.root / "tasks" / item["name"]).rename(self.root / "archive" / item["name"])
        self.assertIn("claim", self.run_cli("list-completed", ok=False)["error"])
        self.assertIn("claim", self.run_cli("show-completed", item["id"], ok=False)["error"])
        self.run_cli("complete", item["id"])
        marker.unlink()
        self.assertIn("without marker", self.run_cli("list-completed", ok=False)["error"])
        self.assertIn("without marker", self.run_cli("show-completed", item["id"], ok=False)["error"])
        marker.touch()
        card = self.root / "archive" / item["name"] / "task.yaml"
        card.write_text('id: 1\ntitle: bad\n', encoding="utf-8")
        self.assertIn("missing fields", self.run_cli("list-completed", ok=False)["error"])
        self.assertIn("missing fields", self.run_cli("show-completed", item["id"], ok=False)["error"])

    def test_completed_read_rejects_orphan_marker_and_duplicate_task(self):
        (self.root / "completed" / "0004").touch()
        self.assertIn("marker without archive", self.run_cli("list-completed", ok=False)["error"])
        self.assertIn("marker without archive", self.run_cli("show-completed", "0004", ok=False)["error"])
        (self.root / "completed" / "0004").unlink()
        item = self.create("first")
        self.run_cli("claim", item["id"])
        self.run_cli("complete", item["id"])
        (self.root / "tasks" / item["name"]).mkdir()
        self.assertIn("both present", self.run_cli("list-completed", ok=False)["error"])
        self.assertIn("both present", self.run_cli("show-completed", item["id"], ok=False)["error"])

    def test_bad_yaml_duplicate_id_and_intermediate_state(self):
        item = self.create("one", id_="9999")
        self.run_cli("create", "--file", self.write("duplicate.yaml", 'id: "9999"\ntitle: bad\ndescription: x\ndepends_on: []\nacceptance_criteria: [ok]\n'), "--slug", "bad", ok=False)
        self.run_cli("create", "--file", self.write("numeric.yaml", 'id: 0002\ntitle: bad\ndescription: x\ndepends_on: []\nacceptance_criteria: [ok]\n'), "--slug", "bad", ok=False)
        self.run_cli("create", "--file", self.write("dupe.yaml", 'title: a\ntitle: b\n'), "--slug", "bad", ok=False)
        self.run_cli("create", "--file", self.write("broken.yaml", 'title: [\n'), "--slug", "bad", ok=False)
        self.run_cli("create", "--file", self.write("overflow.yaml", 'title: x\ndescription: y\ndepends_on: []\nacceptance_criteria: [ok]\n'), "--slug", "overflow", ok=False)
        (self.root / "tasks" / item["name"] / "task.yaml").write_text('id: 9999\ntitle: x\ndescription: y\ndepends_on: []\nacceptance_criteria: [ok]\n')
        self.assertFalse(self.run_cli("validate", item["id"], ok=False)["valid"])
        (self.root / "tasks" / "0012-incomplete").mkdir()
        self.assertFalse(self.run_cli("validate", ok=False)["valid"])

    def test_parallel_create_allocates_distinct_ids(self):
        card = self.write("card.yaml", 'title: Work\ndescription: Work\ndepends_on: []\nacceptance_criteria: [Done]\n')

        def create(n):
            proc = subprocess.run([sys.executable, str(Path(sov_task.__file__)), "--state-dir", str(self.root), "create", "--file", card, "--slug", f"work-{n}"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)["id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(create, range(12)))
        self.assertEqual(set(ids), {f"{n:04d}" for n in range(1, 13)})
        self.assertTrue(self.run_cli("validate")["valid"])

    def test_invalid_dependency_and_missing_claim_cannot_complete(self):
        self.run_cli("create", "--file", self.write("self.yaml", 'id: "0001"\ntitle: x\ndescription: y\ndepends_on: ["0001"]\nacceptance_criteria: [ok]\n'), "--slug", "self", ok=False)
        item = self.create("one", ["0009"])
        self.assertEqual(self.run_cli("show", item["id"])["state"], "BLOCKED")
        self.assertEqual(self.run_cli("claim", item["id"])["status"], "not_ready")
        self.run_cli("complete", item["id"], ok=False)
        self.assertFalse((self.root / "completed" / item["id"]).exists())

    def test_claim_owner_mismatch_and_archive_collision(self):
        item = self.create("one")
        self.run_cli("claim", item["id"], "--owner", "alice")
        self.run_cli("release", item["id"], "--owner", "bob", ok=False)
        self.run_cli("complete", item["id"], "--owner", "bob", ok=False)
        (self.root / "archive" / item["name"]).mkdir()
        self.run_cli("complete", item["id"], "--owner", "alice", ok=False)
        self.assertFalse((self.root / "completed" / item["id"]).exists())

    def write(self, name, text):
        path = Path(self.temp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)


if __name__ == "__main__":
    unittest.main()
