from __future__ import annotations

from contextlib import contextmanager
import os
from hashlib import sha256
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from merge1c.gitrepo import GitError, GitRepository, _relative_path, _WINDOWS_RETRY_DELAYS


def _locked_error():
    error = PermissionError("temporary Windows lock")
    error.winerror = 5
    return error


@contextmanager
def _file_without_delete_share(path: Path):
    """An ordinary Windows reader can prevent renaming the containing tree."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


class GitRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c git ЮТ-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "repository with spaces"
        self.path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Merge test")
        self.git("config", "user.email", "merge@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.write("common.txt", b"base\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.repo = GitRepository(self.path)

    def git(self, *arguments: str, input: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.path), "-c", "core.hooksPath=", *arguments],
            input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            self.fail("git %s failed: %s" % (arguments, result.stderr.decode(errors="replace")))
        return result.stdout

    def write(self, name: str, content: bytes) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    def branches(self, conflict: bool = False) -> tuple[str, str]:
        self.git("checkout", "-q", "-b", "vendor")
        self.write("common.txt" if conflict else "incoming.txt", b"incoming\n")
        incoming = self.commit("vendor")
        self.git("checkout", "-q", "main")
        self.write("common.txt" if conflict else "local.txt", b"local\n")
        local = self.commit("local")
        return local, incoming

    def candidate_directory(self) -> Path:
        path = Path(self.temporary.name) / "candidate ЮТ files"
        self.repo.export_tree("HEAD", path)
        return path

    def test_version_resolve_and_merge_bases(self) -> None:
        self.assertRegex(self.repo.version, r"^\d+\.\d+")
        local, incoming = self.branches()
        self.assertEqual(self.repo.head(), local)
        self.assertEqual(self.repo.resolve("vendor"), incoming)
        self.assertEqual(self.repo.merge_bases(local, incoming), [self.base])
        with self.assertRaises(GitError):
            self.repo.resolve("--help")

    def test_current_branch_reports_name_or_detached_head(self) -> None:
        self.assertEqual(self.repo.current_branch(), "main")
        before = self.repo.head()
        self.git("checkout", "-q", "--detach", "HEAD")
        self.assertIsNone(self.repo.current_branch())
        self.assertEqual(self.repo.head(), before)

    def test_status_is_read_only_and_ignores_untracked_files(self) -> None:
        index = (self.path / ".git" / "index").read_bytes()
        self.write("untracked.txt", b"untracked")
        self.assertEqual(self.repo.status(), "")
        self.write("common.txt", b"changed\n")
        self.assertEqual(self.repo.status(), " M common.txt\0")
        self.assertEqual((self.path / ".git" / "index").read_bytes(), index)
        self.assertFalse(self.repo.operation_in_progress())

    def test_old_git_is_rejected(self) -> None:
        old = subprocess.CompletedProcess([], 0, b"git version 2.49.1\n", b"")
        with patch("merge1c.gitrepo.subprocess.run", return_value=old):
            with self.assertRaisesRegex(GitError, "2.50"):
                GitRepository(self.path)

    @unittest.skipUnless(os.name == "nt", "Windows batch-file dispatch")
    def test_git_batch_wrapper_is_rejected_before_execution(self) -> None:
        wrapper = Path(self.temporary.name) / "fake-git.cmd"
        wrapper.write_text('@echo off\r\nexit /b 0\r\n', encoding="ascii")
        with patch("merge1c.gitrepo.subprocess.run") as execute:
            with self.assertRaises(GitError):
                GitRepository(self.path, executable=str(wrapper))
            execute.assert_not_called()

    def test_configured_higher_minimum_is_enforced_on_every_adapter_instance(self) -> None:
        installed = subprocess.CompletedProcess([], 0, b"git version 2.53.0.windows.3\n", b"")
        with patch("merge1c.gitrepo.subprocess.run", return_value=installed):
            with self.assertRaisesRegex(GitError, "2.54.0"):
                GitRepository(self.path, minimum_version="2.54.0")

    def test_minimum_cannot_be_invalid_or_lower_than_supported_baseline(self) -> None:
        for value in ("2.49.99", "2", "2.50.beta", "", None):
            with self.subTest(value=value), self.assertRaises(GitError):
                GitRepository(self.path, minimum_version=value)

    def test_changed_paths_keeps_both_sides_of_a_rename(self) -> None:
        self.git("mv", "common.txt", "Переименованный файл.txt")
        tip = self.commit("rename")
        self.assertEqual(
            set(self.repo.changed_paths(self.base, tip)),
            {"common.txt", "Переименованный файл.txt"},
        )

    def test_preview_clean_does_not_change_source_index_or_worktree(self) -> None:
        local, incoming = self.branches()
        index = (self.path / ".git" / "index").read_bytes()
        config = (self.path / ".git" / "config").read_bytes()
        status = self.git("--no-optional-locks", "status", "--porcelain=v1", "-z")
        result = self.repo.preview(local, incoming)
        self.assertTrue(result["clean"])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["local"], local)
        self.assertEqual(result["incoming"], incoming)
        self.assertEqual(self.repo.head(), local)
        self.assertFalse((self.path / "incoming.txt").exists())
        self.assertEqual(self.git("show", result["tree"] + ":incoming.txt"), b"incoming\n")
        self.assertEqual(self.git("--no-optional-locks", "status", "--porcelain=v1", "-z"), status)
        self.assertEqual((self.path / ".git" / "index").read_bytes(), index)
        self.assertEqual((self.path / ".git" / "config").read_bytes(), config)

    def test_preview_conflicts_are_structured_and_exit_one_is_not_an_error(self) -> None:
        local, incoming = self.branches(conflict=True)
        result = self.repo.preview(local, incoming)
        self.assertFalse(result["clean"])
        self.assertEqual({entry["stage"] for entry in result["conflicts"]}, {1, 2, 3})
        self.assertEqual({entry["path"] for entry in result["conflicts"]}, {"common.txt"})
        self.assertTrue(any("CONFLICT" in entry["type"] for entry in result["messages"]))
        self.assertEqual((self.path / "common.txt").read_bytes(), b"local\n")
        self.assertFalse((self.path / ".git" / "MERGE_HEAD").exists())

    def test_preview_disables_false_similarity_renames(self) -> None:
        shared = b"\n".join(("shared XML template line %d" % number).encode() for number in range(80))
        self.write("old.xml", shared + b"\nold\n")
        base = self.commit("large template")
        self.git("checkout", "-q", "-b", "supplier")
        (self.path / "old.xml").unlink()
        self.write("new.xml", shared + b"\nnew object\n")
        incoming = self.commit("delete old and add distinct object")
        self.git("checkout", "-q", "main")
        self.write("old.xml", shared + b"\nlocal customization\n")
        local = self.commit("customization")
        self.assertEqual(self.repo.merge_bases(local, incoming), [base])
        result = self.repo.preview(local, incoming)
        self.assertFalse(result["clean"])
        self.assertTrue(any("modify/delete" in item["type"] for item in result["messages"]))

    def test_export_ignores_archive_transformations_and_preserves_raw_bytes(self) -> None:
        self.write(".gitattributes", b"omit.txt export-ignore\nsubst.txt export-subst\n*.bsl -text\n")
        self.write("omit.txt", b"must remain\n")
        self.write("subst.txt", b"$Format:%H$\n")
        raw = b"\xef\xbb\xbf" + "Процедура Тест()\r\nКонецПроцедуры\r\n".encode()
        self.write("Каталог с пробелом/Модуль.bsl", raw)
        commit = self.commit("export attributes")
        destination = Path(self.temporary.name) / "raw export"
        self.repo.export_tree(commit, destination)
        self.assertEqual((destination / "omit.txt").read_bytes(), b"must remain\n")
        self.assertEqual((destination / "subst.txt").read_bytes(), b"$Format:%H$\n")
        self.assertEqual((destination / "Каталог с пробелом/Модуль.bsl").read_bytes(), raw)

    def test_private_snapshot_is_complete_without_any_final_rename(self) -> None:
        root = Path(self.temporary.name) / "private snapshots"
        with patch.object(Path, "rename", side_effect=AssertionError("snapshot must not rename")):
            with self.repo.exported_snapshot("HEAD", root) as snapshot:
                self.assertEqual((snapshot / "common.txt").read_bytes(), b"base\n")
                self.assertEqual(snapshot.parent, root)
        self.assertFalse(snapshot.exists())

    def test_private_snapshot_failure_never_yields_partial_tree(self) -> None:
        root = Path(self.temporary.name) / "private snapshots"
        with patch.object(self.repo, "_write_tree", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(GitError, "write failed"):
                with self.repo.exported_snapshot("HEAD", root):
                    self.fail("a partial tree must never be exposed")
        self.assertEqual(list(root.iterdir()), [])

    def test_snapshot_progress_is_bounded_and_counts_exact_raw_bytes(self) -> None:
        self.write("a-large.bin", b"\x00" * (9 * 1024 * 1024))
        for number in range(205):
            self.write(f"files/{number:03}.txt", b"small\n")
        self.commit("export progress")
        events = []
        root = Path(self.temporary.name) / "private snapshots"
        with self.repo.exported_snapshot("HEAD", root, progress=events.append) as snapshot:
            files = [path for path in snapshot.rglob("*") if path.is_file()]
            self.assertEqual(events[-1]["bytes"], sum(path.stat().st_size for path in files))
            self.assertEqual(events[-1]["current"], len(files))
        self.assertEqual(events[0]["current"], 0)
        self.assertEqual(events[0]["bytes"], 0)
        self.assertTrue(events[-1]["complete"])
        self.assertTrue(all(event["phase"] == "export" for event in events))
        self.assertTrue(all(event["total"] == len(files) for event in events))
        self.assertTrue(all(left["bytes"] <= right["bytes"] for left, right in zip(events, events[1:], strict=False)))
        self.assertGreaterEqual(len(events), 4)
        self.assertLessEqual(len(events), 7)

    def test_public_export_retries_only_bounded_windows_locks(self) -> None:
        root = Path(self.temporary.name)
        destination = root / "retry export"
        with patch.object(Path, "rename", side_effect=_locked_error()) as rename:
            with patch("merge1c.gitrepo._windows_lock_error", return_value=True):
                with patch("merge1c.gitrepo.time.sleep") as sleep:
                    with self.assertRaisesRegex(GitError, "temporary Windows lock"):
                        self.repo.export_tree("HEAD", destination)
        self.assertEqual(rename.call_count, len(_WINDOWS_RETRY_DELAYS) + 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], list(_WINDOWS_RETRY_DELAYS))
        self.assertFalse(destination.exists())
        self.assertFalse(list(root.glob(".merge1c-export-*")))

    def test_public_export_does_not_retry_other_errors(self) -> None:
        destination = Path(self.temporary.name) / "failed export"
        with patch.object(Path, "rename", side_effect=OSError("different error")) as rename:
            with patch("merge1c.gitrepo.time.sleep") as sleep:
                with self.assertRaisesRegex(GitError, "different error"):
                    self.repo.export_tree("HEAD", destination)
        self.assertEqual(rename.call_count, 1)
        sleep.assert_not_called()

    def test_public_export_rechecks_destination_after_waiting(self) -> None:
        destination = Path(self.temporary.name) / "racing export"

        def concurrent_edit(delay):
            destination.mkdir()
            (destination / "new.txt").write_bytes(b"keep")

        with patch.object(Path, "rename", side_effect=_locked_error()) as rename:
            with patch("merge1c.gitrepo._windows_lock_error", return_value=True):
                with patch("merge1c.gitrepo.time.sleep", side_effect=concurrent_edit):
                    with self.assertRaisesRegex(GitError, "changed during export"):
                        self.repo.export_tree("HEAD", destination)
        self.assertEqual(rename.call_count, 1)
        self.assertEqual((destination / "new.txt").read_bytes(), b"keep")

    def test_cleanup_failure_reports_remaining_directory_without_losing_snapshot(self) -> None:
        events = []
        root = Path(self.temporary.name) / "private snapshots"
        with patch("merge1c.gitrepo.shutil.rmtree", side_effect=_locked_error()) as remove:
            with patch("merge1c.gitrepo._windows_lock_error", return_value=True):
                with patch("merge1c.gitrepo.time.sleep"):
                    with self.repo.exported_snapshot("HEAD", root, progress=events.append) as snapshot:
                        self.assertEqual((snapshot / "common.txt").read_bytes(), b"base\n")
        self.assertEqual(remove.call_count, len(_WINDOWS_RETRY_DELAYS) + 1)
        self.assertTrue(snapshot.exists())
        self.assertEqual(events[-1]["phase"], "cleanup_warning")
        self.assertEqual(events[-1]["directory"], str(snapshot))
        self.assertIn(str(snapshot), events[-1]["message"])

    def test_cleanup_failure_does_not_mask_a_body_error(self) -> None:
        failure = ValueError("body failed")
        events = []
        root = Path(self.temporary.name) / "private snapshots"
        with patch("merge1c.gitrepo.shutil.rmtree", side_effect=_locked_error()):
            with patch("merge1c.gitrepo._windows_lock_error", return_value=True):
                with patch("merge1c.gitrepo.time.sleep"):
                    with self.assertRaises(ValueError) as caught:
                        with self.repo.exported_snapshot("HEAD", root, progress=events.append) as snapshot:
                            raise failure
        self.assertIs(caught.exception, failure)
        self.assertTrue(any(str(snapshot) in note for note in failure.__notes__))
        self.assertEqual(events[-1]["phase"], "cleanup_warning")

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_private_snapshot_can_be_read_with_a_real_child_handle_lock(self) -> None:
        root = Path(self.temporary.name) / "private snapshots"
        with self.repo.exported_snapshot("HEAD", root) as snapshot:
            with _file_without_delete_share(snapshot / "common.txt"):
                self.assertEqual((snapshot / "common.txt").read_bytes(), b"base\n")
                with self.assertRaises(PermissionError) as caught:
                    snapshot.rename(root / "renamed")
                self.assertEqual(caught.exception.winerror, 5)
        self.assertFalse(snapshot.exists())

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_public_export_recovers_when_real_child_lock_is_released(self) -> None:
        destination = Path(self.temporary.name) / "locked export"
        original = Path.rename
        lock_context = None
        release = None
        attempts = []

        def rename(source, target):
            nonlocal lock_context, release
            attempts.append(source)
            if lock_context is None:
                lock_context = _file_without_delete_share(source / "common.txt")
                lock_context.__enter__()
            try:
                return original(source, target)
            except PermissionError:
                if release is None:
                    release = threading.Timer(0.1, lock_context.__exit__, args=(None, None, None))
                    release.start()
                raise

        try:
            with patch.object(Path, "rename", rename):
                self.repo.export_tree("HEAD", destination)
        finally:
            if release is not None:
                release.join()
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual((destination / "common.txt").read_bytes(), b"base\n")

    def test_export_refuses_existing_files_and_symlink_tree(self) -> None:
        destination = Path(self.temporary.name) / "existing"
        destination.mkdir()
        (destination / "important.txt").write_bytes(b"keep")
        with self.assertRaisesRegex(GitError, "empty"):
            self.repo.export_tree("HEAD", destination)
        self.assertEqual((destination / "important.txt").read_bytes(), b"keep")
        oid = self.git("hash-object", "-w", "--stdin", input=b"../outside").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000", oid, "link")
        self.git("commit", "-q", "-m", "symlink tree")
        with self.assertRaisesRegex(GitError, "symbolic link"):
            self.repo.export_tree("HEAD", Path(self.temporary.name) / "unsafe")
        with self.assertRaisesRegex(GitError, "symbolic link"):
            with self.repo.exported_snapshot("HEAD", Path(self.temporary.name) / "unsafe-private"):
                self.fail("a symlink tree must not be exposed")
        self.assertFalse((Path(self.temporary.name) / "outside").exists())

    def test_path_guard_rejects_traversal_and_git_metadata(self) -> None:
        for name in ("../escape", "/absolute", "C:/outside", "a/../../b", "a\\..\\b", ".git/config", "x/.GiT/HEAD"):
            with self.subTest(name=name), self.assertRaises(GitError):
                _relative_path(name)

    def test_candidate_is_a_merge_on_a_new_branch_with_exact_bytes_and_deletions(self) -> None:
        local, incoming = self.branches()
        candidate = self.candidate_directory()
        (candidate / "local.txt").unlink()
        (candidate / ".gitattributes").write_bytes(b"*.bsl text eol=lf\n")
        raw = b"\xef\xbb\xbf" + "Процедура Тест()\r\nКонецПроцедуры\r\n".encode()
        module = candidate / "Форма с пробелом" / "Модуль.bsl"
        module.parent.mkdir()
        module.write_bytes(raw)
        index = (self.path / ".git" / "index").read_bytes()
        commit = self.repo.create_candidate(candidate, local, incoming, "review/update", "Reviewed merge\n")
        self.assertEqual(self.repo.resolve("review/update"), commit)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", commit).decode().split(), [commit, local, incoming])
        self.assertEqual(self.git("show", commit + ":Форма с пробелом/Модуль.bsl"), raw)
        self.assertNotIn(b"local.txt", self.git("ls-tree", "-r", "--name-only", commit))
        self.assertEqual(self.repo.head(), local)
        self.assertEqual((self.path / ".git" / "index").read_bytes(), index)
        self.assertTrue((self.path / "local.txt").exists())
        self.assertFalse((self.path / "Форма с пробелом").exists())
        with self.assertRaisesRegex(GitError, "already exists"):
            self.repo.create_candidate(candidate, local, incoming, "review/update", "Do not overwrite")
        self.assertEqual(self.repo.resolve("review/update"), commit)

    def test_candidate_rejects_stale_head_without_creating_branch(self) -> None:
        local, incoming = self.branches()
        candidate = self.candidate_directory()
        self.write("later.txt", b"later\n")
        later = self.commit("new work")
        with self.assertRaisesRegex(GitError, "HEAD changed"):
            self.repo.create_candidate(candidate, local, incoming, "stale", "stale")
        self.assertEqual(self.repo.head(), later)
        with self.assertRaises(GitError):
            self.repo.resolve("stale")

    def test_candidate_rejects_active_source_merge(self) -> None:
        local, incoming = self.branches(conflict=True)
        candidate = self.candidate_directory()
        result = subprocess.run(["git", "-C", str(self.path), "-c", "rerere.enabled=false", "merge", "--no-commit", incoming], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.repo.operation_in_progress())
        with self.assertRaisesRegex(GitError, "active Git operation"):
            self.repo.create_candidate(candidate, local, incoming, "during-merge", "candidate")

    def test_candidate_preserves_executable_mode(self) -> None:
        self.git("update-index", "--chmod=+x", "common.txt")
        self.git("commit", "-q", "-m", "executable")
        local, incoming = self.branches()
        candidate = self.candidate_directory()
        commit = self.repo.create_candidate(candidate, local, incoming, "executable", "preserve mode")
        self.assertTrue(self.git("ls-tree", commit, "common.txt").startswith(b"100755 blob "))

    def test_merge_bases_returns_all_criss_cross_bases(self) -> None:
        local, incoming = self.branches()
        tree = self.repo.preview(local, incoming)["tree"]
        left = self.git("commit-tree", tree, "-p", local, "-p", incoming, input=b"left\n").decode().strip()
        right = self.git("commit-tree", tree, "-p", incoming, "-p", local, input=b"right\n").decode().strip()
        self.assertEqual(set(self.repo.merge_bases(left, right)), {local, incoming})

    def test_unrelated_history_is_a_git_error_with_diagnostics(self) -> None:
        empty_tree = self.git("mktree", input=b"").decode().strip()
        unrelated = self.git("commit-tree", empty_tree, input=b"other root\n").decode().strip()
        self.assertEqual(self.repo.merge_bases("HEAD", unrelated), [])
        with self.assertRaisesRegex(GitError, "unrelated"):
            self.repo.preview("HEAD", unrelated)

    def test_transaction_rejects_head_change_after_capture(self) -> None:
        local, incoming = self.branches()
        candidate = self.candidate_directory()
        original_run = self.repo._run
        def move_before_transaction(arguments, **kwargs):
            if arguments[:2] == ["update-ref", "--stdin"]:
                self.git("update-ref", "refs/heads/main", incoming, local)
            return original_run(arguments, **kwargs)
        with patch.object(self.repo, "_run", side_effect=move_before_transaction):
            with self.assertRaises(GitError):
                self.repo.create_candidate(candidate, local, incoming, "racing", "candidate")
        self.assertEqual(self.repo.head(), incoming)
        with self.assertRaises(GitError):
            self.repo.resolve("racing")

    def test_session_pin_keeps_real_parents_reachable_through_gc(self) -> None:
        local, incoming = self.branches()
        payload = b'{"schema_version":1,"source":"immutable"}\n'
        index = (self.path / ".git/index").read_bytes()
        pin = self.repo.pin_session(local, incoming, uuid.uuid4().hex, payload)
        self.assertTrue(pin["ref"].startswith("refs/merge1c/sessions/"))
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", pin["commit"]).decode().split(), [pin["commit"], local, incoming])
        self.assertEqual(self.git("show", pin["commit"] + ":session.json"), payload)
        self.git("update-ref", "-d", "refs/heads/vendor", incoming)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        self.assertEqual(self.repo.resolve(incoming), incoming)
        self.assertEqual(self.repo.head(), local)
        self.assertEqual((self.path / ".git/index").read_bytes(), index)
        self.repo.release_session(pin["ref"], pin["commit"])
        with self.assertRaises(GitError):
            self.repo.resolve(pin["ref"])

    def test_session_pin_and_release_cannot_overwrite_refs_or_ignore_cas(self) -> None:
        local, incoming = self.branches()
        identifier = uuid.uuid4().hex
        pin = self.repo.pin_session(local, incoming, identifier, b"{}")
        with self.assertRaises(GitError):
            self.repo.pin_session(local, incoming, identifier, b"different")
        self.assertEqual(self.repo.resolve(pin["ref"]), pin["commit"])
        with self.assertRaises(GitError):
            self.repo.release_session(pin["ref"], local)
        self.assertEqual(self.repo.resolve(pin["ref"]), pin["commit"])
        with self.assertRaises(GitError):
            self.repo.release_session("refs/heads/main", local)
        with self.assertRaises(GitError):
            self.repo.pin_session(local, incoming, "../heads/main", b"{}")
        self.assertEqual(self.repo.head(), local)

    def test_session_pin_uses_service_identity_without_changing_config(self) -> None:
        local, incoming = self.branches()
        self.git("config", "user.name", "")
        self.git("config", "user.email", "")
        original = (self.path / ".git/config").read_bytes()
        pin = self.repo.pin_session(local, incoming, uuid.uuid4().hex, b"{}")
        author = self.git("show", "-s", "--format=%an <%ae>", pin["commit"]).decode().strip()
        self.assertEqual(author, "merge1c <merge1c@localhost.invalid>")
        self.assertEqual((self.path / ".git/config").read_bytes(), original)

    def test_publication_is_bound_to_checked_bytes_even_if_edit_predates_capture(self) -> None:
        local, incoming = self.branches()
        candidate = self.candidate_directory()
        expected = {file.relative_to(candidate).as_posix(): sha256(file.read_bytes()).hexdigest() for file in candidate.rglob("*") if file.is_file()}
        file = candidate / "local.txt"
        before = file.stat()
        file.write_bytes(b"evil!\n")
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(GitError, "checked snapshot"):
            self.repo.create_candidate(candidate, local, incoming, "unchecked-edit", "reject unchecked bytes", expected_hashes=expected)
        with self.assertRaises(GitError):
            self.repo.resolve("unchecked-edit")
        self.assertEqual(self.repo.head(), local)

    def test_checked_snapshot_covers_file_set_and_successful_exact_publication(self) -> None:
        local, incoming = self.branches()
        candidate = self.candidate_directory()
        expected = {file.relative_to(candidate).as_posix(): sha256(file.read_bytes()).hexdigest() for file in candidate.rglob("*") if file.is_file()}
        extra = candidate / "extra.txt"
        extra.write_bytes(b"not checked")
        with self.assertRaisesRegex(GitError, "file set"):
            self.repo.create_candidate(candidate, local, incoming, "unexpected-file", "reject extra file", expected_hashes=expected)
        extra.unlink()
        commit = self.repo.create_candidate(candidate, local, incoming, "verified-files", "checked exact bytes", expected_hashes=expected)
        self.assertEqual(self.git("show", commit + ":local.txt"), b"local\n")


if __name__ == "__main__":
    unittest.main()
