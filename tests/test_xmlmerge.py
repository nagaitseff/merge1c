"""Behavioural tests for source preservation and conservative XML merging."""

from __future__ import annotations

import unittest
import sys
from xml.etree import ElementTree as ET

from merge1c.xmlmerge import inspect_xml, merge_xml
from merge1c.xmlsource import XMLSource


def xml(body: str, *, newline="\n", bom=False, attrs="") -> bytes:
    source = '<?xml version="1.0" encoding="UTF-8"?>\n<Form version="2.20"' + attrs + ">\n" + body + "\n</Form>\n"
    return (b"\xef\xbb\xbf" if bom else b"") + source.replace("\n", newline).encode("utf-8")


def codes(result):
    return {issue.code for issue in result.issues}


def form_items(source):
    return [node.attrib["name"] for node in ET.fromstring(source).find("ChildItems")]


class XMLMergeTests(unittest.TestCase):
    def assertResolved(self, result):
        self.assertTrue(result.resolved, result.to_dict())

    def test_source_spans_use_bytes_for_cyrillic_comments_cdata_and_quoted_gt(self):
        data = b'\xef\xbb\xbf' + '<Form><InputField title="x > y" name="Поле" id="7"><V><![CDATA[<x> & y]]></V><!--коммент--></InputField></Form>'.encode()
        source = XMLSource(data)
        field = source.root.children[0]
        self.assertEqual(field.raw, '<InputField title="x > y" name="Поле" id="7"><V><![CDATA[<x> & y]]></V><!--коммент--></InputField>'.encode())
        self.assertEqual(field.children[0].text, "<x> & y")
        attr = field.attribute("name")
        self.assertEqual(data[attr.value_start:attr.value_end], "Поле".encode())

    def test_noop_preserves_incoming_bom_crlf_declaration_quotes_and_comments(self):
        base = xml("\t<Value>old</Value>")
        incoming = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\r\n<Form version=\'2.20\'>\r\n <!-- retain -->\r\n <Value><![CDATA[new & <x>]]></Value>\r\n</Form>\r\n'
        result = merge_xml(base, base, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming)

    def test_disjoint_scalar_changes_preserve_incoming_unrelated_bytes(self):
        base = xml("\t<A>one</A>\n\t<B>old</B>")
        local = xml("\t<A>два &amp; три</A>\n\t<B>old</B>")
        incoming = xml("  <!-- untouched -->\n\t<A>one</A>\n  <B><![CDATA[new]]></B>", newline="\r\n", bom=True)
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming.replace(b"<A>one</A>", "<A>два &amp; три</A>".encode()))

    def test_same_scalar_edit_is_already_applied(self):
        base = xml("<A>old</A>")
        local = xml("<A>new</A>")
        incoming = xml("  <A>new</A>", newline="\r\n", bom=True)
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming)

    def test_conflicting_scalar_keeps_incoming_without_markers(self):
        base, local, incoming = [xml(f"<A>{value}</A>") for value in ("base", "ours", "theirs")]
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.value_conflict", codes(result))
        self.assertEqual(result.content, incoming)
        self.assertNotIn(b"<<<<<<<", result.content)

    def test_attributes_merge_independently_and_keep_quotes_order(self):
        base = xml('<A x="one" y="two"/>')
        local = xml('<A x="ours" y="two"/>')
        incoming = xml("<A  y='theirs' x = 'one' />")
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming.replace(b"x = 'one'", b"x = 'ours'"))

    def test_empty_scalar_can_be_expanded_without_reserializing_parent(self):
        base = xml(" <Comment />\n <B>old</B>")
        local = xml(" <Comment>text</Comment>\n <B>old</B>")
        incoming = xml(" <Comment />\n <B>new</B>", newline="\r\n")
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming.replace(b"<Comment />", b"<Comment >text</Comment>"))

    def test_same_gap_incoming_first_then_local_preserving_order(self):
        before = '<InputField name="A" id="1"/>'
        after = '<InputField name="B" id="2"/>'
        own = '<InputField name="Local" id="3"/>'
        other = '<InputField name="Vendor" id="4"/>'
        def wrap(items):
            return xml("\t<ChildItems>\n" + "\n".join("\t\t" + item for item in items) + "\n\t</ChildItems>")

        result = merge_xml(wrap([before, after]), wrap([before, own, after]), wrap([before, other, after]))
        self.assertResolved(result)
        self.assertEqual(form_items(result.content), ["A", "Vendor", "Local", "B"])

    def test_collision_remaps_local_only_and_preserves_free_local_id(self):
        base = xml('\t<ChildItems>\n\t\t<InputField name="A" id="1"/>\n\t</ChildItems>')
        local = base.replace(b"\t</ChildItems>", b'\t\t<InputField name="Mine" id="2"><Width>2</Width></InputField>\n\t\t<InputField name="Free" id="3"/>\n\t</ChildItems>')
        incoming = base.replace(b"\t</ChildItems>", b'\t\t<InputField name="Vendor" id="2"/>\n\t</ChildItems>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        root = ET.fromstring(result.content)
        ids = {e.attrib["name"]: e.attrib["id"] for e in root.find("ChildItems")}
        self.assertEqual(ids, {"A": "1", "Vendor": "2", "Mine": "4", "Free": "3"})
        self.assertIn(b"<Width>2</Width>", result.content)
        self.assertEqual([c.code for c in result.changes].count("xml.id_remapped"), 1)

    def test_attribute_command_item_and_column_scopes_are_independent(self):
        data = xml('''\t<AutoCommandBar name="Bar" id="-1"/>
\t<ChildItems><InputField name="Field" id="1"><DataPath>Rows.A</DataPath></InputField></ChildItems>
\t<Attributes>
\t\t<Attribute name="Rows" id="1"><Columns><Column name="A" id="1"/></Columns></Attribute>
\t\t<Attribute name="OtherRows" id="2"><Columns><Column name="B" id="1"/></Columns></Attribute>
\t</Attributes>
\t<Commands><Command name="Run" id="1"><Action>Run</Action></Command></Commands>''')
        self.assertFalse(any(i.blocking for i in inspect_xml(data)))
        result = merge_xml(data, data, data)
        self.assertResolved(result)
        self.assertEqual(result.content, data)

    def test_duplicate_numeric_id_in_same_scope_is_rejected_even_noop(self):
        data = xml('<ChildItems><InputField name="A" id="1"/><InputField name="B" id="1"/></ChildItems>')
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn("xml.duplicate_id", codes(result))

    def test_unknown_numeric_reference_blocks_collision_remap(self):
        base = xml('<ChildItems/>')
        local = xml('<ChildItems><InputField name="Mine" id="1"><MysteryReference>1</MysteryReference></InputField></ChildItems>')
        incoming = xml('<ChildItems><InputField name="Vendor" id="1"/></ChildItems>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.unknown_id_reference", codes(result))
        self.assertEqual(result.content, incoming)

    def test_equivalent_independent_additions_deduplicate_with_incoming_ids(self):
        base = xml('<ChildItems/>')
        local = xml('<ChildItems><InputField name="A" id="5"><Width>20</Width></InputField></ChildItems>')
        incoming = xml('<ChildItems><InputField name="A" id="2"><Width>20</Width></InputField></ChildItems>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming)
        self.assertIn("xml.addition_deduplicated", {c.code for c in result.changes})

    def test_same_added_name_with_different_content_conflicts(self):
        base = xml('<Attributes/>')
        local = xml('<Attributes><Attribute name="A" id="5"><Type>ours</Type></Attribute></Attributes>')
        incoming = xml('<Attributes><Attribute name="A" id="2"><Type>theirs</Type></Attribute></Attributes>')
        result = merge_xml(base, local, incoming)
        self.assertIn("xml.add_add", codes(result))
        self.assertFalse(result.resolved)

    def test_delete_edit_is_conflict(self):
        base = xml('<Attributes><Attribute name="A" id="1"><Type>old</Type></Attribute></Attributes>')
        local = xml('<Attributes/>')
        incoming = base.replace(b">old<", b">new<")
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.delete_edit", codes(result))
        self.assertEqual(result.content, incoming)

    def test_local_deletion_preserves_unrelated_incoming_change(self):
        base = xml(' <A>one</A>\n <B>old</B>')
        local = xml(' <B>old</B>')
        incoming = base.replace(b"<B>old</B>", b"<B>new</B>")
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming.replace(b" <A>one</A>\n", b""))

    def test_anchor_reversal_is_explicit_conflict(self):
        def wrap(names):
            return xml('<ChildItems>' + ''.join(f'<InputField name="{name}" id="{ord(name)}"/>' for name in names) + '</ChildItems>')

        result = merge_xml(wrap("AB"), wrap("AXB"), wrap("BA"))
        self.assertFalse(result.resolved)
        self.assertIn("xml.anchor_order", codes(result))

    def test_lost_parent_is_not_silently_recreated(self):
        base = xml('<ChildItems><UsualGroup name="Group" id="1"><ChildItems/></UsualGroup></ChildItems>')
        local = base.replace(b'<ChildItems/>', b'<ChildItems><InputField name="Mine" id="2"/></ChildItems>')
        incoming = xml('<ChildItems/>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.edit_delete", codes(result))
        self.assertEqual(result.content, incoming)

    def test_concurrent_unknown_complex_edits_are_not_guessed(self):
        base = xml('<Mystery><A>oldA</A><B>oldB</B></Mystery>')
        local = base.replace(b"oldA", b"ourA")
        incoming = base.replace(b"oldB", b"newB")
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.unsupported_structure", codes(result))

    def test_local_only_opaque_subtree_is_copied_without_losing_unknown_nodes(self):
        base = xml('<Mystery><Known>old</Known><Unknown p="x"/></Mystery>\n<B>old</B>')
        local = base.replace(b"<Known>old</Known>", b"<Known>new</Known><!--local-->")
        incoming = base.replace(b"<B>old</B>", b"<B>vendor</B>")
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertIn(b'<Unknown p="x"/>', result.content)
        self.assertIn(b"<!--local-->", result.content)
        self.assertIn(b"<B>vendor</B>", result.content)

    def test_inserted_comment_is_carried_once(self):
        base = xml('\t<ChildItems>\n\t\t<InputField name="A" id="1"/>\n\t</ChildItems>')
        local = base.replace(b"\t</ChildItems>", b'\t\t<!--local addition-->\n\t\t<InputField name="Mine" id="2"/>\n\t</ChildItems>')
        incoming = base.replace(b"\t</ChildItems>", b'\t\t<!--vendor addition-->\n\t\t<InputField name="Vendor" id="3"/>\n\t</ChildItems>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content.count(b"<!--local addition-->"), 1)
        self.assertEqual(result.content.count(b"<!--vendor addition-->"), 1)
        self.assertEqual(form_items(result.content), ["A", "Vendor", "Mine"])

    def test_comment_edit_is_an_explicit_unsupported_change_not_silent_loss(self):
        base = xml('<!--base comment--><A>old</A>')
        local = base.replace(b"base comment", b"local comment")
        result = merge_xml(base, local, base)
        self.assertFalse(result.resolved)
        self.assertIn("xml.markup_conflict", codes(result))

    def test_namespace_aliases_compare_by_uri_and_keep_incoming_prefix(self):
        base = b'<p:Form xmlns:p="urn:form"><p:A>old</p:A><p:B>old</p:B></p:Form>'
        local = b'<q:Form xmlns:q="urn:form"><q:A>ours</q:A><q:B>old</q:B></q:Form>'
        incoming = b'<p:Form xmlns:p="urn:form"><p:A>old</p:A><p:B>theirs</p:B></p:Form>'
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming.replace(b">old<", b">ours<", 1))

    def test_inserted_namespace_closure_keeps_qname_value_binding(self):
        base = b'<Form xmlns:t="urn:vendor"><Attributes/></Form>'
        local = b'<Form xmlns:t="urn:local"><Attributes><Attribute name="Mine" id="1"><Type>t:Thing</Type></Attribute></Attributes></Form>'
        incoming = b'<Form xmlns:t="urn:vendor"><Attributes><Attribute name="Vendor" id="2"/></Attributes></Form>'
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        source = XMLSource(result.content)
        mine = source.root.children[0].children[-1]
        self.assertEqual(mine.nsmap["t"], "urn:local")
        self.assertEqual(source.root.nsmap["t"], "urn:vendor")

    def test_new_dangling_data_path_blocks_success(self):
        base = xml('<ChildItems/><Attributes><Attribute name="A" id="1"/></Attributes>')
        local = base.replace(b'<ChildItems/>', b'<ChildItems><InputField name="Mine" id="1"><DataPath>A</DataPath></InputField></ChildItems>')
        incoming = xml('<ChildItems/><Attributes/>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.unresolved_data_path", codes(result))

    def test_reapplying_original_delta_is_idempotent_including_id_mapping(self):
        base = xml('<ChildItems/>')
        local = xml('<ChildItems><InputField name="Mine" id="1"/></ChildItems>')
        incoming = xml('<ChildItems><InputField name="Vendor" id="1"/></ChildItems>')
        first = merge_xml(base, local, incoming)
        self.assertResolved(first)
        second = merge_xml(base, local, first.content)
        self.assertResolved(second)
        self.assertEqual(second.content, first.content)

    def test_root_metadata_uuid_and_kind_mismatch_rejected_even_local_noop(self):
        b = b'<MetaDataObject version="2.20"><CommonForm uuid="573f1753-9194-4aaf-9217-5cac70ff108f"><Properties/></CommonForm></MetaDataObject>'
        other_uuid = b.replace(b"573f1753", b"673f1753")
        other_type = b.replace(b"CommonForm", b"Catalog")
        for incoming in (other_uuid, other_type):
            with self.subTest(incoming=incoming):
                result = merge_xml(b, b, incoming)
                self.assertFalse(result.resolved)
                self.assertIn("xml.identity_mismatch", codes(result))

    def test_dump_format_conversion_is_not_silently_merged(self):
        base = xml('<A/>')
        incoming = base.replace(b'2.20', b'2.21')
        result = merge_xml(base, base, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("xml.format_mismatch", codes(result))

    def test_new_attribute_and_its_field_are_merged_together(self):
        base = xml('<ChildItems/><Attributes/>')
        local = xml('<ChildItems><InputField name="Mine" id="1"><DataPath>Mine</DataPath></InputField></ChildItems><Attributes><Attribute name="Mine" id="1"><Type/></Attribute></Attributes>')
        incoming = xml('<ChildItems><InputField name="Vendor" id="1"><DataPath>Vendor</DataPath></InputField></ChildItems><Attributes><Attribute name="Vendor" id="1"><Type/></Attribute></Attributes>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertFalse(any(i.blocking for i in inspect_xml(result.content)))
        self.assertEqual(result.content.count(b'name="Mine" id="2"'), 2)

    def test_independent_column_scopes_remap_only_the_collision(self):
        base = xml('<Attributes><Attribute name="A" id="1"><Columns/></Attribute><Attribute name="B" id="2"><Columns/></Attribute></Attributes>')
        local = base.replace(b'<Columns/>', b'<Columns><Column name="Mine" id="1"/></Columns>')
        incoming = base.replace(b'<Columns/>', b'<Columns><Column name="Vendor" id="1"/></Columns>', 1)
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        attributes = ET.fromstring(result.content).find('Attributes')
        self.assertEqual(attributes[0].find('Columns')[-1].attrib['id'], '2')
        self.assertEqual(attributes[1].find('Columns')[-1].attrib['id'], '1')

    def test_renaming_a_definition_is_not_misread_as_reusing_its_id(self):
        base = xml('<ChildItems><InputField name="Old" id="1"/></ChildItems>')
        local = base.replace(b'Old', b'New')
        result = merge_xml(base, local, base)
        self.assertFalse(result.resolved)
        self.assertIn('xml.rename_unsupported', codes(result))

    def test_moving_existing_item_does_not_lose_incoming_edits(self):
        base = xml('<ChildItems><UsualGroup name="A" id="1"><ChildItems><InputField name="Field" id="3"/></ChildItems></UsualGroup><UsualGroup name="B" id="2"><ChildItems/></UsualGroup></ChildItems>')
        local = xml('<ChildItems><UsualGroup name="A" id="1"><ChildItems/></UsualGroup><UsualGroup name="B" id="2"><ChildItems><InputField name="Field" id="3"/></ChildItems></UsualGroup></ChildItems>')
        incoming = base.replace(b'name="Field" id="3"/>', b'name="Field" id="3"><Width>25</Width></InputField>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.move_unsupported', codes(result))
        self.assertEqual(result.content, incoming)

    def test_new_comment_in_empty_collection_is_not_lost(self):
        base = xml('<ChildItems/>')
        local = xml('<ChildItems>\n <!--mine-->\n <InputField name="Mine" id="1"/>\n</ChildItems>')
        result = merge_xml(base, local, base)
        self.assertResolved(result)
        self.assertEqual(result.content.count(b'<!--mine-->'), 1)

    def test_comment_on_deduplicated_addition_cannot_silently_disappear(self):
        base = xml('<ChildItems/>')
        local = xml('<ChildItems><!--local comment--><InputField name="Same" id="1"/></ChildItems>')
        incoming = xml('<ChildItems><InputField name="Same" id="2"/></ChildItems>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.markup_conflict', codes(result))

    def test_known_qname_value_reuses_target_alias_without_rebinding(self):
        base = xml('<Type>x:Old</Type>\n<B>old</B>', attrs=' xmlns:x="urn:types"')
        local = xml('<Type>x:New</Type>\n<B>old</B>', attrs=' xmlns:x="urn:types"')
        incoming = xml('<Type>t:Old</Type>\n<B>vendor</B>', attrs=' xmlns:t="urn:types" xmlns:x="urn:unrelated"')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, incoming.replace(b't:Old', b't:New'))

    def test_xsi_type_namespace_and_attribute_namespace_are_added_locally(self):
        base = xml('<A/>\n<B>old</B>')
        local = xml('<A xsi:type="t:Thing" t:flag="yes"/>\n<B>old</B>', attrs=' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:t="urn:types"')
        incoming = xml('<A/>\n<B>vendor</B>', attrs=' xmlns:t="urn:other"')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        source = XMLSource(result.content)
        a = source.root.children[0]
        self.assertEqual(a.value('{urn:types}flag'), 'yes')
        self.assertEqual(a.value('{http://www.w3.org/2001/XMLSchema-instance}type'), 't1:Thing')
        self.assertEqual(source.root.nsmap['t'], 'urn:other')

    def test_unprefixed_xsi_qname_tracks_default_namespace_not_only_its_text(self):
        base = b'<p:Form xmlns:p="urn:form" xmlns="urn:old-type" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><p:A xsi:type="Thing"/><p:B>old</p:B></p:Form>'
        local = base.replace(b'xmlns="urn:old-type"', b'xmlns="urn:local-type"')
        incoming = base.replace(b'<p:B>old</p:B>', b'<p:B>vendor</p:B>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        source = XMLSource(result.content)
        a = source.root.children[0]
        value = a.value('{http://www.w3.org/2001/XMLSchema-instance}type')
        prefix, name = value.split(':')
        self.assertEqual((a.nsmap[prefix], name), ('urn:local-type', 'Thing'))
        self.assertEqual(source.root.nsmap[''], 'urn:old-type')

    def test_pending_namespace_alias_cannot_bind_two_distinct_uris(self):
        base = b'<f:Form xmlns:f="urn:form" xmlns="urn:base" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
        local = b'<f:Form xmlns:f="urn:form" xmlns="urn:local-type" xmlns:m1c="urn:attribute" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" m1c:flag="yes" xsi:type="Thing"/>'
        result = merge_xml(base, local, base)
        self.assertResolved(result)
        source = XMLSource(result.content)
        self.assertEqual(source.root.value('{urn:attribute}flag'), 'yes')
        value = source.root.value('{http://www.w3.org/2001/XMLSchema-instance}type')
        prefix, name = value.split(':')
        self.assertEqual((source.root.nsmap[prefix], name), ('urn:local-type', 'Thing'))
        self.assertEqual(source.root.nsmap[''], 'urn:base')
        self.assertEqual(ET.fromstring(result.content).attrib['{urn:attribute}flag'], 'yes')

    def test_unknown_numeric_reference_matches_remapped_id_by_integer_value(self):
        base = b'<Form><ChildItems/></Form>'
        local = b'<Form><ChildItems><InputField name="Mine" id="01"><UnknownReference>1</UnknownReference></InputField></ChildItems></Form>'
        incoming = b'<Form><ChildItems><InputField name="Vendor" id="1"/></ChildItems></Form>'
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.unknown_id_reference', codes(result))
        self.assertEqual(result.content, incoming)

    def test_unknown_numeric_attribute_cannot_hide_reference_with_leading_zero(self):
        base = b'<Form><ChildItems/></Form>'
        incoming = b'<Form><ChildItems><InputField name="Vendor" id="1"/></ChildItems></Form>'
        for value in ('01', '+1', ' 1 '):
            with self.subTest(value=value):
                local = ('<Form><ChildItems><InputField name="Mine" id="1" ref="' + value + '"/></ChildItems></Form>').encode()
                result = merge_xml(base, local, incoming)
                self.assertFalse(result.resolved)
                self.assertIn('xml.unknown_id_reference', codes(result))
                self.assertEqual(result.content, incoming)

    def test_integer_conversion_limits_report_issues_instead_of_crashing(self):
        previous_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            base = b'<Form><ChildItems/></Form>'
            huge = b'9' * 700
            local = b'<Form><ChildItems><InputField name="Mine" id="' + huge + b'"/></ChildItems></Form>'
            result = merge_xml(base, local, base)
            self.assertFalse(result.resolved)
            self.assertIn('xml.invalid_id', codes(result))

            incoming = b'<Form><ChildItems><InputField name="Vendor" id="1"/></ChildItems></Form>'
            local = b'<Form><ChildItems><InputField name="Mine" id="1" ref="' + huge + b'"/></ChildItems></Form>'
            result = merge_xml(base, local, incoming)
            self.assertFalse(result.resolved)
            self.assertIn('xml.unknown_id_reference', codes(result))

            local = b'<Form><ChildItems><InputField name="Mine" id="1"/></ChildItems></Form>'
            incoming = b'<Form><ChildItems><InputField name="Vendor" id="1"/><InputField name="Largest" id="' + b'9' * 640 + b'"/></ChildItems></Form>'
            result = merge_xml(base, local, incoming)
            self.assertFalse(result.resolved)
            self.assertIn('xml.invalid_id', codes(result))
        finally:
            sys.set_int_max_str_digits(previous_limit)

    def test_pending_parent_namespace_is_visible_without_rebinding_child(self):
        base = b'<f:Form xmlns:f="urn:form" xmlns="urn:base" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><f:Properties><f:A/></f:Properties></f:Form>'
        local = b'<f:Form xmlns:f="urn:form" xmlns="urn:base" xmlns:m1c="urn:root-attribute" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" m1c:flag="yes"><f:Properties xmlns="urn:local-type" xsi:type="Thing"><f:A/></f:Properties></f:Form>'
        result = merge_xml(base, local, base)
        self.assertResolved(result)
        source = XMLSource(result.content)
        self.assertEqual(source.root.value('{urn:root-attribute}flag'), 'yes')
        properties = source.root.children[0]
        prefix, name = properties.value('{http://www.w3.org/2001/XMLSchema-instance}type').split(':')
        self.assertEqual((properties.nsmap[prefix], name), ('urn:local-type', 'Thing'))
        self.assertEqual(properties.children[0].tag, '{urn:form}A')

    def test_existing_comment_reorder_is_explicit_instead_of_discarded(self):
        base = b'<Form><!--A--><A>1</A><!--B--><B>1</B></Form>'
        local = b'<Form><!--B--><A>1</A><!--A--><B>1</B></Form>'
        incoming = base.replace(b'<B>1', b'<B>2')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.markup_conflict', codes(result))

    def test_existing_comment_move_preserves_its_anchor_or_requires_review(self):
        base = b'<Form><!--A--><A>1</A><B>1</B></Form>'
        local = b'<Form><A>1</A><!--A--><B>1</B></Form>'
        incoming = base.replace(b'<B>1', b'<B>2')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.markup_conflict', codes(result))

    def test_shared_comment_move_can_keep_incoming_position(self):
        base = b'<Form><!--A--><A>1</A><B>1</B></Form>'
        moved = b'<Form><A>1</A><!--A--><B>1</B></Form>'
        local = moved.replace(b'<A>1', b'<A>2')
        incoming = moved.replace(b'<B>1', b'<B>2')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertEqual(result.content, b'<Form><A>2</A><!--A--><B>2</B></Form>')

    def test_document_level_markup_move_is_not_silently_discarded(self):
        base = b'<!--A--><Form><B>1</B></Form>'
        local = b'<Form><B>1</B></Form><!--A-->'
        incoming = base.replace(b'<B>1', b'<B>2')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.outer_markup', codes(result))

    def test_unbound_qname_value_is_rejected_before_noop(self):
        data = xml('<Type>missing:Thing</Type>')
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn('xml.unbound_qname', codes(result))

    def test_inherited_xml_space_preserve_is_not_reindented(self):
        base = xml('<Mystery xml:space="preserve"><A>one</A>  <B>two</B></Mystery>\n<C>old</C>')
        local = base.replace(b'</A>  <B>', b'</A>    <B>')
        incoming = base.replace(b'<C>old</C>', b'<C>vendor</C>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        self.assertIn(b'</A>    <B>', result.content)

    def test_inserted_subtree_retains_inherited_xml_space_when_parent_changes(self):
        base = xml('<ChildItems xml:space="preserve"/>')
        local = xml('<ChildItems xml:space="preserve"><InputField name="Mine" id="1"><Hint>  text  </Hint></InputField></ChildItems>')
        incoming = xml('<ChildItems xml:space="default"><InputField name="Vendor" id="1"/></ChildItems>')
        result = merge_xml(base, local, incoming)
        self.assertResolved(result)
        source = XMLSource(result.content)
        own = source.root.children[0].children[-1]
        self.assertTrue(own.preserve_space)
        self.assertEqual(own.children[0].text, '  text  ')
        self.assertIn(b'xml:space="default"', result.content)

    def test_missing_metadata_uuid_is_invalid(self):
        data = b'<MetaDataObject><CommonForm><Properties/></CommonForm></MetaDataObject>'
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn('xml.metadata_uuid_missing', codes(result))

    def test_metadata_child_uuid_cannot_change_its_kind(self):
        base = b'<MetaDataObject><Catalog uuid="573f1753-9194-4aaf-9217-5cac70ff108f"><ChildObjects><Attribute uuid="673f1753-9194-4aaf-9217-5cac70ff108f"><Properties><Name>A</Name></Properties></Attribute></ChildObjects></Catalog></MetaDataObject>'
        incoming = base.replace(b'<Attribute ', b'<TabularSection ').replace(b'</Attribute>', b'</TabularSection>')
        result = merge_xml(base, base, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.identity_mismatch', codes(result))

    def test_metadata_child_kind_is_part_of_collection_key(self):
        data = b'<MetaDataObject><Catalog uuid="573f1753-9194-4aaf-9217-5cac70ff108f"><ChildObjects><Attribute uuid="673f1753-9194-4aaf-9217-5cac70ff108f"><Properties><Name>A</Name></Properties></Attribute><Command uuid="773f1753-9194-4aaf-9217-5cac70ff108f"><Properties><Name>A</Name></Properties></Command></ChildObjects></Catalog></MetaDataObject>'
        self.assertResolved(merge_xml(data, data, data))

    def test_duplicate_metadata_uuid_is_rejected(self):
        data = b'<MetaDataObject><Catalog uuid="573f1753-9194-4aaf-9217-5cac70ff108f"><ChildObjects><Attribute uuid="673f1753-9194-4aaf-9217-5cac70ff108f"><Properties><Name>A</Name></Properties></Attribute><Attribute uuid="673f1753-9194-4aaf-9217-5cac70ff108f"><Properties><Name>B</Name></Properties></Attribute></ChildObjects></Catalog></MetaDataObject>'
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn('xml.duplicate_uuid', codes(result))

    def test_excessive_nesting_is_an_explicit_error_not_a_python_crash(self):
        data = b'<A>' * 300 + b'</A>' * 300
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn('xml.invalid_input', codes(result))

    def test_missing_known_definition_id_is_invalid(self):
        data = xml('<ChildItems><InputField name="A"/></ChildItems>')
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn('xml.missing_id', codes(result))

    def test_duplicate_event_name_is_invalid_even_without_numeric_ids(self):
        data = xml('<Events><Event name="OnOpen">One</Event><Event name="OnOpen">Two</Event></Events>')
        result = merge_xml(data, data, data)
        self.assertFalse(result.resolved)
        self.assertIn('xml.duplicate_collection_key', codes(result))

    def test_reserved_root_command_bar_is_not_renumbered(self):
        base = xml('<ChildItems/>')
        local = xml('<AutoCommandBar name="Mine" id="-1"/><ChildItems/>')
        incoming = xml('<AutoCommandBar name="Vendor" id="-1"/><ChildItems/>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.reserved_id_collision', codes(result))
        self.assertEqual(result.content, incoming)

    def test_new_unknown_item_without_id_is_not_silently_accepted(self):
        base = xml('<ChildItems/>')
        local = xml('<ChildItems><FutureWidget name="Mine"/></ChildItems>')
        result = merge_xml(base, local, base)
        self.assertFalse(result.resolved)
        self.assertIn('xml.unsupported_item_type', codes(result))

    def test_unknown_ids_are_preserved_when_unchanged_but_new_ones_block(self):
        base = xml('<Mystery id="1"><A>old</A></Mystery><B>old</B>')
        incoming = base.replace(b'<B>old</B>', b'<B>vendor</B>')
        self.assertResolved(merge_xml(base, base, incoming))
        local = base.replace(b'<A>old</A>', b'<A>ours</A>')
        result = merge_xml(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn('xml.unknown_id_scope', codes(result))

    def test_invalid_utf8_xml_dtd_declared_entities_and_incomplete_input(self):
        valid = xml('<A/>')
        invalid = [b'<Form>\xff</Form>', b'<Form><A></Form>', b'<Form>',
                   b'<!DOCTYPE Form [<!ENTITY mine "x">]><Form>&mine;</Form>',
                   b'<!DOCTYPE Form SYSTEM "file:///should-never-be-read"><Form/>',
                   b'<?xml version="1.0" encoding="ISO-8859-1"?><Form/>',
                   b'<x:Form/>', '<Form/>'.encode('utf-16')]
        for content in invalid:
            with self.subTest(content=content):
                result = merge_xml(content, content, valid)
                self.assertFalse(result.resolved)
                self.assertIn("xml.invalid_input", codes(result))
                self.assertEqual(result.content, valid)


if __name__ == "__main__":
    unittest.main()
