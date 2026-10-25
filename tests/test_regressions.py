from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from merge1c import Config, Tool, load_config, merge_directories, prepare_directories
from merge1c.session import Session, SessionError
from merge1c.engine import MergeError


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sides = [self.root / name for name in ("base", "local", "incoming")]
        for side in self.sides:
            side.mkdir()

    def test_directory_one_sided_xml_edit_preserves_incoming_lexical_style(self):
        base = b'<Form>\n <A>old</A>\n</Form>\n'
        local = b'\xef\xbb\xbf<Form>\r\n <A>new</A>\r\n</Form>\r\n'
        for side, data in zip(self.sides, (base, local, base), strict=True):
            (side / "form.xml").write_bytes(data)
        result = merge_directories(*self.sides, self.root / "out", config=Config(require_platform=False))
        self.assertTrue(result.resolved, result.to_dict())
        self.assertEqual((self.root / "out/form.xml").read_bytes(), base.replace(b"old", b"new"))

    def test_directory_incoming_only_uuid_replacement_requires_explicit_choice(self):
        def metadata(kind, identifier):
            return f'<MetaDataObject><{kind} uuid="{identifier}"><Properties><Name>X</Name></Properties></{kind}></MetaDataObject>'.encode()
        old = metadata("CommonForm", "00000000-0000-4000-8000-000000000001")
        new = metadata("Subsystem", "00000000-0000-4000-8000-000000000002")
        for side, data in zip(self.sides, (old, old, new), strict=True):
            (side / "object.xml").write_bytes(data)
        result = merge_directories(*self.sides, self.root / "out", config=Config(require_platform=False))
        self.assertFalse(result.resolved)
        self.assertEqual((self.root / "out/object.xml").read_bytes(), new)

    def test_work_files_with_cache_like_names_are_not_silently_dropped(self):
        for side in self.sides:
            for folder in ("__pycache__", ".merge1c"):
                (side / folder).mkdir()
                (side / folder / "tracked.bin").write_bytes(b"important")
        session = prepare_directories(*self.sides, self.root / "session", config=Config(require_platform=False))
        for folder in ("__pycache__", ".merge1c"):
            self.assertEqual((session.candidate / folder / "tracked.bin").read_bytes(), b"important")

    def test_case_collisions_between_sides_stop_before_writing(self):
        (self.sides[1] / "Report.xml").write_bytes(b"<Form><A>ours</A></Form>")
        (self.sides[2] / "report.xml").write_bytes(b"<Form><A>vendor</A></Form>")
        with self.assertRaises(MergeError):
            merge_directories(*self.sides, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_cross_side_directory_case_collision_stops_before_writing(self):
        for side, folder, name in ((self.sides[1], "Forms", "A.xml"), (self.sides[2], "forms", "B.xml")):
            (side / folder).mkdir()
            (side / folder / name).write_bytes(b"<Form/>")
        with self.assertRaises(MergeError):
            merge_directories(*self.sides, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_cross_side_case_insensitive_file_directory_collision(self):
        (self.sides[1] / "Form").write_bytes(b"ours")
        (self.sides[2] / "form").mkdir()
        (self.sides[2] / "form/B.xml").write_bytes(b"<Form/>")
        with self.assertRaises(MergeError):
            merge_directories(*self.sides, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_session_lock_prevents_a_second_writer(self):
        session = prepare_directories(*self.sides, self.root / "session", config=Config(require_platform=False))
        (session.path / "session.lock").write_text("{}")
        with self.assertRaises(SessionError):
            Session(session.path).check()
        self.assertTrue((session.path / "session.lock").exists())

    def test_validator_script_change_invalidates_its_passed_result(self):
        script = self.root / "validator.py"
        script.write_text("pass\n", encoding="utf-8")
        config = Config(require_platform=False, required_validators=("external",),
                        tools={"external": Tool(executable=sys.executable, args=(str(script),))})
        session = prepare_directories(*self.sides, self.root / "session", config=config)
        self.assertTrue(session.check(run_tools=True)["ready"])
        script.write_text("raise SystemExit(1)\n", encoding="utf-8")
        report = Session(session.path).report()
        self.assertFalse(report["ready"])
        self.assertIn("external", report["stale_tools"])

    def test_project_placeholder_uses_config_location(self):
        config_path = self.root / "merge1c.toml"
        config_path.write_text('[tools.check]\nargs = ["{project}/scripts/check.py", "{candidate}"]\n', encoding="utf-8")
        config = load_config(config_path)
        self.assertEqual(str(self.root.resolve()), config.project_root)

    def test_source_root_dot_means_full_tree_and_slashes_are_normalized(self):
        config_path = self.root / "merge1c.toml"
        config_path.write_text('[project]\nsource_roots = ["."]\n', encoding="utf-8")
        self.assertEqual((), load_config(config_path).source_roots)
        config_path.write_text('[project]\nsource_roots = ["src//cf/"]\n', encoding="utf-8")
        self.assertEqual(("src/cf",), load_config(config_path).source_roots)


if __name__ == "__main__":
    unittest.main()

