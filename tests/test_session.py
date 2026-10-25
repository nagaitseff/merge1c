from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from merge1c.config import Config, Tool
from merge1c.engine import MergeError, snapshot
from merge1c.gitrepo import GitError, GitRepository
from merge1c.session import Session, SessionError, prepare_directories, prepare_repository
from tests.test_engine import FORM_PATH, MODULE_PATH, form, metadata, method, put


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-session-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sides = [self.root / name for name in ("base", "local", "incoming")]
        for root in self.sides:
            root.mkdir()
        self.location = self.root / "session"

    def prepare(self, config: Config | None = None) -> Session:
        return prepare_directories(*self.sides, self.location, config=config or Config(require_platform=False))

    def conflict(self, name: str = "conflict.bin") -> None:
        for root, content in zip(self.sides, (b"base\x00", b"local\x00", b"incoming\x00"), strict=True):
            put(root, name, content)

    def test_resume_resolve_and_check_are_durable_and_preserve_inputs(self) -> None:
        self.conflict()
        expected = [snapshot(side) for side in self.sides]
        session = self.prepare()
        self.assertFalse(session.report()["ready"])
        reopened = Session(session.path)
        report = reopened.resolve("conflict.bin", take="local")
        self.assertTrue(report["ready"], report)
        self.assertEqual((reopened.candidate / "conflict.bin").read_bytes(), b"local\x00")
        reopened_again = Session(session.path)
        self.assertTrue(reopened_again.check()["ready"])
        self.assertEqual(len(reopened_again.report()["resolutions"]), 1)
        self.assertEqual([snapshot(side) for side in self.sides], expected)
        events = [json.loads(line) for line in (session.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event["phase"] == "resolved" for event in events))
        self.assertEqual(json.loads((session.path / "report.json").read_text(encoding="utf-8"))["ready"], True)

    def test_resolving_delete_edit_can_accept_the_deleted_side(self) -> None:
        self.conflict()
        (self.sides[1] / "conflict.bin").unlink()
        session = self.prepare()
        report = session.resolve("conflict.bin", take="local")
        self.assertTrue(report["ready"], report)
        self.assertFalse((session.candidate / "conflict.bin").exists())

    def test_saved_input_tampering_is_rejected_on_resume(self) -> None:
        self.conflict()
        session = self.prepare()
        (session.path / "inputs/local/conflict.bin").write_bytes(b"tampered")
        reopened = Session(session.path)
        with self.assertRaises(SessionError):
            reopened.check()
        with self.assertRaises(SessionError):
            reopened.resolve("conflict.bin", take="local")

    def test_report_cannot_claim_ready_when_saved_inputs_are_tampered(self) -> None:
        for side in self.sides:
            put(side, "value.bin", b"base")
        session = self.prepare()
        self.assertTrue(session.report()["ready"])
        put(session.path / "inputs/local", "value.bin", b"tampered")
        reopened = Session(session.path)
        try:
            report = reopened.report()
        except SessionError:
            return
        self.assertFalse(report["ready"])

    def test_configuration_snapshot_tampering_is_rejected(self) -> None:
        session = self.prepare()
        manifest = session.path / "session.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["config"]["require_platform"] = True
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SessionError):
            Session(session.path)

    def test_default_platform_requirement_never_claims_platform_passed(self) -> None:
        session = self.prepare(Config())
        report = session.check()
        self.assertFalse(report["ready"])
        self.assertEqual(report["platform"], "not_run")
        self.assertEqual(report["structural_checks"], "passed")

    def test_repaired_cross_file_validation_issue_is_recomputed(self) -> None:
        for side in self.sides:
            put(side, FORM_PATH, form())
            put(side, MODULE_PATH, method())
        put(self.sides[1], MODULE_PATH, b"")
        put(self.sides[2], FORM_PATH, form("Обработчик"))
        session = self.prepare()
        self.assertFalse(session.report()["ready"])
        put(session.candidate, MODULE_PATH, method())
        report = session.check()
        self.assertTrue(report["ready"], report)
        self.assertNotIn("form.missing_handler", {issue["code"] for issue in report["issues"]})

    def test_status_does_not_advertise_ready_after_unchecked_candidate_edit(self) -> None:
        for side in self.sides:
            put(side, "form.xml", form())
        session = self.prepare()
        self.assertTrue(session.check()["ready"])
        put(session.candidate, "form.xml", b"<Form>")
        reopened = Session(session.path)
        self.assertFalse(reopened.report()["ready"], "An edited candidate has not passed the stored checks")
        self.assertFalse(reopened.check()["ready"])

    def test_renamed_owned_module_resolution_uses_original_side_paths(self) -> None:
        old = "src/cf/CommonForms/Старая.xml"
        new = "src/cf/CommonForms/Новая.xml"
        old_module = old[:-4] + "/Ext/Form/Module.bsl"
        new_module = new[:-4] + "/Ext/Form/Module.bsl"
        for side, value in zip(self.sides, ("1", "2", "3"), strict=True):
            put(side, old, metadata())
            put(side, old_module, method(value=value))
        (self.sides[2] / old).unlink()
        (self.sides[2] / old_module).unlink()
        put(self.sides[2], new, metadata("Новая"))
        put(self.sides[2], new_module, method(value="3"))
        session = self.prepare()
        self.assertFalse(session.report()["ready"])
        report = session.resolve(new_module, take="local")
        self.assertTrue(report["ready"], report)
        self.assertEqual((session.candidate / new_module).read_bytes(), method(value="2"))
        self.assertFalse((session.candidate / old_module).exists())

    def test_stale_session_object_cannot_lose_another_resolution(self) -> None:
        self.conflict("first.bin")
        self.conflict("second.bin")
        first = self.prepare()
        stale = Session(first.path)
        first.resolve("first.bin", take="local")
        try:
            stale.resolve("second.bin", take="local")
        except SessionError:
            # Refusing a stale writer is also safe; silently overwriting it is not.
            pass
        reopened = Session(first.path)
        self.assertEqual((reopened.candidate / "first.bin").read_bytes(), b"local\x00")
        self.assertIn("first.bin", {item["path"] for item in reopened.report()["resolutions"]})
        self.assertNotIn("first.bin", {item["path"] for item in reopened.report()["issues"]})

    def test_required_validator_is_invalidated_by_candidate_changes(self) -> None:
        tool = Tool(executable=sys.executable, args=("-c", "pass"))
        session = self.prepare(Config(require_platform=False, required_validators=("syntax",), tools={"syntax": tool}))
        self.assertFalse(session.report()["ready"])
        self.assertTrue(session.check(run_tools=True)["ready"])
        put(session.candidate, "new.bin", b"new")
        report = session.check()
        self.assertFalse(report["ready"])
        self.assertEqual(report["required_tools"], "pending")

    def test_validator_cannot_modify_candidate_and_claim_success(self) -> None:
        code = "from pathlib import Path; import sys; Path(sys.argv[1], 'new.bin').write_bytes(b'changed')"
        tool = Tool(executable=sys.executable, args=("-c", code, "{candidate}"))
        session = self.prepare(Config(require_platform=False, required_validators=("syntax",), tools={"syntax": tool}))
        report = session.check(run_tools=True)
        self.assertFalse(report["ready"])
        self.assertIn("tool.modified_candidate", {item["code"] for item in report["issues"]})

    def test_wrong_platform_version_cannot_satisfy_requirement(self) -> None:
        code = "import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(dict(status='passed', platform_version='wrong')), encoding='utf-8')"
        tool = Tool(adapter="platform", executable=sys.executable, args=("-c", code, "{report}"), report_required=True)
        session = self.prepare(Config(require_platform=True, tools={"platform": tool}))
        report = session.check(run_tools=True)
        self.assertFalse(report["ready"])
        self.assertEqual(report["platform"], "not_run")
        self.assertEqual(report["tools"]["platform"]["status"], "failed")

    def test_resolution_rejects_traversal_and_nonexistent_conflicts(self) -> None:
        self.conflict()
        session = self.prepare()
        for path in ("../outside", "/absolute", "C:/outside", "not-a-conflict.bin"):
            with self.subTest(path=path), self.assertRaises(SessionError):
                session.resolve(path, take="local")

    def test_resolution_cannot_write_through_a_candidate_directory_symlink(self) -> None:
        self.conflict("nested/conflict.bin")
        session = self.prepare()
        nested = session.candidate / "nested"
        (nested / "conflict.bin").unlink()
        nested.rmdir()
        external = self.root / "external"
        external.mkdir()
        protected = external / "conflict.bin"
        protected.write_bytes(b"outside must remain unchanged")
        try:
            nested.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Directory symlinks are unavailable: %s" % exc)
        with self.assertRaises((SessionError, MergeError)):
            session.resolve("nested/conflict.bin", take="local")
        self.assertEqual(protected.read_bytes(), b"outside must remain unchanged")


class RepositorySessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-repository-session-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Session test")
        self.git("config", "user.email", "session@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        put(self.repo, "base.bin", b"base")
        self.commit("base")
        self.git("checkout", "-q", "-b", "vendor")
        put(self.repo, "vendor.bin", b"vendor")
        self.incoming = self.commit("vendor")
        self.git("checkout", "-q", "main")
        put(self.repo, "local.bin", b"local")
        self.local = self.commit("local")
        self.location = self.root / "session"

    def git(self, *args: str) -> bytes:
        result = subprocess.run(["git", "-C", str(self.repo), "-c", "core.hooksPath=", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            self.fail("git %s: %s" % (args, result.stderr.decode(errors="replace")))
        return result.stdout

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    def test_prepare_resume_and_new_candidate_branch_leave_sources_unchanged(self) -> None:
        source_before = snapshot(self.repo)
        index_before = (self.repo / ".git/index").read_bytes()
        session = prepare_repository(self.repo, "vendor", self.location, config=Config(require_platform=False))
        reopened = Session(session.path)
        self.assertTrue(reopened.check()["ready"])
        oid = reopened.create_branch("candidate/review")
        self.assertEqual(self.git("rev-parse", "candidate/review").decode().strip(), oid)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", oid).decode().split(), [oid, self.local, self.incoming])
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.local)
        self.assertEqual(snapshot(self.repo), source_before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)

    def test_candidate_branch_is_blocked_without_required_platform_check(self) -> None:
        session = prepare_repository(self.repo, "vendor", self.location, config=Config())
        with self.assertRaises(SessionError):
            session.create_branch("candidate/unvalidated")
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.local)

    def test_edit_after_session_check_cannot_publish_unchecked_bytes(self) -> None:
        session = prepare_repository(self.repo, "vendor", self.location, config=Config(require_platform=False))
        original = GitRepository.create_candidate

        def edit_then_capture(repo, candidate, *args, **kwargs):
            put(Path(candidate), "local.bin", b"unverified edit")
            return original(repo, candidate, *args, **kwargs)

        with patch.object(GitRepository, "create_candidate", edit_then_capture):
            with self.assertRaises(GitError):
                session.create_branch("candidate/unverified")
        branches = self.git("for-each-ref", "--format=%(refname)", "refs/heads/candidate/unverified")
        self.assertEqual(branches, b"")
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.local)

    def test_committed_inputs_warning_survives_rechecking(self) -> None:
        put(self.repo, "local.bin", b"uncommitted work")
        session = prepare_repository(self.repo, "vendor", self.location, config=Config(require_platform=False))
        report = Session(session.path).check()
        self.assertIn("git.uncommitted_state", {issue["code"] for issue in report["issues"]})
        self.assertEqual((session.candidate / "local.bin").read_bytes(), b"local")
        self.assertEqual((self.repo / "local.bin").read_bytes(), b"uncommitted work")

    def test_configured_storage_works_when_system_temp_is_unavailable(self) -> None:
        storage = self.root / "allowed sessions"
        tool = Tool(executable=sys.executable, args=("-c", "pass"))
        config = Config(session_root=str(storage), require_platform=False,
                        required_validators=("check",), tools={"check": tool})
        with patch("tempfile.gettempdir", side_effect=PermissionError("system temp is unavailable")):
            session = prepare_repository(self.repo, "vendor", config=config)
            self.assertEqual(session.path.parent, storage.resolve())
            self.assertTrue(session.check(run_tools=True)["ready"])
            commit = session.create_branch("candidate/custom-storage")
        self.assertEqual(self.git("rev-parse", "candidate/custom-storage").decode().strip(), commit)

    def test_prepare_uses_private_snapshots_without_publishing_directory_renames(self) -> None:
        events = []
        with patch.object(GitRepository, "export_tree", side_effect=AssertionError("No public export needed")):
            session = prepare_repository(self.repo, "vendor", self.location,
                                         config=Config(require_platform=False), progress=events.append)
        self.assertTrue(session.report()["ready"])
        self.assertEqual(session.data["git"]["local_branch"], "main")
        exported = [event for event in events if event["phase"] == "export"]
        self.assertEqual({event["side"] for event in exported}, {"base", "local", "incoming"})
        self.assertTrue(all(0 <= event["current"] <= event["total"] for event in exported))

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_prepare_completes_while_exported_child_file_blocks_directory_rename(self) -> None:
        from tests.test_gitrepo import _file_without_delete_share

        original = GitRepository.exported_snapshot
        reproduced = []

        @contextmanager
        def locked(repo, revision, parent, *, progress=None):
            with original(repo, revision, parent, progress=progress) as root:
                child = root / "vendor.bin"
                if revision == self.incoming:
                    with _file_without_delete_share(child):
                        with self.assertRaises(PermissionError) as caught:
                            root.rename(root.with_name("would-fail-in-0.1.2"))
                        self.assertEqual(caught.exception.winerror, 5)
                        reproduced.append(root)
                        yield root
                else:
                    yield root

        with patch.object(GitRepository, "exported_snapshot", locked):
            session = prepare_repository(self.repo, "vendor", self.location, config=Config(require_platform=False))
        self.assertEqual(len(reproduced), 1)
        self.assertTrue(session.report()["ready"])
        self.assertEqual((session.candidate / "vendor.bin").read_bytes(), b"vendor")
        self.assertIn("pin", session.data["git"])

    def test_cleanup_warning_preserves_a_pinned_git_session_and_survives_check(self) -> None:
        original = GitRepository.exported_snapshot

        @contextmanager
        def warning(repo, revision, parent, *, progress=None):
            with original(repo, revision, parent, progress=progress) as root:
                yield root
                saved = Session(self.location)
                self.assertIn("pin", saved.data["git"])
                progress({"phase": "cleanup_warning", "directory": str(root), "message": "locked by test"})

        with patch.object(GitRepository, "exported_snapshot", warning):
            session = prepare_repository(self.repo, "vendor", self.location, config=Config(require_platform=False))
        report = Session(session.path).check()
        self.assertTrue(report["ready"], report)
        self.assertIn("storage.cleanup", {issue["code"] for issue in report["issues"]})
        commit = session.create_branch("review/cleanup-warning")
        self.assertEqual(self.git("rev-parse", "review/cleanup-warning").decode().strip(), commit)

    def test_changed_source_head_prevents_candidate_publication(self) -> None:
        session = prepare_repository(self.repo, "vendor", self.location, config=Config(require_platform=False))
        put(self.repo, "later.bin", b"later")
        later = self.commit("later")
        with self.assertRaises(SessionError):
            session.create_branch("candidate/stale")
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), later)

    def test_repository_subdirectory_cannot_hide_session_inside_repository(self) -> None:
        subdirectory = self.repo / "source"
        subdirectory.mkdir()
        with self.assertRaises(SessionError):
            prepare_repository(subdirectory, "vendor", self.repo / "hidden-session", config=Config(require_platform=False))
        self.assertFalse((self.repo / "hidden-session").exists())


if __name__ == "__main__":
    unittest.main()
