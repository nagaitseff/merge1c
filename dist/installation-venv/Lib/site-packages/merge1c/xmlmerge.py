"""Conservative, source-preserving three-way merge for Designer XML.

The incoming document is the output substrate.  Only accepted local changes
produce byte patches; untouched source is never serialized.  Unsupported or
ambiguous changes are reported as issues, with an ordinary XML candidate and
no conflict markers.  This is structural checking, not a 1C platform compiler.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
from uuid import UUID

from .models import Change, Issue, MergeResult
from .xmlsource import Attribute, Node, XMLNS_NS, XMLSource, XMLSourceError, local_name


COLLECTIONS = {"Attributes", "Commands", "ChildItems", "Columns", "Events", "ChildObjects"}
UI_TYPES = {
    "AutoCommandBar", "Button", "ButtonGroup", "CalendarField", "ChartField",
    "CheckBoxField", "ColumnGroup", "CommandBar", "ContextMenu", "ExtendedTooltip",
    "FormattedDocumentField", "GanttChartField", "GraphicalSchemaField",
    "HTMLDocumentField", "InputField", "LabelDecoration", "Page", "Pages",
    "PeriodField", "PictureDecoration", "PictureField", "PlannerField",
    "ProgressBarField", "RadioButtonField", "SearchControlAddition",
    "SearchStringAddition", "SpreadsheetDocumentField", "Table", "TextDocumentField",
    "TrackBarField", "UsualGroup", "ViewStatusAddition",
}
PROPERTY_CONTAINERS = {
    "Properties", "Type", "NumberQualifiers", "StringQualifiers", "DateQualifiers",
    "BinaryDataQualifiers", "Title", "Synonym", "ToolTip", "InputHint", "Picture",
    "Font", "Border", "Parameters", "Parameter", "Attribute", "Column", "Command",
    "Event", "Form", "MetaDataObject",
} | UI_TYPES | COLLECTIONS
# These are scalar values, not references. Unknown numeric positions are never
# rewritten when an ID moves: they make that remapping unsupported instead.
NUMERIC_SCALARS = {
    "Width", "Height", "MinWidth", "MaxWidth", "MinHeight", "MaxHeight",
    "Digits", "FractionDigits", "Length", "Scale", "FontSize", "Size",
    "HorizontalSpacing", "VerticalSpacing", "LeftMargin", "RightMargin",
    "TopMargin", "BottomMargin", "VerticalScrollBar", "HorizontalScrollBar",
    "HeightInTableRows", "WidthInChars", "MaxTextLength", "ChoiceHistoryOnInput",
}
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
CORE_TYPE = "{http://v8.1c.ru/8.1/data/core}Type"
_INTEGER = re.compile(r"-?\d+\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class _Patch:
    start: int
    end: int
    content: bytes


@dataclass(slots=True)
class _Definition:
    node: Node
    attribute: Attribute
    scope: tuple[str, ...]
    identity: str


def _escape(value: str, quote: bytes = b'"') -> bytes:
    value = value.replace("&", "&amp;").replace("<", "&lt;")
    value = value.replace('"', "&quot;") if quote == b'"' else value.replace("'", "&apos;")
    return value.encode("utf-8")


def _attributes(node: Node) -> dict[str, Attribute]:
    return {a.name: a for a in node.attributes if not a.name.startswith("{" + XMLNS_NS + "}")}


def _qvalue(value: str, node: Node, *, unprefixed=False):
    """Resolve a value known by its schema position to be a QName."""
    if unprefixed:
        value = value.strip()
    if ":" in value:
        prefix, name = value.split(":", 1)
        if prefix in node.nsmap:
            return (node.nsmap[prefix], name)
    if unprefixed and re.fullmatch(r"[\w.-]+", value):
        return (node.nsmap.get("", ""), value)
    return value


def _preserves_space(node):
    return node.preserve_space


def _fingerprint(node: Node, *, ignore_ids=False, projections=None, cache=None):
    identity = id(node)
    if cache is not None and identity in cache:
        return cache[identity]
    attrs = []
    for name, attr in _attributes(node).items():
        if ignore_ids and name == "id" and _id_scope(node) is not None:
            continue
        value = projections.get(attr.value_start, attr.value) if projections else attr.value
        attrs.append((name, _qvalue(value, node, unprefixed=True) if name == XSI_TYPE else value))
    text = node.text
    if node.children and not text.strip() and not _preserves_space(node):
        text = ""
    if not node.children and node.name == "Type":
        text = _qvalue(text, node, unprefixed=node.tag == CORE_TYPE)
    # Comment text is content. Pure indentation, quote style and namespace
    # declaration placement are lexical details owned by the incoming source.
    marks = tuple((m.kind, m.value, m.before_child) for m in node.markup)
    result = (node.tag, tuple(sorted(attrs)), text, marks,
              tuple(_fingerprint(c, ignore_ids=ignore_ids, projections=projections, cache=cache) for c in node.children))
    if cache is not None:
        cache[identity] = result
    return result


def _id_scope(node: Node) -> tuple[str, ...] | None:
    if node.source.root.name != "Form":
        return None
    if node.name == "Attribute" and node.parent and node.parent.name == "Attributes" and node.parent.parent is node.source.root:
        return ("attributes",)
    if node.name == "Command" and node.parent and node.parent.name == "Commands" and node.parent.parent is node.source.root:
        return ("commands",)
    if node.name == "Column" and node.parent and node.parent.name == "Columns":
        owner = node.parent.parent
        if owner and owner.name == "Attribute" and _id_scope(owner) == ("attributes",):
            return ("columns", owner.value("name").casefold())
    if node.name not in UI_TYPES:
        return None
    cursor = node
    while cursor.parent is not None:
        if cursor.name not in UI_TYPES and cursor.name != "ChildItems":
            return None
        if cursor.parent is node.source.root:
            return ("items",) if cursor.name in {"ChildItems", "AutoCommandBar", "ContextMenu"} else None
        cursor = cursor.parent
    return None


def _definitions(source: XMLSource):
    for node in source.nodes:
        attr = node.attribute("id")
        if attr is None:
            continue
        scope = _id_scope(node)
        if attr and scope is not None:
            yield _Definition(node, attr, scope, node.value("name").casefold())


def _parents(node):
    node = node.parent
    while node is not None:
        yield node
        node = node.parent


def _child_key(node: Node, parent: Node):
    if parent.name in COLLECTIONS:
        if node.value("name"):
            return ("name", node.value("name").casefold())
        if parent.name == "ChildObjects":
            properties = next((c for c in node.children if c.name == "Properties"), None)
            name = next((c.text for c in properties.children if c.name == "Name"), "") if properties else ""
            if name:
                return ("metadata", node.tag, name.casefold())
            if not node.children and node.text.strip():
                return ("link", node.tag, node.text.strip().casefold())
        return None
    return ("property", node.tag)


def _identity(source: XMLSource):
    root = source.root
    if root.name == "MetaDataObject":
        if len(root.children) != 1:
            return None
        root = root.children[0]
    if root.attribute("uuid"):
        return root.tag, root.value("uuid").casefold()
    return root.tag, None


def _metadata_uuids(source: XMLSource):
    if source.root.name != "MetaDataObject":
        return {}
    namespace = source.root.tag.split("}", 1)[0] + "}" if source.root.tag.startswith("{") else ""
    return {node.value("uuid").casefold(): node.tag for node in source.nodes
            if node.attribute("uuid") and (node.tag.startswith(namespace) if namespace else not node.tag.startswith("{"))}


def _definition_details(definition: _Definition):
    return {"node": definition.node.location(), "scope": list(definition.scope), "id": definition.attribute.value}


def _inspect(source: XMLSource, path: str) -> list[Issue]:
    issues = []
    identity = _identity(source)
    if source.root.name == "MetaDataObject" and identity is None:
        issues.append(Issue("xml.metadata_root", "A metadata file must contain one object", "error", path))
    elif source.root.name == "MetaDataObject" and not identity[1]:
        issues.append(Issue("xml.metadata_uuid_missing", "A metadata object UUID is required", "error", path))
    if identity and identity[1]:
        try:
            UUID(identity[1])
        except ValueError:
            issues.append(Issue("xml.invalid_uuid", "Invalid metadata object UUID", "error", path,
                                {"uuid": identity[1]}))
    metadata_ids = set()
    for node in source.nodes:
        if source.root.name == "MetaDataObject" and node.attribute("uuid") and node.parent and (node.parent is source.root or node.parent.name == "ChildObjects"):
            identifier = node.value("uuid").casefold()
            try:
                identifier = str(UUID(identifier))
            except ValueError:
                issues.append(Issue("xml.invalid_uuid", "Invalid metadata object UUID", "error", path,
                                    {"node": node.location(), "uuid": identifier}))
            if identifier in metadata_ids:
                issues.append(Issue("xml.duplicate_uuid", "Two metadata definitions use the same UUID", "error", path,
                                    {"node": node.location(), "uuid": identifier}))
            metadata_ids.add(identifier)
        if node.name in COLLECTIONS:
            keys = set()
            for child in node.children:
                key = _child_key(child, node)
                if key is not None and key in keys:
                    issues.append(Issue("xml.duplicate_collection_key", "Duplicate logical name in a supported collection", "error", path,
                                        {"node": child.location()}))
                keys.add(key)
        if source.root.name == "Form" and _id_scope(node) is not None and node.attribute("id") is None:
            issues.append(Issue("xml.missing_id", "A supported form definition requires its scoped ID", "error", path,
                                {"node": node.location()}))
        values = [(attr.value, attr.qname) for attr in node.attributes if attr.name == XSI_TYPE]
        if node.name == "Type" and not node.children:
            values.append((node.text, "text"))
        for value, position in values:
            if re.fullmatch(r"[\w.-]+:[\w.-]+", value) and value.split(":", 1)[0] not in node.nsmap:
                issues.append(Issue("xml.unbound_qname", "A known QName value uses an unbound namespace prefix", "error", path,
                                    {"node": node.location(), "position": position, "value": value}))
    ids = defaultdict(dict)
    names = defaultdict(dict)
    definitions = list(_definitions(source))
    for definition in definitions:
        value = definition.attribute.value
        if not _INTEGER.fullmatch(value):
            issues.append(Issue("xml.invalid_id", "A supported form ID must be an integer", "error", path, _definition_details(definition)))
            continue
        try:
            numeric = int(value)
        except ValueError:
            issues.append(Issue("xml.invalid_id", "Form ID exceeds the supported integer conversion size", "error", path, _definition_details(definition)))
            continue
        if numeric < 0 and not (numeric == -1 and definition.node.name == "AutoCommandBar" and definition.node.parent is source.root):
            issues.append(Issue("xml.reserved_id", "Unsupported reserved form ID", "error", path, _definition_details(definition)))
        if numeric in ids[definition.scope]:
            issues.append(Issue("xml.duplicate_id", "Duplicate ID in the same form scope", "error", path, _definition_details(definition)))
        ids[definition.scope][numeric] = definition
        if not definition.identity:
            issues.append(Issue("xml.missing_name", "A supported form definition needs a name", "error", path, _definition_details(definition)))
        elif definition.identity in names[definition.scope]:
            issues.append(Issue("xml.duplicate_name", "Duplicate name in the same form scope", "error", path, _definition_details(definition)))
        names[definition.scope][definition.identity] = definition
    if source.root.name == "Form":
        known_positions = {d.attribute.value_start for d in definitions}
        for node in source.nodes:
            attr = node.attribute("id")
            if attr and attr.value_start not in known_positions:
                issues.append(Issue("xml.unknown_id_scope", "ID scope is not supported at this XML position", "warning", path,
                                    {"node": node.location(), "id": attr.value}))
        attr_names = set(names[("attributes",)])
        item_names = set(names[("items",)])
        command_names = set(names[("commands",)])
        for node in source.nodes:
            value = node.text.strip()
            issue = None
            if node.name == "DataPath" and value:
                head = value.split(".", 1)[0].casefold()
                if head not in attr_names:
                    issue = ("xml.unresolved_data_path", "DataPath does not name a declared form attribute")
            elif node.name == "CommandName" and value.startswith("Form.Command."):
                if value[len("Form.Command."):].casefold() not in command_names:
                    issue = ("xml.unresolved_command", "CommandName does not name a declared form command")
            elif node.name == "CommandName" and value.startswith("Form.Item."):
                if value[len("Form.Item."):].split(".", 1)[0].casefold() not in item_names:
                    issue = ("xml.unresolved_item", "CommandName does not name a declared form item")
            elif node.name == "Item" and node.parent and node.parent.name in {"SearchControlAddition", "SearchStringAddition", "ViewStatusAddition"}:
                if value.casefold() not in item_names:
                    issue = ("xml.unresolved_item", "Addition does not name a declared form item")
            if issue:
                # A standalone XML file cannot prove whether BSL creates an
                # attribute/command dynamically. New unresolved links in a merge
                # are nevertheless blocking, rather than declared safe.
                issues.append(Issue(issue[0], issue[1], "warning", path,
                                    {"node": node.location(), "value": value}))
    return issues


def inspect_xml(data: bytes, *, path: str = "") -> list[Issue]:
    """Check strict XML syntax and supported form invariants without writing."""
    try:
        source = XMLSource(data)
    except (XMLSourceError, RecursionError) as error:
        return [Issue("xml.invalid_input", str(error), "error", path)]
    return _inspect(source, path)


def _apply(data: bytes, patches: list[_Patch]) -> bytes:
    result = []
    cursor = 0
    # Stable sorting keeps multiple inserts at one location in planned order.
    for patch in sorted(patches, key=lambda p: (p.start, p.end)):
        if patch.start < cursor or not 0 <= patch.start <= patch.end <= len(data):
            raise XMLSourceError("Overlapping or invalid source patches")
        result.extend((data[cursor:patch.start], patch.content))
        cursor = patch.end
    result.append(data[cursor:])
    return b"".join(result)


class _Merger:
    def __init__(self, base: XMLSource, local: XMLSource, incoming: XMLSource, path: str):
        self.base, self.local, self.incoming, self.path = base, local, incoming, path
        self.issues: list[Issue] = []
        self.changes: list[Change] = []
        self.patches: list[_Patch] = []
        self.id_values: dict[int, str] = {}
        self.added_definitions: set[int] = set()
        self.namespace_inserts: dict[int, dict[str, str]] = defaultdict(dict)
        self.fingerprints: dict[int, tuple] = {}
        self.cache_fingerprints = False

    def issue(self, code, message, node, **details):
        self.issues.append(Issue(code, message, path=self.path,
                                 details={"node": node.location(), **details}))

    def change(self, code, message, node, **details):
        self.changes.append(Change(code, message, self.path,
                                   {"node": node.location(), **details}))

    def fp(self, node, *, ignore_ids=False):
        return _fingerprint(node, ignore_ids=ignore_ids,
                            projections=self.id_values if node.source is self.local else None,
                            cache=self.fingerprints if self.cache_fingerprints and not ignore_ids else None)

    def equivalent(self, left, right, *, ignore_ids=False):
        return self.fp(left, ignore_ids=ignore_ids) == self.fp(right, ignore_ids=ignore_ids)

    def preassign_ids(self):
        def parent_key(node):
            return tuple((parent.name, parent.value("name").casefold())
                         for parent in _parents(node) if parent.value("name"))

        base_defs = {(d.scope, d.identity): d for d in _definitions(self.base)}
        base_ids = {(d.scope, int(d.attribute.value)): d for d in base_defs.values()}
        new_defs = {(d.scope, d.identity): d for d in _definitions(self.incoming)}
        local_defs = list(_definitions(self.local))
        local_keys = {(d.scope, d.identity) for d in local_defs}
        occupied = defaultdict(set)
        all_values = defaultdict(set)
        collisions = []
        for definition in list(new_defs.values()) + local_defs:
            all_values[definition.scope].add(int(definition.attribute.value))
        for definition in new_defs.values():
            occupied[definition.scope].add(int(definition.attribute.value))
        for definition in local_defs:
            key = (definition.scope, definition.identity)
            if key in base_defs:
                old = base_defs[key]
                # Moving an existing definition is not an independent new
                # addition. Keep that operation explicit until a graph-aware
                # move can transfer incoming edits and all dependencies.
                if definition.scope == ("items",):
                    if parent_key(old.node) != parent_key(definition.node):
                        self.issue("xml.move_unsupported", "Moving an existing form item between parents requires review", definition.node)
                continue
            old = base_ids.get((definition.scope, int(definition.attribute.value)))
            if old and (old.scope, old.identity) not in local_keys:
                self.issue("xml.rename_unsupported", "An existing ID now has a different name; rename versus replacement is ambiguous", definition.node,
                           previous=old.node.value("name"))
            self.added_definitions.add(definition.attribute.value_start)
            if key in new_defs:
                if definition.node.tag == new_defs[key].node.tag:
                    self.id_values[definition.attribute.value_start] = new_defs[key].attribute.value
                    if self.equivalent(definition.node, new_defs[key].node, ignore_ids=True):
                        self.change("xml.addition_deduplicated", "Equivalent additions retained once with incoming identity", new_defs[key].node)
                continue
            numeric = int(definition.attribute.value)
            if numeric in occupied[definition.scope]:
                if numeric < 0:
                    self.issue("xml.reserved_id_collision", "A reserved form ID cannot be renumbered", definition.node,
                               id=definition.attribute.value)
                else:
                    collisions.append(definition)
            else:
                occupied[definition.scope].add(numeric)
        for definition in collisions:
            candidate = max(all_values[definition.scope] | occupied[definition.scope] | {0}) + 1
            occupied[definition.scope].add(candidate)
            try:
                replacement = str(candidate)
            except ValueError:
                self.issue("xml.invalid_id", "A new form ID exceeds the supported integer conversion size", definition.node)
                continue
            self.id_values[definition.attribute.value_start] = replacement
            self.change("xml.id_remapped", "Local addition ID remapped within its scope", definition.node,
                        scope=list(definition.scope), old=definition.attribute.value, new=replacement)
        changed_values = {int(d.attribute.value) for d in local_defs
                          if int(self.id_values.get(d.attribute.value_start, d.attribute.value)) != int(d.attribute.value)}
        if changed_values:
            def changed_integer(value):
                value = value.strip()
                if re.fullmatch(r"[+-]?\d+", value, flags=re.ASCII) is None:
                    return False
                try:
                    return int(value) in changed_values
                except ValueError:
                    # It is still an unknown numeric position. A conversion
                    # resource limit must stop migration, rather than bypass it.
                    return True

            for node in self.local.nodes:
                if not node.children and changed_integer(node.text) and node.name not in NUMERIC_SCALARS:
                    self.issue("xml.unknown_id_reference", "Cannot prove how this numeric value relates to a remapped ID", node,
                               value=node.text.strip())
                for attr in _attributes(node).values():
                    if attr.name != "id" and changed_integer(attr.value) and local_name(attr.name) not in NUMERIC_SCALARS:
                        self.issue("xml.unknown_id_reference", "Cannot rewrite an unrecognized numeric attribute as an ID reference", node,
                                   attribute=attr.qname, value=attr.value)

    def _namespace_patches(self, donor: Node, target_ns: dict[str, str]):
        own = {a.qname[6:] if a.qname.startswith("xmlns:") else ""
               for a in donor.attributes if a.name.startswith("{" + XMLNS_NS + "}")}
        additions = []
        # Carry the donor namespace closure, including QName-valued text and
        # unknown opaque subtrees. Bindings remain local to the copied root.
        for prefix, uri in donor.nsmap.items():
            if prefix == "xml" or prefix in own or target_ns.get(prefix, "") == uri:
                continue
            label = "xmlns:" + prefix if prefix else "xmlns"
            additions.append(b" " + label.encode("utf-8") + b'="' + _escape(uri) + b'"')
        if not donor.nsmap.get("", "") and target_ns.get("", "") and "" not in own:
            additions.append(b' xmlns=""')
        if additions:
            offset = donor.open_end - (2 if donor.self_closing else 1)
            return [_Patch(offset, offset, b"".join(additions))]
        return []

    def transplant(self, donor: Node, target_parent: Node | None, target_indent=None):
        patches = []
        for node in donor.walk():
            for attr in node.attributes:
                if attr.value_start in self.id_values:
                    value = self.id_values[attr.value_start]
                    if value != attr.value:
                        patches.append(_Patch(attr.value_start, attr.value_end, _escape(value, attr.quote)))
        patches += self._namespace_patches(donor, target_parent.nsmap if target_parent else {})
        parent_space = target_parent.preserve_space if target_parent else False
        if donor.preserve_space != parent_space and donor.attribute("{http://www.w3.org/XML/1998/namespace}space") is None:
            at = donor.open_end - (2 if donor.self_closing else 1)
            patches.append(_Patch(at, at, b' xml:space="preserve"' if donor.preserve_space else b' xml:space="default"'))
        old_indent = donor.source.line_indent(donor.start)
        if target_indent is not None and old_indent is not None:
            for node in donor.walk():
                if not node.children or _preserves_space(node):
                    continue
                cursor = node.open_end
                for end, following in [(child.start, child.end) for child in node.children] + [(node.close_start, node.close_start)]:
                    gap = donor.source.data[cursor:end]
                    if gap and not gap.strip():
                        new_gap = gap
                        # Only inter-element whitespace is adapted. Text, CDATA
                        # and comment contents are never reindented by line regex.
                        new_gap = re.sub(rb"(\r?\n)([ \t]*)", lambda m: self.incoming.newline + (
                            target_indent + m[2][len(old_indent):] if m[2].startswith(old_indent) else m[2]), gap)
                        if new_gap != gap:
                            patches.append(_Patch(cursor, end, new_gap))
                    cursor = following
        relative = [_Patch(p.start - donor.start, p.end - donor.start, p.content) for p in patches]
        return _apply(donor.raw, relative)

    def _copy(self, local, incoming):
        self.patches.append(_Patch(incoming.start, incoming.end,
                                   self.transplant(local, incoming.parent,
                                                   incoming.source.line_indent(incoming.start))))
        self.change("xml.subtree_updated", "Applied an isolated local subtree change", incoming)

    def _key(self, node: Node, parent: Node):
        return _child_key(node, parent)

    def _mapping(self, node):
        mapping = {}
        for child in node.children:
            key = self._key(child, node)
            if key is None or key in mapping:
                return None
            mapping[key] = child
        return mapping

    def _is_supported(self, node):
        if node.parent is None or node.name in PROPERTY_CONTAINERS:
            return True
        return node.parent.name == "MetaDataObject" and bool(node.attribute("uuid"))

    def _unknown_new_ids(self, node):
        if self.local.root.name != "Form":
            return False
        return any(child.attribute("id") and _id_scope(child) is None for child in node.walk())

    def merge_node(self, base: Node, local: Node, incoming: Node):
        if base.tag != local.tag or base.tag != incoming.tag:
            self.issue("xml.node_type", "The matched XML node changes its type", incoming,
                       base=base.tag, local=local.tag, incoming=incoming.tag)
            return
        if any(node.attribute("uuid") for node in (base, local, incoming)):
            uuids = [node.value("uuid").casefold() for node in (base, local, incoming)]
            if len(set(uuids)) != 1:
                self.issue("xml.identity_mismatch", "Same-position metadata objects have different UUIDs", incoming,
                           base=uuids[0], local=uuids[1], incoming=uuids[2])
                return
        if self.equivalent(local, base) or self.equivalent(local, incoming):
            return
        mixed = any(node.children and (node.text.strip() or _preserves_space(node) and node.text)
                    for node in (base, local, incoming))
        bm, lm, nm = self._mapping(base), self._mapping(local), self._mapping(incoming)
        if mixed or (local.children or base.children or incoming.children) and (not self._is_supported(base) or bm is None or lm is None or nm is None):
            if self.equivalent(incoming, base):
                if self._unknown_new_ids(local):
                    self.issue("xml.unknown_id_scope", "Local subtree has unsupported ID definitions", local)
                else:
                    self._copy(local, incoming)
            else:
                self.issue("xml.unsupported_structure", "Concurrent changes in an opaque/mixed/repeated XML structure require review", incoming)
            return
        self._merge_attributes(base, local, incoming)
        self._merge_markup(base, local, incoming, bm or {}, lm or {}, nm or {})
        if not (base.children or local.children or incoming.children):
            self._merge_leaf(base, local, incoming)
        else:
            self._merge_children(base, local, incoming, bm or {}, lm or {}, nm or {})

    def _merge_attributes(self, base, local, incoming):
        def semantic(value, node, attribute_name):
            return _qvalue(value, node, unprefixed=True) if attribute_name == XSI_TYPE and value is not None else value

        ba, la, na = map(_attributes, (base, local, incoming))
        for name in set(ba) | set(la):
            bv = ba[name].value if name in ba else None
            lv = la[name].value if name in la else None
            nv = na[name].value if name in na else None
            if local.source is self.local and name in la:
                lv = self.id_values.get(la[name].value_start, lv)
            bs, ls, ns = semantic(bv, base, name), semantic(lv, local, name), semantic(nv, incoming, name)
            if ls == bs or ls == ns:
                continue
            if name == "id" and _id_scope(local) is not None:
                self.issue("xml.existing_id_change", "Changing an existing definition ID requires explicit identity/reference migration", incoming,
                           base=bv, local=lv, incoming=nv)
                continue
            if ns != bs:
                self.issue("xml.attribute_conflict", "Both sides changed the same XML attribute", incoming,
                           attribute=name, base=bv, local=lv, incoming=nv)
                continue
            if name not in la:
                attr = na[name]
                self.patches.append(_Patch(attr.start, attr.end, b""))
            elif name in na:
                attr = na[name]
                if name == XSI_TYPE:
                    lv = self._adapt_qvalue(lv, local, incoming, unprefixed=True)
                    if lv is None:
                        continue
                self.patches.append(_Patch(attr.value_start, attr.value_end, _escape(lv, attr.quote)))
            else:
                attr = la[name]
                qname = self._adapt_attribute_name(attr, local, incoming)
                if name == XSI_TYPE:
                    lv = self._adapt_qvalue(lv, local, incoming, unprefixed=True)
                    if lv is None:
                        continue
                self.patches.append(_Patch(incoming.open_end - (2 if incoming.self_closing else 1),
                                           incoming.open_end - (2 if incoming.self_closing else 1),
                                           b" " + qname.encode("utf-8") + b"=" + attr.quote + _escape(lv, attr.quote) + attr.quote))
            self.change("xml.attribute_updated", "Applied a local XML attribute change", incoming, attribute=name)

    def _prefix(self, uri, preferred, incoming):
        # Existing bindings are never rebound on an existing target node: that
        # could alter untouched tag names or QName-valued descendants.
        # Planned declarations are already part of the effective environment.
        # Otherwise independent additions could claim the same generated alias
        # for different URIs before any of their byte patches are applied.
        bindings = {}
        for node in reversed([incoming, *_parents(incoming)]):
            bindings.update(node.nsmap)
            bindings.update(self.namespace_inserts.get(node.start, {}))
        for prefix, existing in bindings.items():
            if prefix and existing == uri:
                return prefix
        prefix = preferred or "m1c"
        index = 1
        while prefix in bindings and bindings[prefix] != uri:
            prefix = (preferred or "m1c") + str(index)
            index += 1
        if prefix not in bindings:
            self.namespace_inserts[incoming.start][prefix] = uri
            at = incoming.open_end - (2 if incoming.self_closing else 1)
            self.patches.append(_Patch(at, at, b" xmlns:" + prefix.encode() + b'="' + _escape(uri) + b'"'))
        return prefix

    def _adapt_qvalue(self, value, donor, incoming, *, unprefixed=False):
        resolved = _qvalue(value, donor, unprefixed=unprefixed)
        if isinstance(resolved, tuple):
            if not resolved[0]:
                if incoming.nsmap.get("", ""):
                    self.issue("xml.namespace_conflict", "A no-namespace QName cannot be transplanted without changing the target default namespace", incoming)
                    return None
                return resolved[1]
            if ":" not in value and incoming.nsmap.get("", "") == resolved[0]:
                return resolved[1]
            prefix = self._prefix(resolved[0], value.split(":", 1)[0] if ":" in value else "m1c", incoming)
            return prefix + ":" + resolved[1]
        return value

    def _adapt_attribute_name(self, attr, donor, incoming):
        if not attr.name.startswith("{"):
            return attr.qname
        uri, name = attr.name[1:].split("}", 1)
        preferred = attr.qname.split(":", 1)[0] if ":" in attr.qname else "m1c"
        return self._prefix(uri, preferred, incoming) + ":" + name

    def _merge_leaf(self, base, local, incoming):
        bv, lv, nv = base.text, local.text, incoming.text
        if local.name == "Type":
            def qvalue(value, node):
                return _qvalue(value, node, unprefixed=node.tag == CORE_TYPE)

            same_base = qvalue(lv, local) == qvalue(bv, base)
            same_new = qvalue(lv, local) == qvalue(nv, incoming)
            new_same_base = qvalue(nv, incoming) == qvalue(bv, base)
        else:
            same_base, same_new, new_same_base = lv == bv, lv == nv, nv == bv
        if same_base or same_new:
            return
        if not new_same_base:
            self.issue("xml.value_conflict", "Both sides changed the same XML value", incoming,
                       base=bv, local=lv, incoming=nv)
            return
        inner = local.source.data[local.open_end:local.close_start] if not local.self_closing else b""
        if local.name == "Type" and isinstance(_qvalue(lv, local, unprefixed=local.tag == CORE_TYPE), tuple):
            # A changed QName can use a target alias without disturbing existing
            # namespace bindings. Preserve the raw token when already valid.
            adapted = self._adapt_qvalue(lv, local, incoming, unprefixed=local.tag == CORE_TYPE)
            if adapted is None:
                return
            if adapted != lv:
                inner = _escape(adapted)
        if incoming.self_closing:
            suffix = b">" + inner + b"</" + incoming.qname.encode("utf-8") + b">"
            self.patches.append(_Patch(incoming.open_end - 2, incoming.open_end, suffix))
        else:
            self.patches.append(_Patch(incoming.open_end, incoming.close_start, inner))
        self.change("xml.value_updated", "Applied a local XML value change", incoming)

    def _merge_markup(self, base, local, incoming, bm, lm, nm):
        def positioned(node, excluded=()):
            return [(mark.kind, mark.value,
                     self._key(node.children[mark.before_child], node) if mark.before_child < len(node.children) else None)
                    for mark in node.markup if id(mark) not in excluded]

        bmarks = Counter((m.kind, m.value) for m in base.markup)
        base_positions, local_positions, incoming_positions = map(positioned, (base, local, incoming))
        if local_positions == base_positions or local_positions == incoming_positions:
            return
        # Newly inserted comments immediately before a new child travel with
        # that child. Existing comment/PI edits are deliberately explicit.
        carried = set()
        marks_by_child = defaultdict(list)
        for mark in local.markup:
            marks_by_child[mark.before_child].append(mark)
        for index, child in enumerate(local.children):
            key = self._key(child, local)
            if key not in bm and key not in nm:
                for mark in marks_by_child[index]:
                    mark_key = (mark.kind, mark.value)
                    if not bmarks[mark_key]:
                        carried.add(id(mark))
        if positioned(local, carried) != base_positions:
            self.issue("xml.markup_conflict", "Editing existing comments/processing instructions requires review", incoming)

    def _delete(self, node):
        start, end = node.start, node.end
        indent = node.source.line_indent(start)
        if indent is not None:
            line_start = start - len(indent)
            next_lf = node.source.data.find(b"\n", end)
            if next_lf >= 0 and not node.source.data[end:next_lf].strip(b" \t\r"):
                start, end = line_start, next_lf + 1
        self.patches.append(_Patch(start, end, b""))
        self.change("xml.node_deleted", "Applied a local deletion of an unchanged incoming node", node)

    def _merge_children(self, base, local, incoming, bm, lm, nm):
        if base.name == "ChildItems":
            bseq = [self._key(c, base) for c in base.children]
            lseq = [self._key(c, local) for c in local.children if self._key(c, local) in bm]
            nseq = [self._key(c, incoming) for c in incoming.children if self._key(c, incoming) in bm]
            common = set(lseq) & set(nseq)
            if [k for k in bseq if k in common] != [k for k in lseq if k in common] and [k for k in lseq if k in common] != [k for k in nseq if k in common]:
                self.issue("xml.order_conflict", "A local reorder of existing form items requires review", incoming)
        for key, before in bm.items():
            ours, theirs = lm.get(key), nm.get(key)
            if ours is None:
                if theirs is None:
                    continue
                if self.equivalent(before, theirs):
                    self._delete(theirs)
                else:
                    self.issue("xml.delete_edit", "Local deletion conflicts with an incoming edit", theirs)
            elif theirs is None:
                if not self.equivalent(before, ours):
                    self.issue("xml.edit_delete", "A locally edited node was deleted in the incoming version", ours)
            else:
                self.merge_node(before, ours, theirs)
        additions = []
        for child in local.children:
            key = self._key(child, local)
            if key in bm:
                continue
            if key in nm:
                if self.equivalent(child, nm[key], ignore_ids=True):
                    self.change("xml.addition_deduplicated", "Equivalent additions retained once with incoming identity", nm[key])
                else:
                    self.issue("xml.add_add", "Both sides added different nodes with the same logical name", nm[key])
            elif self._unknown_new_ids(child):
                self.issue("xml.unknown_id_scope", "Cannot insert local form definitions with unknown ID scopes", child)
            elif local.name == "ChildItems" and child.name not in UI_TYPES:
                self.issue("xml.unsupported_item_type", "Cannot insert an unsupported form item type", child)
            else:
                additions.append(child)
        if additions:
            self._insert_additions(base, local, incoming, bm, nm, additions)

    def _insert_additions(self, base, local, incoming, bm, nm, additions):
        local_order = [self._key(c, local) for c in local.children]
        incoming_order = [self._key(c, incoming) for c in incoming.children]
        local_positions = {id(child): index for index, child in enumerate(local.children)}
        incoming_positions = {key: index for index, key in enumerate(incoming_order)}
        local_marks, incoming_marks = defaultdict(list), defaultdict(list)
        for mark in local.markup:
            local_marks[mark.before_child].append(mark)
        for mark in incoming.markup:
            incoming_marks[mark.before_child].append(mark)
        baseline_marks = {(mark.kind, mark.value) for mark in base.markup}
        # Compute nearest surviving original neighbours in two linear passes.
        # Thousands of additions must not each rescan the entire sibling list.
        left_at, right_at = [], [None] * len(local_order)
        previous = None
        for key in local_order:
            left_at.append(previous)
            if key in bm and key in nm:
                previous = key
        following = None
        for index in range(len(local_order) - 1, -1, -1):
            right_at[index] = following
            if local_order[index] in bm and local_order[index] in nm:
                following = local_order[index]
        placements = defaultdict(list)
        anchors = {}
        for child in additions:
            index = local_positions[id(child)]
            left, right = left_at[index], right_at[index]
            if left is not None and right is not None and incoming_positions[left] >= incoming_positions[right]:
                self.issue("xml.anchor_order", "Incoming changes reversed the local insertion anchors", child)
                continue
            if left is None and right is None and bm:
                self.issue("xml.anchor_missing", "No original sibling anchor survives for this local insertion", child)
                continue
            # Insert before the right original neighbour (or at the end), so
            # incoming additions in this same gap remain before local additions.
            placements[right].append(child)
            anchors[id(child)] = left, right
        for right, children in placements.items():
            target = nm[right] if right is not None else None
            if target:
                at = target.start
                indent = incoming.source.line_indent(at)
                marks = incoming_marks[incoming_positions[right]]
                if marks:
                    at = min(at, marks[0].start)
                    indent = incoming.source.line_indent(at)
                if indent is not None:
                    at -= len(indent)
            else:
                at = incoming.close_start
                parent_indent = incoming.source.line_indent(incoming.start) or b""
                indent = next((incoming.source.line_indent(c.start) for c in incoming.children
                               if incoming.source.line_indent(c.start) is not None), None)
                if indent is None and incoming.source.line_indent(at) is not None:
                    indent = parent_indent + self.incoming.indent_unit
                close_indent = incoming.source.line_indent(at)
                if close_indent is not None:
                    at -= len(close_indent)
            rendered = []
            for child in children:
                payload = self.transplant(child, incoming, indent)
                marks = [m for m in local_marks[local_positions[id(child)]]
                         if (m.kind, m.value) not in baseline_marks]
                leading = b""
                for mark in marks:
                    raw = local.source.data[mark.start:mark.end]
                    leading += raw + (self.incoming.newline + (indent or b""))
                payload = leading + payload
                rendered.append((indent or b"") + payload + (self.incoming.newline if indent is not None else b""))
                self.change("xml.node_inserted", "Inserted a local addition after incoming additions in its original sibling gap", child,
                            parent=incoming.location(), left=anchors[id(child)][0], right=anchors[id(child)][1])
            body = b"".join(rendered)
            if incoming.self_closing:
                parent_indent = incoming.source.line_indent(incoming.start) or b""
                child_indent = parent_indent + self.incoming.indent_unit
                # No source children exist. Re-render only this insertion's
                # leading structural whitespace, not any surrounding document.
                fragments = []
                for child in children:
                    for mark in local_marks[local_positions[id(child)]]:
                        if (mark.kind, mark.value) not in baseline_marks:
                            fragments.append(child_indent + local.source.data[mark.start:mark.end] + self.incoming.newline)
                    fragments.append(child_indent + self.transplant(child, incoming, child_indent) + self.incoming.newline)
                body = b"".join(fragments)
                suffix = b">" + self.incoming.newline + body + parent_indent + b"</" + incoming.qname.encode("utf-8") + b">"
                self.patches.append(_Patch(incoming.open_end - 2, incoming.open_end, suffix))
            else:
                self.patches.append(_Patch(at, at, body))


def merge_xml(base: bytes, local: bytes, incoming: bytes, *, path: str = "") -> MergeResult:
    """Merge supported XML changes, retaining incoming bytes outside patches.

Conflicts return an XML candidate and blocking issues. Callers must use
``resolved`` before accepting the candidate; no input/worktree is modified.
"""
    sources = []
    inspections = []
    issues = []
    for label, content in (("base", base), ("local", local), ("incoming", incoming)):
        try:
            source = XMLSource(content)
        except (XMLSourceError, RecursionError) as error:
            issues.append(Issue("xml.invalid_input", str(error), "error", path, {"side": label}))
            continue
        sources.append(source)
        inspected = _inspect(source, path)
        inspections.append(inspected)
        for issue in inspected:
            if issue.blocking:
                issues.append(Issue(issue.code, issue.message, issue.severity, issue.path,
                                    {**issue.details, "side": label}))
    if issues:
        return MergeResult(incoming, issues)
    base_source, local_source, incoming_source = sources
    identities = [_identity(source) for source in sources]
    if len(set(identities)) != 1:
        return MergeResult(incoming, [Issue("xml.identity_mismatch", "Root metadata type/UUID differs between the three versions", path=path,
                                            details={"base": identities[0], "local": identities[1], "incoming": identities[2]})])
    metadata_types = {}
    for source in sources:
        for identity, kind in _metadata_uuids(source).items():
            if identity in metadata_types and metadata_types[identity] != kind:
                return MergeResult(incoming, [Issue("xml.identity_mismatch", "A metadata UUID changes its object kind", path=path,
                                                    details={"uuid": identity, "base_kind": metadata_types[identity], "kind": kind})])
            metadata_types[identity] = kind
    if base_source.root.name in {"Form", "MetaDataObject"} and len({source.root.value("version") for source in sources}) != 1:
        return MergeResult(incoming, [Issue("xml.format_mismatch", "XML dump format versions differ; format conversion is not a merge", path=path)])
    # Validate all sides before this byte-exact identity shortcut.
    if local == base or local == incoming:
        return MergeResult(incoming, inspections[2])
    merger = _Merger(base_source, local_source, incoming_source, path)
    merger.preassign_ids()
    if merger.issues:
        return MergeResult(incoming, merger.issues, merger.changes)
    merger.cache_fingerprints = True
    def outer(source):
        return [(mark.kind, mark.value, mark.start < source.root.start) for mark in source.outer_markup]

    if outer(local_source) != outer(base_source) and outer(local_source) != outer(incoming_source):
        merger.issues.append(Issue("xml.outer_markup", "Editing document-level comments/processing instructions requires review", path=path))
    merger.merge_node(base_source.root, local_source.root, incoming_source.root)
    # Fingerprints accelerate repeated ancestor/child comparisons, but are no
    # longer needed while the fourth (output) XML tree is being validated.
    merger.fingerprints.clear()
    try:
        content = _apply(incoming, merger.patches)
        output = XMLSource(content)
    except (XMLSourceError, RecursionError) as error:
        merger.issues.append(Issue("xml.output_validation", str(error), "error", path))
        return MergeResult(incoming, merger.issues, merger.changes)
    original_warnings = {(i.code, str(sorted(i.details.items()))) for i in inspections[2]}
    for issue in _inspect(output, path):
        if issue.blocking or (issue.code.startswith("xml.unresolved_") and (issue.code, str(sorted(issue.details.items()))) not in original_warnings):
            merger.issues.append(Issue(issue.code, issue.message, "conflict", path, issue.details))
    return MergeResult(content, merger.issues, merger.changes)
