from __future__ import annotations

from pathlib import Path
import random
import tempfile
import unittest

from merge1c.config import Config
from merge1c.engine import MergeError, _remapped_files, merge_directories, snapshot, validate_directory


OBJECT_UUID = "11111111-2222-4333-8444-555555555555"
FORM_PATH = "src/cf/CommonForms/Тест/Ext/Form.xml"
MODULE_PATH = FORM_PATH[:-4] + "/Module.bsl"


def metadata(name: str = "Старая", *, kind: str = "CommonForm", uuid: str = OBJECT_UUID) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        f'<{kind} uuid="{uuid}"><Properties><Name>{name}</Name></Properties></{kind}>'
        '</MetaDataObject>\r\n'
    ).encode("utf-8")


def form(handler: str | None = None) -> bytes:
    events = "" if handler is None else f'<Events><Event name="OnOpen">{handler}</Event></Events>'
    return (f'<?xml version="1.0" encoding="UTF-8"?><Form xmlns="http://v8.1c.ru/8.3/xcf/logform">{events}</Form>').encode()


def method(name: str = "Обработчик", value: str = "1") -> bytes:
    return (f"Процедура {name}()\r\n    Значение = {value};\r\nКонецПроцедуры\r\n").encode()


def put(root: Path, name: str, content: bytes) -> None:
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)


class PathMappingTests(unittest.TestCase):
    def test_indexed_mapping_matches_reference_on_randomized_nested_paths(self):
        def reference_mapping(files, rules):
            result = {}
            ordered_rules = sorted(rules, key=lambda pair: len(pair[0]), reverse=True)
            for original in sorted(files):
                target_path = original
                for source, target in ordered_rules:
                    if original == source:
                        target_path = target
                        break
                    if source.endswith(".xml") and original.startswith(source[:-4] + "/"):
                        target_path = target[:-4] + original[len(source) - 4:]
                        break
                result.setdefault(target_path, original)
            return result

        generator = random.Random(42)
        for case in range(300):
            paths = ["/".join(generator.choices(("A", "B", "C", "D", "E"),
                                               k=generator.randint(1, 4))) + ".xml"
                     for _ in range(40)]
            rules = [(path, path if generator.random() < 0.4 else "Renamed/" + path)
                     for path in paths]
            files = {path: "hash" for path in
                     [*paths, *(path[:-4] + "/Ext/Module.bsl" for path in paths)]}
            with self.subTest(case=case):
                self.assertEqual(_remapped_files(files, rules, [], "local"),
                                 reference_mapping(files, rules))

    def test_nested_identity_rule_is_a_barrier_to_parent_relocation(self):
        files = {name: "hash" for name in (
            "Documents/A.xml", "Documents/A/Ext/Module.bsl",
            "Documents/A/Forms/F.xml", "Documents/A/Forms/F/Ext/Form.xml",
        )}
        rules = [("Documents/A.xml", "Documents/B.xml"),
                 ("Documents/A/Forms/F.xml", "Documents/A/Forms/F.xml")]
        self.assertEqual(_remapped_files(files, rules, [], "local"), {
            "Documents/B.xml": "Documents/A.xml",
            "Documents/B/Ext/Module.bsl": "Documents/A/Ext/Module.bsl",
            "Documents/A/Forms/F.xml": "Documents/A/Forms/F.xml",
            "Documents/A/Forms/F/Ext/Form.xml": "Documents/A/Forms/F/Ext/Form.xml",
        })

    def test_deepest_rename_preserves_owned_subtree_and_first_duplicate_rule(self):
        files = {"Documents/A/Forms/F.xml": "hash", "Documents/A/Forms/F/Ext/Form.xml": "hash"}
        rules = [("Documents/A.xml", "Documents/B.xml"),
                 ("Documents/A/Forms/F.xml", "Documents/B/Forms/G.xml"),
                 ("Documents/A/Forms/F.xml", "Documents/Wrong.xml")]
        self.assertEqual(_remapped_files(files, rules, [], "local"), {
            "Documents/B/Forms/G.xml": "Documents/A/Forms/F.xml",
            "Documents/B/Forms/G/Ext/Form.xml": "Documents/A/Forms/F/Ext/Form.xml",
        })

    def test_large_unchanged_configuration_does_not_scan_all_rules_per_path(self):
        activity = {"comparisons": 0, "prefix_checks": 0}

        class TrackedPath(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                activity["comparisons"] += 1
                return str.__eq__(self, other)

            def startswith(self, *args):
                activity["prefix_checks"] += 1
                return str.startswith(self, *args)

        rules = [(f"CommonModules/M{i:05d}.xml", f"CommonModules/M{i:05d}.xml") for i in range(600)]
        files = {TrackedPath(path): "hash" for path in
                 [*(source for source, _ in rules), *(source[:-4] + "/Ext/Module.bsl" for source, _ in rules)]}
        result = _remapped_files(files, rules, [], "local")
        self.assertEqual(result, {path: path for path in files})
        # Count semantic path comparisons rather than rely on machine timings.
        self.assertLessEqual(sum(activity.values()), len(files) * 10)


class DirectoryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-engine-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base, self.local, self.incoming = [self.root / side for side in ("base", "local", "incoming")]
        for path in (self.base, self.local, self.incoming):
            path.mkdir()
        self.out = self.root / "candidate"
        self.config = Config(require_platform=False)

    def everywhere(self, name: str, content: bytes) -> None:
        for root in (self.base, self.local, self.incoming):
            put(root, name, content)

    def merge(self, **kwargs):
        return merge_directories(self.base, self.local, self.incoming, self.out, config=self.config, **kwargs)

    def test_clean_file_merges_still_validate_cross_file_handler_bindings(self) -> None:
        self.everywhere(FORM_PATH, form())
        self.everywhere(MODULE_PATH, method())
        put(self.local, MODULE_PATH, b"")
        put(self.incoming, FORM_PATH, form("Обработчик"))
        # Neither input side has a dangling handler; the combination does.
        self.assertFalse(any(item.blocking for item in validate_directory(self.local, self.config)))
        self.assertFalse(any(item.blocking for item in validate_directory(self.incoming, self.config)))
        result = self.merge()
        self.assertFalse(result.resolved)
        self.assertIn("form.missing_handler", {issue.code for issue in result.issues})
        self.assertEqual((self.out / FORM_PATH).read_bytes(), form("Обработчик"))

    def test_one_sided_addition_and_deletion_preserve_other_side_work(self) -> None:
        self.everywhere("remove.bin", b"\x00base\xff")
        (self.incoming / "remove.bin").unlink()
        put(self.local, "Local/added.bin", b"\x00local\xff")
        put(self.incoming, "Vendor/added.bin", b"\x00vendor\xfe")
        before = [snapshot(side) for side in (self.base, self.local, self.incoming)]
        result = self.merge()
        self.assertTrue(result.resolved)
        self.assertFalse((self.out / "remove.bin").exists())
        self.assertEqual((self.out / "Local/added.bin").read_bytes(), b"\x00local\xff")
        self.assertEqual((self.out / "Vendor/added.bin").read_bytes(), b"\x00vendor\xfe")
        self.assertEqual([snapshot(side) for side in (self.base, self.local, self.incoming)], before)

    def test_delete_versus_edit_is_explicit_and_keeps_a_reviewable_file(self) -> None:
        self.everywhere("Common/value.bin", b"\x00base")
        (self.local / "Common/value.bin").unlink()
        put(self.incoming, "Common/value.bin", b"\x00changed")
        result = self.merge()
        self.assertFalse(result.resolved)
        self.assertIn("file.delete_edit", {issue.code for issue in result.issues})
        self.assertEqual((self.out / "Common/value.bin").read_bytes(), b"\x00changed")

    def test_binary_add_add_and_concurrent_changes_never_claim_success(self) -> None:
        self.everywhere("changed.bin", b"\x00base")
        put(self.local, "changed.bin", b"\x00local")
        put(self.incoming, "changed.bin", b"\x00incoming")
        put(self.local, "new.bin", b"\x00new local")
        put(self.incoming, "new.bin", b"\x00new incoming")
        result = self.merge()
        self.assertFalse(result.resolved)
        self.assertTrue({"file.concurrent_change", "file.add_add"}.issubset({issue.code for issue in result.issues}))
        self.assertNotIn(b"<<<<<<<", (self.out / "changed.bin").read_bytes())

    def test_metadata_rename_moves_owned_module_and_keeps_local_edit(self) -> None:
        old = "src/cf/CommonForms/Старая.xml"
        new = "src/cf/CommonForms/Новая.xml"
        old_module = old[:-4] + "/Ext/Form/Module.bsl"
        new_module = new[:-4] + "/Ext/Form/Module.bsl"
        self.everywhere(old, metadata())
        self.everywhere(old_module, method(value="1"))
        put(self.local, old_module, method(value="2"))
        (self.incoming / old).unlink()
        put(self.incoming, new, metadata("Новая"))
        (self.incoming / old_module).unlink()
        put(self.incoming, new_module, method(value="1"))
        before = [snapshot(side) for side in (self.base, self.local, self.incoming)]
        result = self.merge()
        self.assertTrue(result.resolved, result.to_dict())
        self.assertFalse((self.out / old).exists())
        self.assertFalse((self.out / old_module).exists())
        self.assertEqual((self.out / new_module).read_bytes(), method(value="2"))
        self.assertEqual((self.out / new).read_bytes(), metadata("Новая"))
        self.assertEqual([snapshot(side) for side in (self.base, self.local, self.incoming)], before)

    def test_metadata_rename_delete_requires_decision(self) -> None:
        old = "src/cf/CommonForms/Старая.xml"
        new = "src/cf/CommonForms/Новая.xml"
        self.everywhere(old, metadata())
        (self.local / old).unlink()
        (self.incoming / old).unlink()
        put(self.incoming, new, metadata("Новая"))
        result = self.merge()
        self.assertFalse(result.resolved, "Rename/delete must not resurrect the deleted object silently")
        self.assertTrue(any("rename" in issue.code and "delete" in issue.code for issue in result.issues), result.to_dict())

    def test_same_path_different_metadata_types_is_not_a_content_merge(self) -> None:
        path = "src/cf/CommonForms/Объект.xml"
        put(self.base, path, metadata(kind="CommonForm"))
        put(self.local, path, metadata(kind="DataProcessor"))
        put(self.incoming, path, metadata(kind="Subsystem"))
        result = self.merge()
        self.assertFalse(result.resolved)
        self.assertTrue(any("identity" in issue.code for issue in result.issues), result.to_dict())
        self.assertNotIn(b"<<<<<<<", (self.out / path).read_bytes())

    def test_file_directory_collision_is_an_explicit_domain_error(self) -> None:
        put(self.local, "Объект", b"file")
        put(self.incoming, "Объект/child.bin", b"child")
        try:
            result = self.merge()
        except MergeError:
            return
        self.assertFalse(result.resolved)
        self.assertTrue(any(issue.blocking for issue in result.issues))

    def test_output_overlap_is_rejected_before_any_input_changes(self) -> None:
        self.everywhere("important.bin", b"keep")
        before = [snapshot(side) for side in (self.base, self.local, self.incoming)]
        for output in (self.local, self.local / "candidate", self.root):
            with self.subTest(output=output), self.assertRaises(MergeError):
                merge_directories(self.base, self.local, self.incoming, output, config=self.config)
        self.assertEqual([snapshot(side) for side in (self.base, self.local, self.incoming)], before)

    def test_progress_has_exact_final_counts_and_no_false_success(self) -> None:
        self.everywhere("conflict.bin", b"base")
        put(self.local, "conflict.bin", b"local")
        put(self.incoming, "conflict.bin", b"incoming")
        events = []
        result = self.merge(progress=events.append)
        self.assertEqual(events[-1]["phase"], "complete")
        self.assertEqual(events[-1]["files"], result.files)
        self.assertFalse(events[-1]["resolved"])
        self.assertEqual(events[-1]["unresolved"], sum(item.blocking for item in result.issues))

    def test_input_change_during_merge_blocks_success(self) -> None:
        self.everywhere("value.bin", b"base")
        put(self.incoming, "value.bin", b"incoming")
        changed = False
        def modify_input(event):
            nonlocal changed
            if event["phase"] == "merge" and not changed:
                put(self.incoming, "value.bin", b"changed during merge")
                changed = True
        result = self.merge(progress=modify_input)
        self.assertFalse(result.resolved)
        self.assertIn("input.changed", {issue.code for issue in result.issues})


if __name__ == "__main__":
    unittest.main()
