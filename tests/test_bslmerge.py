"""Behavioural tests for the bounded BSL source merger."""

import unittest

from merge1c.bslmerge import inspect_bsl, merge_bsl, method_names


def module(*methods, prefix="", suffix=""):
    return (prefix + "\n".join(methods) + suffix).encode("utf-8")


def procedure(name, body="    X = 1;", *, signature="", documentation=""):
    return (documentation + f"Процедура {name}({signature})\n" + body
            + "\nКонецПроцедуры\n")


class BslInspectionTests(unittest.TestCase):
    def test_keywords_in_strings_comments_and_members_are_not_declarations(self):
        text = procedure("A", '''    // Процедура Fake() КонецПроцедуры
    S = "Процедура Fake()";
    Q = "ВЫБРАТЬ
       |КонецПроцедуры
       |Функция Another()";
    S = "quote ""Процедура""";
    Объект.Процедура();''')
        self.assertEqual([], inspect_bsl(text.encode("utf-8")))
        self.assertEqual({"a"}, method_names(text.encode("utf-8")))

    def test_duplicate_methods_are_local_case_insensitive(self):
        text = module(procedure("Тест"), procedure("тЕСТ"))
        codes = {item.code for item in inspect_bsl(text)}
        self.assertIn("BSL_DUPLICATE_METHOD", codes)
        self.assertEqual(set(), method_names(text))
        self.assertEqual([], inspect_bsl(module(procedure("Тест"))))

    def test_exclusive_conditional_method_declarations_are_not_duplicates(self):
        text = module(procedure("Тест"), prefix="#Если Клиент Тогда\n",
                      suffix="#Иначе\n" + procedure("Тест") + "#КонецЕсли\n")
        self.assertEqual([], inspect_bsl(text))
        self.assertEqual({"тест"}, method_names(text))

    def test_duplicate_variables_and_parameters_use_their_scope(self):
        separate_scopes = module(procedure("A", "    Перем X;"), procedure("B", "    Перем X;"))
        self.assertEqual([], inspect_bsl(separate_scopes))
        duplicate = module(procedure("A", "    Перем X;\n    Перем x;"))
        self.assertIn("BSL_DUPLICATE_VARIABLE", {item.code for item in inspect_bsl(duplicate)})
        parameters = module(procedure("A", signature="Знач X, x = 1"))
        self.assertIn("BSL_DUPLICATE_PARAMETER", {item.code for item in inspect_bsl(parameters)})

    def test_exclusive_variable_branches_are_not_duplicates(self):
        data = ("#Если Клиент Тогда\nПерем X;\n#Иначе\nПерем x;\n#КонецЕсли\n"
                + procedure("A")).encode("utf-8")
        self.assertEqual([], inspect_bsl(data))

    def test_multiline_variable_declarations_are_indexed(self):
        data = ("Перем A,\n    a;\n" + procedure("P")).encode("utf-8")
        self.assertIn("BSL_DUPLICATE_VARIABLE", {item.code for item in inspect_bsl(data)})

    def test_cr_only_comments_do_not_hide_real_declarations(self):
        data = module(procedure("A"), procedure("B"), prefix="// Header\n\n").replace(b"\n", b"\r")
        self.assertEqual([], inspect_bsl(data))
        self.assertEqual({"a", "b"}, method_names(data))

    def test_unbalanced_structures_and_encoding_are_reported(self):
        cases = [
            (b"\xff", "BSL_ENCODING"),
            ("Процедура A()\n".encode("utf-8"), "BSL_METHOD_BALANCE"),
            ("#Если Клиент Тогда\n".encode("utf-8"), "BSL_PREPROCESSOR_BALANCE"),
            ("#Область A\n".encode("utf-8"), "BSL_REGION_BALANCE"),
            (module(procedure("A", '    S = "never closed;')), "BSL_UNCLOSED_STRING"),
            (module(procedure("A", "    X = F(1;")), "BSL_DELIMITER_BALANCE"),
            (module(procedure("A", "    X = `bad`;")), "BSL_UNKNOWN_CHARACTER"),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, {item.code for item in inspect_bsl(data, path="m.bsl")})


class BslMergeTests(unittest.TestCase):
    def assertResolved(self, result):
        self.assertTrue(result.resolved, [(issue.code, issue.message) for issue in result.issues])
        return result.content.decode("utf-8-sig")

    def test_noop_and_bom_only_change_preserve_incoming_bytes(self):
        base = module(procedure("A"))
        incoming = b"\xef\xbb\xbf" + module(procedure("A", "    X = 2;"))
        self.assertEqual(incoming, merge_bsl(base, b"\xef\xbb\xbf" + base, incoming).content)
        self.assertEqual(incoming, merge_bsl(base, incoming, incoming).content)

    def test_unrelated_method_changes_are_combined(self):
        base = module(procedure("A"), procedure("B"))
        local = module(procedure("A", "    X = 2;"), procedure("B"))
        incoming = module(procedure("A"), procedure("B", "    X = 3;"))
        result = merge_bsl(base, local, incoming)
        expected = module(procedure("A", "    X = 2;"), procedure("B", "    X = 3;"))
        self.assertEqual(expected, result.content)
        self.assertResolved(result)

    def test_same_method_changes_require_manual_resolution(self):
        base = module(procedure("A"))
        result = merge_bsl(base, module(procedure("A", "    X = 2;")),
                           module(procedure("A", "    X = 3;")), path="A.bsl")
        self.assertFalse(result.resolved)
        self.assertIsNone(result.content)
        self.assertIn("BSL_METHOD_CONFLICT", {issue.code for issue in result.issues})
        self.assertTrue(all(issue.path == "A.bsl" for issue in result.issues))

    def test_signature_change_is_the_same_method(self):
        base = module(procedure("A", signature="X"), procedure("B"))
        local = module(procedure("A", signature="Знач X, Y = 0"), procedure("B"))
        incoming = module(procedure("A", signature="X"), procedure("B", "    X = 2;"))
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertEqual(1, text.count("Процедура A("))
        self.assertIn("A(Знач X, Y = 0)", text)

    def test_local_addition_follows_incoming_additions_at_same_anchor(self):
        base = module(procedure("A"), procedure("B"))
        local = module(procedure("A"), procedure("Local"), procedure("Local2"), procedure("B"))
        incoming = module(procedure("A"), procedure("Vendor"), procedure("B"))
        result = merge_bsl(base, local, incoming)
        text = self.assertResolved(result)
        self.assertLess(text.index("Vendor("), text.index("Local("))
        self.assertLess(text.index("Local("), text.index("Local2("))
        self.assertLess(text.index("Local2("), text.index("B("))
        self.assertEqual({"a", "b", "vendor", "local", "local2"}, method_names(result.content))

    def test_identical_addition_is_not_duplicated(self):
        base = module(procedure("A"), procedure("B"))
        local = module(procedure("A", "    X = 2;"), procedure("Added"), procedure("B"))
        incoming = module(procedure("A"), procedure("Added"), procedure("B", "    X = 3;"))
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertEqual(1, text.count("Процедура Added("))

    def test_different_same_name_additions_conflict(self):
        base = module(procedure("A"))
        local = module(procedure("A"), procedure("Added", "    X = 2;"))
        incoming = module(procedure("A"), procedure("Added", "    X = 3;"))
        result = merge_bsl(base, local, incoming)
        self.assertIn("BSL_ADD_ADD", {issue.code for issue in result.issues})
        self.assertFalse(result.resolved)

    def test_incoming_move_combines_with_local_edit_and_documentation(self):
        a = procedure("A", documentation="// Documentation for A.\n&НаКлиенте\n")
        changed_a = a.replace("X = 1", "X = 2")
        base = module(a, procedure("B"), prefix="// Header\n\n#Область API\n", suffix="#КонецОбласти\n")
        local = module(changed_a, procedure("B"), prefix="// Header\n\n#Область API\n", suffix="#КонецОбласти\n")
        incoming = module(procedure("B"), a, prefix="// Updated header\n\n#Область API\n", suffix="#КонецОбласти\n")
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertTrue(text.startswith("// Updated header\n\n#Область API\n"))
        self.assertLess(text.index("B("), text.index("A("))
        self.assertIn(changed_a, text)
        self.assertEqual(1, text.count("// Documentation for A."))

    def test_local_region_and_order_move_is_not_silently_discarded(self):
        base = module(procedure("A"), prefix="#Область First\n",
                      suffix="#КонецОбласти\n#Область Second\n" + procedure("B") + "#КонецОбласти\n")
        local = module(procedure("B"), prefix="#Область First\n",
                       suffix="#КонецОбласти\n#Область Second\n" + procedure("A") + "#КонецОбласти\n")
        incoming = base.replace(b"X = 1", b"X = 2", 1)
        result = merge_bsl(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.content)
        self.assertIn("BSL_PLACEMENT_CONFLICT", {issue.code for issue in result.issues})

    def test_local_region_move_with_incoming_addition_requires_source_anchors(self):
        base = module(procedure("A"), prefix="#Область First\n",
                      suffix="#КонецОбласти\n#Область Second\n" + procedure("B") + "#КонецОбласти\n")
        local = module(procedure("A"), procedure("B"),
                       prefix="#Область First\n#КонецОбласти\n#Область Second\n", suffix="#КонецОбласти\n")
        incoming = module(procedure("A"), procedure("Added"), prefix="#Область First\n",
                          suffix="#КонецОбласти\n#Область Second\n" + procedure("B") + "#КонецОбласти\n")
        result = merge_bsl(base, local, incoming)
        self.assertFalse(result.resolved)
        self.assertIn("BSL_PLACEMENT_CONFLICT", {issue.code for issue in result.issues})

    def test_incoming_region_and_order_move_keeps_local_method_edit(self):
        base = module(procedure("A"), prefix="#Область First\n",
                      suffix="#КонецОбласти\n#Область Second\n" + procedure("B") + "#КонецОбласти\n")
        local = base.replace(b"X = 1", b"X = 2", 1)
        incoming = module(procedure("B"), prefix="#Область First\n",
                          suffix="#КонецОбласти\n#Область Second\n" + procedure("A") + "#КонецОбласти\n")
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertLess(text.index("B("), text.index("#Область Second"))
        self.assertGreater(text.index("A("), text.index("#Область Second"))
        self.assertIn(procedure("A", "    X = 2;"), text)

    def test_shared_region_and_order_move_keeps_independent_body_edits(self):
        base = module(procedure("A"), prefix="#Область First\n",
                      suffix="#КонецОбласти\n#Область Second\n" + procedure("B") + "#КонецОбласти\n")
        moved = module(procedure("B"), prefix="#Область First\n",
                       suffix="#КонецОбласти\n#Область Second\n" + procedure("A") + "#КонецОбласти\n")
        local = moved.replace(b"X = 1", b"X = 2", 1)
        incoming = module(procedure("B"), prefix="#Область First\n",
                          suffix="#КонецОбласти\n#Область Second\n" + procedure("A", "    X = 3;") + "#КонецОбласти\n")
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertIn(procedure("B", "    X = 2;"), text)
        self.assertIn(procedure("A", "    X = 3;"), text)

    def test_addition_stays_in_its_region(self):
        prefix = "// Header\n\n#Область API\n"
        suffix = "#КонецОбласти\n\n#Область Internal\n" + procedure("C") + "#КонецОбласти\n"
        base = module(procedure("A"), prefix=prefix, suffix=suffix)
        local = module(procedure("A"), procedure("Local"), prefix=prefix, suffix=suffix)
        incoming = module(procedure("A", "    X = 2;"), procedure("Vendor"), prefix=prefix, suffix=suffix)
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertLess(text.index("Vendor("), text.index("Local("))
        self.assertLess(text.index("Local("), text.index("#КонецОбласти"))
        self.assertTrue(text.endswith(suffix))

    def test_module_variables_and_initialization_preserved_and_merged(self):
        prefix = "Перем Cache;\n\n"
        suffix = "\nCache = 1;\n"
        base = module(procedure("A"), prefix=prefix, suffix=suffix)
        local = module(procedure("A"), prefix=prefix, suffix="\nCache = 2;\n")
        incoming = module(procedure("A", "    X = 3;"), prefix=prefix, suffix=suffix)
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertTrue(text.startswith(prefix))
        self.assertTrue(text.endswith("\nCache = 2;\n"))
        self.assertIn("X = 3", text)

    def test_conditional_declarations_fail_closed_for_concurrent_edits(self):
        prefix = "#Если Клиент Тогда\n"
        suffix = "#КонецЕсли\n"
        base = module(procedure("A"), procedure("B"), prefix=prefix, suffix=suffix)
        local = base.replace(b"X = 1", b"X = 2", 1)
        incoming = base.replace(b"X = 1", b"X = 3")
        result = merge_bsl(base, local, incoming)
        self.assertIn("BSL_CONDITIONAL_STRUCTURE", {issue.code for issue in result.issues})
        self.assertFalse(result.resolved)
        self.assertEqual(incoming, merge_bsl(base, base, incoming).content)

    def test_preprocessor_inside_methods_is_preserved(self):
        body = "#Если Клиент Тогда\n    X = 1;\n#Иначе\n    X = 2;\n#КонецЕсли"
        base = module(procedure("A", body), procedure("B"))
        local = module(procedure("A", body.replace("X = 1", "X = 9")), procedure("B"))
        incoming = module(procedure("A", body), procedure("B", "    X = 3;"))
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertIn(body.replace("X = 1", "X = 9"), text)

    def test_delete_vs_edit_conflicts(self):
        base = module(procedure("A"), procedure("B"))
        local = module(procedure("B"))
        incoming = module(procedure("A", "    X = 2;"), procedure("B"))
        result = merge_bsl(base, local, incoming)
        self.assertIn("BSL_DELETE_EDIT", {issue.code for issue in result.issues})
        self.assertIsNone(result.content)

    def test_delete_unchanged_method_preserves_other_incoming_change(self):
        base = module(procedure("A"), procedure("B"))
        local = module(procedure("B"))
        incoming = module(procedure("A"), procedure("B", "    X = 2;"))
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertNotIn("Процедура A(", text)
        self.assertIn("X = 2", text)

    def test_crlf_and_bom_are_not_reformatted(self):
        base = module(procedure("A"), procedure("B")).replace(b"\n", b"\r\n")
        local = base.replace(b"X = 1", b"X = 2", 1)
        incoming = b"\xef\xbb\xbf" + base
        # Change only the second method in the incoming source.
        split = incoming.rfind(b"X = 1")
        incoming = incoming[:split] + incoming[split:].replace(b"X = 1", b"X = 3", 1)
        result = merge_bsl(base, local, incoming)
        self.assertResolved(result)
        self.assertTrue(result.content.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", result.content.replace(b"\r\n", b""))

    def test_eof_method_move_keeps_valid_boundary_and_local_edit(self):
        a = procedure("A").rstrip("\n")
        base = module(procedure("B"), a)
        local = module(procedure("B"), a.replace("X = 1", "X = 2"))
        incoming = module(procedure("A"), procedure("B"))
        text = self.assertResolved(merge_bsl(base, local, incoming))
        self.assertIn("X = 2;\nКонецПроцедуры\n", text)
        self.assertLess(text.index("A("), text.index("B("))

    def test_repeated_merge_does_not_duplicate_local_addition(self):
        base = module(procedure("A"), procedure("B"))
        local = module(procedure("A"), procedure("B"), procedure("Local").rstrip("\n"))
        incoming = module(procedure("A", "    X = 2;"), procedure("B"))
        first = merge_bsl(base, local, incoming)
        self.assertResolved(first)
        second = merge_bsl(base, local, first.content)
        self.assertResolved(second)
        self.assertEqual(first.content, second.content)


if __name__ == "__main__":
    unittest.main()
