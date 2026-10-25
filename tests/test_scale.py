"""Moderate-size behavioural checks; no fragile elapsed-time thresholds."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from examples.benchmark import make_inputs, without_local_additions
from merge1c.xmlmerge import inspect_xml, merge_xml


class ScaleTests(unittest.TestCase):
    def test_hundreds_of_fields_keep_incoming_bytes_and_scoped_ids(self):
        records, additions = 300, 12
        base, local, incoming = make_inputs(records, additions)
        result = merge_xml(base, local, incoming)
        self.assertTrue(result.resolved, result.to_dict())
        self.assertEqual(without_local_additions(result.content), incoming)
        self.assertEqual(result.content[:3], b"\xef\xbb\xbf")
        self.assertFalse(any(issue.blocking for issue in inspect_xml(result.content)))
        form = ET.fromstring(result.content)
        fields = list(form.find("{*}ChildItems"))
        attributes = list(form.find("{*}Attributes"))
        self.assertEqual(len(fields), records + 2 * additions)
        self.assertEqual(len(attributes), records + 2 * additions)
        gap = [node.attrib["name"] for node in fields[records // 2:records // 2 + 2 * additions]]
        self.assertTrue(all(name.startswith("Типовое") for name in gap[:additions]))
        self.assertTrue(all(name.startswith("Наше") for name in gap[additions:]))
        ui_ids = [int(node.attrib["id"]) for field in fields for node in field.iter() if "id" in node.attrib]
        attr_ids = [int(node.attrib["id"]) for node in attributes]
        self.assertEqual(len(set(ui_ids)), len(ui_ids))
        self.assertEqual(len(set(attr_ids)), len(attr_ids))
        self.assertEqual(sum(change.code == "xml.id_remapped" for change in result.changes), additions * 4)

    def test_repeated_scale_merge_is_byte_idempotent(self):
        base, local, incoming = make_inputs(200, 8)
        first = merge_xml(base, local, incoming)
        self.assertTrue(first.resolved, first.to_dict())
        second = merge_xml(base, local, first.content)
        self.assertTrue(second.resolved, second.to_dict())
        self.assertEqual(second.content, first.content)

    def test_large_noop_returns_exact_incoming_source(self):
        base, _, incoming = make_inputs(400, 10)
        result = merge_xml(base, base, incoming)
        self.assertTrue(result.resolved, result.to_dict())
        self.assertEqual(result.content, incoming)

    def test_many_same_gap_additions_keep_all_vendor_nodes_and_comments(self):
        base, local, incoming = make_inputs(250, 150)
        result = merge_xml(base, local, incoming)
        self.assertTrue(result.resolved, result.to_dict())
        self.assertEqual(without_local_additions(result.content), incoming)
        self.assertEqual(sum(change.code == "xml.id_remapped" for change in result.changes), 600)


if __name__ == "__main__":
    unittest.main()
