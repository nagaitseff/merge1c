"""End-to-end Git histories matching the release/develop/vendor workflow."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from merge1c.config import Config
from merge1c.engine import snapshot
from merge1c.session import Session, prepare_repository


FORM = "src/cf/CommonForms/Рабочая/Ext/Form.xml"


def form_with(items: list[tuple[str, int]]) -> bytes:
    fields = "\r\n".join(f'\t\t<InputField name="{name}" id="{number}"/>' for name, number in items)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">\r\n'
        '\t<!-- incoming source formatting must remain -->\r\n'
        f'\t<ChildItems>\r\n{fields}\r\n\t</ChildItems>\r\n</Form>\r\n'
    )
    return b"\xef\xbb\xbf" + body.encode("utf-8")


def bsl(name: str, value: int) -> bytes:
    return f"Процедура {name}()\r\n    Значение = {value};\r\nКонецПроцедуры\r\n".encode()


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-workflow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "master")
        for name, value in (("user.name", "Workflow test"), ("user.email", "workflow@example.invalid"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git("config", name, value)

    def process(self, *args: str, allowed=(0,)) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(["git", "-C", str(self.repo), "-c", "core.hooksPath=", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(result.returncode, allowed, "%s\n%s" % (args, result.stderr.decode(errors="replace")))
        return result

    def git(self, *args: str) -> bytes:
        return self.process(*args).stdout

    def put(self, path: str, data: bytes) -> None:
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").decode().strip()

    def assert_parents(self, commit: str, *parents: str) -> None:
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", commit).decode().split(), [commit, *parents])

    def assert_form(self, data: bytes, expected: list[str]) -> dict[str, str]:
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"<<<<<<<", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"<!-- incoming source formatting must remain -->", data)
        tree = ET.fromstring(data)
        children = next(element for element in tree if element.tag.rsplit("}", 1)[-1] == "ChildItems")
        self.assertEqual([item.attrib["name"] for item in children], expected)
        ids = {item.attrib["name"]: item.attrib["id"] for item in children}
        self.assertEqual(len(set(ids.values())), len(ids))
        return ids

    def test_two_develop_sync_cycles_preserve_existing_history_and_ongoing_work(self) -> None:
        self.put(FORM, form_with([("A", 1)]))
        initial = self.commit("initial supplier release")
        self.git("checkout", "-q", "-b", "develop")
        self.put("ongoing/Незавершенная.bsl", bsl("Незавершенная", 1))
        draft = self.commit("ongoing development stays outside release")
        task_path = "tasks/Задача1234.bsl"
        self.put(task_path, bsl("ГотоваяЗадача", 1))
        original_task = self.commit("feature/1234 accepted to develop")
        self.put(FORM, form_with([("A", 1), ("Mine", 2), ("Free", 3)]))
        local_first = self.commit("local form additions")

        self.git("checkout", "-q", "master")
        # This is fixture construction using native Git, not a cherry-pick engine.
        self.git("cherry-pick", original_task)
        copied_task = self.git("rev-parse", "HEAD").decode().strip()
        self.assertNotEqual(copied_task, original_task)
        self.put(task_path, bsl("ГотоваяЗадача", 2))
        self.put(FORM, form_with([("A", 1), ("Vendor", 2)]))
        master_first = self.commit("released adaptation and supplier form update")
        self.git("checkout", "-q", "develop")
        before = snapshot(self.repo)
        config = Config(profile="develop_sync", incoming_role="master", require_platform=False)
        session = prepare_repository(self.repo, "master", self.root / "cycle-one", config=config)
        self.assertEqual(session.data["git"]["base"], initial)
        self.assertEqual(snapshot(self.repo), before)
        self.assertFalse(session.report()["ready"])
        self.assertIn(task_path, {item["path"] for item in session.report()["issues"]})
        # The released correction is explicitly accepted; the unrelated draft survives.
        report = Session(session.path).resolve(task_path, take="incoming")
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["incoming_role"], "master")
        self.assertEqual((session.candidate / "ongoing/Незавершенная.bsl").read_bytes(), bsl("Незавершенная", 1))
        ids = self.assert_form((session.candidate / FORM).read_bytes(), ["A", "Vendor", "Mine", "Free"])
        self.assertEqual(ids, {"A": "1", "Vendor": "2", "Mine": "4", "Free": "3"})
        first_merge = Session(session.path).create_branch("candidate/sync-one")
        self.assert_parents(first_merge, local_first, master_first)
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), local_first)
        self.git("merge", "--ff-only", "candidate/sync-one")
        self.assertEqual(self.git("branch", "--show-current").decode().strip(), "develop")
        self.git("merge-base", "--is-ancestor", draft, "develop")
        self.git("merge-base", "--is-ancestor", original_task, "develop")
        self.git("merge-base", "--is-ancestor", master_first, "develop")

        self.put("ongoing/Следующая.bsl", bsl("Следующая", 7))
        local_second = self.commit("more ongoing development")
        self.git("checkout", "-q", "master")
        self.put(FORM, form_with([("A", 1), ("Vendor", 2), ("VendorNext", 10)]))
        self.put("release/ВторойРелиз.bsl", bsl("ВторойРелиз", 2))
        master_second = self.commit("next released update")
        self.git("checkout", "-q", "develop")
        second = prepare_repository(self.repo, "master", self.root / "cycle-two", config=config)
        self.assertEqual(second.data["git"]["base"], master_first)
        self.assertTrue(second.check()["ready"], second.report())
        second_ids = self.assert_form((second.candidate / FORM).read_bytes(), ["A", "Vendor", "VendorNext", "Mine", "Free"])
        self.assertEqual(second_ids["Mine"], "4")
        self.assertEqual(second_ids["Free"], "3")
        second_merge = second.create_branch("candidate/sync-two")
        self.assert_parents(second_merge, local_second, master_second)
        self.git("merge", "--ff-only", "candidate/sync-two")
        for ancestor in (first_merge, local_first, local_second, original_task, master_first, master_second):
            self.git("merge-base", "--is-ancestor", ancestor, "develop")
        self.assertEqual((self.repo / "ongoing/Незавершенная.bsl").read_bytes(), bsl("Незавершенная", 1))
        self.assertEqual((self.repo / "ongoing/Следующая.bsl").read_bytes(), bsl("Следующая", 7))
        self.assertEqual((self.repo / task_path).read_bytes(), bsl("ГотоваяЗадача", 2))
        self.assertEqual(self.git("rev-parse", "master").decode().strip(), master_second)

    def test_unrelated_metadata_types_are_not_paired_by_xml_template_similarity(self) -> None:
        old = "src/cf/CommonForms/ИнформацияОНевозможностиДобавленияОтсканированного.xml"
        own = "src/cfe/YAXUNIT/DataProcessors/ЮТHTTPОтвет.xml"
        vendor = "src/cf/Subsystems/Продажи/Subsystems/РаботаСТорговымиПлощадками.xml"
        def metadata(kind: str, name: str, uuid: str) -> bytes:
            shared = "\r\n".join("Shared generated XML boilerplate line %03d" % number for number in range(150))
            return (f'<?xml version="1.0" encoding="UTF-8"?>\r\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\r\n<{kind} uuid="{uuid}">\r\n<Properties>\r\n<Name>{name}</Name>\r\n<Comment>\r\n{shared}\r\n</Comment>\r\n</Properties>\r\n</{kind}>\r\n</MetaDataObject>\r\n').encode()
        base_data = metadata("CommonForm", "Информация", "11111111-2222-4333-8444-555555555555")
        own_data = metadata("DataProcessor", "ЮТHTTPОтвет", "22222222-2222-4333-8444-555555555555")
        vendor_data = metadata("Subsystem", "РаботаСТорговымиПлощадками", "33333333-2222-4333-8444-555555555555")
        self.put(old, base_data)
        self.commit("old generated metadata")
        self.git("checkout", "-q", "-b", "vendor")
        (self.repo / old).unlink()
        self.put(vendor, vendor_data)
        incoming = self.commit("remove old form and introduce subsystem")
        self.git("checkout", "-q", "master")
        (self.repo / old).unlink()
        self.put(own, own_data)
        local = self.commit("remove old form and introduce extension processor")
        misleading = self.process("-c", "merge.renames=true", "merge-tree", "--write-tree", "-z", local, incoming, allowed=(0, 1))
        self.assertEqual(misleading.returncode, 1, "Fixture should reproduce the false rename pairing")
        self.assertIn(b"rename/rename", misleading.stdout)
        session = prepare_repository(self.repo, "vendor", self.root / "metadata-session", config=Config(require_platform=False))
        self.assertTrue(session.check()["ready"], session.report())
        self.assertFalse((session.candidate / old).exists())
        self.assertEqual((session.candidate / own).read_bytes(), own_data)
        self.assertEqual((session.candidate / vendor).read_bytes(), vendor_data)
        self.assertFalse(any(item["code"] == "metadata.rename" for item in session.report()["changes"]))


if __name__ == "__main__":
    unittest.main()
