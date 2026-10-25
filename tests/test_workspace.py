from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from merge1c import Config, load_config, prepare_directories
from merge1c.cli import DEFAULT_CONFIG, LOCAL_EXAMPLE
from merge1c.session import Session
from merge1c.workspace import StorageError, check_location, check_storage, session_location, storage_root


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-storage-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_annotated_templates_load_and_local_storage_overrides(self):
        project = self.root / "merge1c.toml"
        project.write_text(DEFAULT_CONFIG, encoding="utf-8")
        local = self.root / "merge1c.local.toml"
        local.write_text(LOCAL_EXAMPLE.replace("session_root = ''", "session_root = 'work'"), encoding="utf-8")
        config = load_config(project, local_path=local)
        self.assertEqual(storage_root(config), self.root / "work")
        self.assertEqual(load_config(project, profile="develop_sync").incoming_role, "master")

    def test_user_temp_is_default_and_explicit_session_has_priority(self):
        with patch("merge1c.workspace.tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(storage_root(Config()), self.root / "merge1c-sessions")
        config = Config(session_root=str(self.root / "configured"))
        requested = self.root / "explicit"
        self.assertEqual(session_location(config, requested), requested)
        first = session_location(config, None, label="vendor/2:1?*")
        second = session_location(config, None, label="vendor/2:1?*")
        self.assertEqual(first.parent, self.root / "configured")
        self.assertNotEqual(first, second)
        self.assertNotIn(":", first.name)
        self.assertFalse(first.exists())

    def test_probe_preserves_existing_files_and_leaves_no_scratch(self):
        directory = self.root / "storage"
        directory.mkdir()
        marker = directory / "existing.bin"
        marker.write_bytes(b"keep")
        self.assertEqual(check_storage(directory), directory)
        self.assertEqual(list(directory.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_rename_denied_is_reported_even_when_writes_are_allowed(self):
        original = Path.rename
        def rename(path, target):
            if path.name == "before":
                raise PermissionError("directory rename denied")
            return original(path, target)
        with patch.object(Path, "rename", rename), self.assertRaisesRegex(StorageError, "переименовать каталог") as caught:
            check_storage(self.root / "storage")
        self.assertIn("storage.session_root", str(caught.exception))
        self.assertEqual(list((self.root / "storage").iterdir()), [])

    def test_parent_creation_denied_has_actionable_error(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("no create rights")):
            with self.assertRaisesRegex(StorageError, "создать каталог хранения"):
                check_storage(self.root / "denied")

    def test_inside_source_is_rejected_before_any_write(self):
        source = self.root / "repository"
        source.mkdir()
        with self.assertRaises(StorageError):
            check_storage(source / "sessions", forbidden_roots=(source,))
        with self.assertRaises(StorageError):
            check_location(source / "session", (source,))
        self.assertEqual(list(source.iterdir()), [])

    def test_automatic_directory_session_uses_configured_root(self):
        sides = [self.root / side for side in ("base", "local", "incoming")]
        for side in sides:
            side.mkdir()
            (side / "same.txt").write_bytes(b"same")
        storage = self.root / "sessions"
        result = prepare_directories(*sides, config=Config(session_root=str(storage), require_platform=False))
        self.assertEqual(result.path.parent, storage)
        self.assertTrue(result.report()["ready"])
        self.assertEqual((result.candidate / "same.txt").read_bytes(), b"same")

    def test_previous_session_without_storage_field_remains_readable(self):
        sides = [self.root / side for side in ("base", "local", "incoming")]
        for side in sides:
            side.mkdir()
        result = prepare_directories(*sides, self.root / "session", config=Config(require_platform=False))
        manifest = result.path / "session.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        del data["config"]["session_root"]
        data["config_hash"] = sha256(json.dumps(data["config"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(Session(result.path).check()["ready"])

    def test_bom_config_and_toml_error_show_line_and_hint(self):
        config = self.root / "merge1c.toml"
        config.write_text("[storage]\nsession_root = 'work'\n", encoding="utf-8-sig")
        self.assertEqual(load_config(config).session_root, "work")
        config.write_text("[project]\nsource_roots = [src/cf]\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Строка 2") as caught:
            load_config(config)
        self.assertIn("source_roots = ['src/cf']", str(caught.exception))
        self.assertIn("^", str(caught.exception))

    def test_storage_types_unknown_keys_and_local_scope_are_strict(self):
        project, local = self.root / "merge1c.toml", self.root / "local.toml"
        for value in ("123", "true", "[]", "'   '"):
            project.write_text(f"[storage]\nsession_root = {value}\n", encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_config(project)
        project.write_text("[storage]\nwrong_key = 'x'\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(project)
        project.write_text("", encoding="utf-8")
        local.write_text("[storage]\nwrong_key = 'x'\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(project, local_path=local)
