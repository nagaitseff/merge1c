"""Configuration policy, local bindings and frozen snapshots."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest

from merge1c.config import Config, ConfigError, Tool, config_from_snapshot, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge1c-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def read(self, project="", local=None, *, profile="vendor_update"):
        project_path = self.root / "merge1c.toml"
        project_path.write_text(project, encoding="utf-8")
        local_path = None
        if local is not None:
            local_path = self.root / "merge1c.local.toml"
            local_path.write_text(local, encoding="utf-8")
        return load_config(project_path, local_path=local_path, profile=profile)

    def test_defaults_and_builtin_profiles(self):
        vendor = load_config()
        self.assertEqual(1, vendor.schema_version)
        self.assertEqual("vendor", vendor.incoming_role)
        self.assertEqual("8.3.27.2342", vendor.platform_version)
        self.assertTrue(vendor.require_platform)
        self.assertEqual("master", load_config(profile="develop_sync").incoming_role)

    def test_profile_overrides_project_and_arrays_replace(self):
        config = self.read('''schema_version = 1
[project]
source_roots = ["src/cf", "extensions"]
platform_version = "8.3.27.2342"
[profiles.maintenance]
incoming_role = "vendor"
[profiles.maintenance.project]
source_roots = ["config"]
''', profile="maintenance")
        self.assertEqual(("config",), config.source_roots)
        self.assertEqual("maintenance", config.profile)

    def test_local_bindings_preserve_project_tool_policy(self):
        config = self.read('''[resolution]
manual_tool = "compare"
[tools.compare]
adapter = "command"
kind = "resolver"
args = ["{base}", "{local}", "{incoming}", "{result}"]
timeout = 20
''', '''[git]
executable = 'C:\\Portable Git\\bin\\git.exe'
[tools.compare]
executable = 'C:\\Compare tools\\compare.exe'
''')
        self.assertEqual(r"C:\Portable Git\bin\git.exe", config.git_executable)
        self.assertEqual(r"C:\Compare tools\compare.exe", config.tools["compare"].executable)
        self.assertEqual(20, config.tools["compare"].timeout)
        self.assertEqual(("{base}", "{local}", "{incoming}", "{result}"), config.tools["compare"].args)

    def test_local_file_cannot_change_semantic_policy(self):
        invalid = [
            '[validation]\nrequire_platform = false\n',
            '[git]\nrename_detection = "similarity"\n',
            '[tools.check]\ntimeout = 1\n',
            '[tools.check]\nargs = ["evil"]\n',
        ]
        for local in invalid:
            with self.subTest(local=local), self.assertRaises(ConfigError):
                self.read('[tools.check]\nkind = "validator"\n', local)

    def test_local_binding_cannot_create_undeclared_tool(self):
        with self.assertRaises(ConfigError):
            self.read("", '[tools.undeclared]\nexecutable = "elsewhere.exe"\n')

    def test_unknown_keys_and_table_types_fail_early(self):
        invalid = [
            'schema_verison = 1\n',
            '[project]\nunknown = true\n',
            'project = "not a table"\n',
            '[tools.check]\ntiemout = 1\n',
            '[profiles.vendor_update]\nunknown = true\n',
        ]
        for project in invalid:
            with self.subTest(project=project), self.assertRaises(ConfigError):
                self.read(project)

    def test_strict_types(self):
        invalid = [
            'schema_version = true\n',
            '[project]\nsource_roots = "src"\n',
            '[validation]\nrequire_platform = "yes"\n',
            '[tools.check]\ntimeout = true\n',
            '[tools.check]\ntimeout = 0\n',
            '[tools.check]\nargs = [1]\n',
            '[tools.check]\nreport_required = 1\n',
            '[profiles.vendor_update]\nincoming_role = []\n',
        ]
        for project in invalid:
            with self.subTest(project=project), self.assertRaises(ConfigError):
                self.read(project)

    def test_source_roots_cannot_escape_project(self):
        for root in ("../outside", "src/../../outside", "/absolute", "C:/absolute", "C:relative", "//server/share", "src/*"):
            with self.subTest(root=root), self.assertRaises(ConfigError):
                self.read('[project]\nsource_roots = [' + json.dumps(root) + ']\n')
        self.assertEqual(("src/cf",), self.read("[project]\nsource_roots = ['src\\cf/']\n").source_roots)

    def test_fixed_invariants_cannot_be_disabled(self):
        invalid = [
            '[git]\nrename_detection = "git"\n',
            '[resolution]\nambiguous = "take_local"\n',
            '[xml.forms]\npreserve_source = false\n',
            '[xml.forms]\nid_policy = "regenerate"\n',
            '[bsl]\nmode = "regex"\n',
            '[bsl]\npreserve_source = false\n',
        ]
        for project in invalid:
            with self.subTest(project=project), self.assertRaises(ConfigError):
                self.read(project)

    def test_references_to_tools_are_kind_checked(self):
        invalid = [
            '[validation]\nrequired_tools = ["missing"]\n',
            '[resolution]\nmanual_tool = "missing"\n',
            '[validation]\nrequired_tools = ["x"]\n[tools.x]\nkind = "resolver"\n',
            '[resolution]\nmanual_tool = "x"\n[tools.x]\nkind = "validator"\n',
        ]
        for project in invalid:
            with self.subTest(project=project), self.assertRaises(ConfigError):
                self.read(project)

    def test_placeholders_are_literal_paths_without_formatting_or_access(self):
        invalid_args = ("{unknown}", "{base.__class__}", "{base[0]}", "{}", "{base!r}", "{base:>20}", "{base:{local}}", "{base")
        for argument in invalid_args:
            with self.subTest(argument=argument), self.assertRaises(ConfigError):
                self.read('[tools.check]\nargs = [' + json.dumps(argument) + ']\n')
        self.assertEqual(("{{literal}}", "--source={candidate}"), self.read(
            '[tools.check]\nargs = ["{{literal}}", "--source={candidate}"]\n').tools["check"].args)

    def test_fingerprint_is_stable_across_json_roundtrip_and_file_edits(self):
        config = self.read('[tools.check]\nargs = ["{candidate}"]\n')
        original = config.fingerprint
        snapshot = json.loads(json.dumps(config.to_dict()))
        restored = config_from_snapshot(snapshot)
        self.assertEqual(original, restored.fingerprint)
        (self.root / "merge1c.toml").write_text('[project]\nplatform_version = "changed"\n', encoding="utf-8")
        self.assertEqual(original, config.fingerprint)
        snapshot["platform_version"] = "elsewhere"
        self.assertNotEqual(original, config_from_snapshot(snapshot).fingerprint)

    def test_effective_config_is_immutable_including_tool_mapping(self):
        config = self.read('[tools.check]\ntimeout = 10\n')
        with self.assertRaises(FrozenInstanceError):
            config.platform_version = "changed"
        with self.assertRaises((TypeError, AttributeError)):
            config.tools["check"] = Tool(timeout=1)

    def test_schema_and_git_minimum_versions(self):
        for project in ('schema_version = 2\n', '[git]\nminimum_version = "2.49.1"\n', '[git]\nminimum_version = "two"\n'):
            with self.subTest(project=project), self.assertRaises(ConfigError):
                self.read(project)
        self.assertEqual("2.50.0", self.read('[git]\nminimum_version = "2.50.0"\n').git_minimum)

    def test_unselected_profiles_are_checked_for_schema_and_roles(self):
        invalid = (
            '[profiles.later]\nunknown = true\n',
            '[profiles.later]\nincoming_role = "typo"\n',
            '[profiles.later]\nincoming_role = []\n',
            'profiles.later = "not a table"\n',
            '[profiles.later]\nschema_version = 1\n',
            '[profiles.later.profiles.nested]\nincoming_role = "vendor"\n',
            '[profiles.later.project]\nunknown = true\n',
            '[profiles.later.project]\nsource_roots = "src"\n',
            '[profiles.later.validation]\nrequire_platform = 1\n',
            '[profiles.later.tools.check]\ntimeout = true\n',
            '[profiles.later.tools.check]\nunknown = true\n',
            '[profiles.later.tools.check]\nargs = ["{unknown}"]\n',
        )
        for project in invalid:
            with self.subTest(project=project), self.assertRaises(ConfigError):
                self.read(project)

    def test_unselected_profile_tool_references_are_validated_only_when_applied(self):
        project = '[profiles.later.validation]\nrequired_tools = ["later_check"]\n'
        self.assertEqual((), self.read(project).required_validators)
        with self.assertRaises(ConfigError):
            self.read(project, profile="later")

    def test_direct_tool_api_rejects_invalid_types_and_templates(self):
        invalid = (
            {"adapter": []}, {"adapter": "unknown"}, {"kind": "unknown"},
            {"executable": Path("program")}, {"executable": "bad\x00name"},
            {"args": "--one-argument"}, {"args": [1]}, {"args": None},
            {"args": ["{unknown}"]}, {"args": ["{base!r}"]}, {"args": ["bad\x00arg"]},
            {"timeout": True}, {"timeout": 0}, {"timeout": 1.5},
            {"report_required": 1},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ConfigError):
                Tool(**values)
        self.assertEqual(("{project}/check.py",), Tool(args=["{project}/check.py"]).args)
        self.assertEqual("validator", Tool(adapter="beyond_compare").kind)
        self.assertFalse(Tool(adapter="platform").report_required)

    def test_direct_config_api_rejects_invalid_types_and_tool_references(self):
        invalid = (
            {"schema_version": True}, {"schema_version": 2},
            {"profile": None}, {"incoming_role": []}, {"incoming_role": "unknown"},
            {"platform_version": 1}, {"project_root": Path(".")},
            {"git_executable": []}, {"git_minimum": "2.49.0"}, {"git_minimum": "3.-1"},
            {"source_roots": "src"}, {"source_roots": [1]}, {"source_roots": ["../outside"]},
            {"required_validators": "check"}, {"required_validators": ["missing"]},
            {"require_platform": 1}, {"tools": []}, {"tools": {"check": {}}},
            {"tools": {1: Tool()}}, {"manual_tool": "missing"},
            {"required_validators": ["check"], "tools": {"check": Tool(kind="resolver")}},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ConfigError):
                Config(**values)
        config = Config(source_roots=["src\\cf/", "src/cf"], tools={"check": Tool()}, required_validators=["check"])
        self.assertEqual(("src/cf",), config.source_roots)
        self.assertEqual(("check",), config.required_validators)


if __name__ == "__main__":
    unittest.main()
