"""Strict UTF-8 XML reader retaining byte ranges for surgical edits.

Expat establishes XML/namespace correctness.  The small start-tag scanner only
locates tokens already accepted by that parser; it does not parse XML by regex.
No serializer is used to reconstruct an existing document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sys import intern
from xml.parsers import expat


XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"
SEPARATOR = "\x1f"


class XMLSourceError(ValueError):
    pass


def expanded(name: str) -> str:
    parts = name.split(SEPARATOR)
    return "{" + parts[0] + "}" + parts[1] if len(parts) > 1 else name


def local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


@dataclass(slots=True)
class Attribute:
    qname: str
    name: str
    value: str
    start: int
    end: int
    value_start: int
    value_end: int
    quote: bytes


@dataclass(slots=True)
class Markup:
    kind: str
    value: str
    start: int
    end: int
    before_child: int


@dataclass(slots=True)
class Node:
    source: "XMLSource"
    tag: str
    qname: str
    start: int
    open_end: int
    self_closing: bool
    nsmap: dict[str, str]
    attributes: list[Attribute]
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)
    markup: list[Markup] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    close_start: int = 0
    end: int = 0
    preserve_space: bool = False
    _local_name: str = field(init=False, repr=False)

    def __post_init__(self):
        self._local_name = intern(local_name(self.tag))

    @property
    def name(self) -> str:
        return self._local_name

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def raw(self) -> bytes:
        return self.source.data[self.start:self.end]

    def attribute(self, name: str) -> Attribute | None:
        for attribute in self.attributes:
            if attribute.name == name:
                return attribute
        return None

    def value(self, name: str, default: str = "") -> str:
        attribute = self.attribute(name)
        return attribute.value if attribute else default

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def location(self) -> str:
        label = self.name
        if self.value("name"):
            label += "[" + self.value("name") + "]"
        return self.parent.location() + "/" + label if self.parent else label


def _tag_end(data: bytes, start: int) -> int:
    quote = 0
    for index in range(start, len(data)):
        value = data[index]
        if quote:
            if value == quote:
                quote = 0
        elif value in (34, 39):
            quote = value
        elif value == 62:
            return index + 1
    raise XMLSourceError("Unterminated XML tag")


def _qname(name: str, nsmap: dict[str, str], *, attribute=False) -> str:
    if name == "xmlns":
        return "{" + XMLNS_NS + "}"
    if name.startswith("xmlns:"):
        return "{" + XMLNS_NS + "}" + name[6:]
    if ":" in name:
        prefix, local = name.split(":", 1)
        uri = nsmap.get(prefix)
        if uri is None:
            raise XMLSourceError("Unbound namespace prefix: " + prefix)
        return "{" + uri + "}" + local
    uri = "" if attribute else nsmap.get("", "")
    return "{" + uri + "}" + name if uri else name


def _start_tokens(data: bytes, start: int, end: int):
    index = start + 1
    name_start = index
    while index < end and data[index] not in b" \t\r\n/>":
        index += 1
    qname = intern(data[name_start:index].decode("utf-8"))
    attributes = []
    while index < end:
        whitespace_start = index
        while index < end and data[index] in b" \t\r\n":
            index += 1
        if data[index:index + 1] in (b"/", b">"):
            break
        name_start = index
        while index < end and data[index] not in b" \t\r\n=":
            index += 1
        name = intern(data[name_start:index].decode("utf-8"))
        while data[index] in b" \t\r\n":
            index += 1
        if data[index] != 61:
            raise XMLSourceError("Cannot locate attribute equals sign")
        index += 1
        while data[index] in b" \t\r\n":
            index += 1
        quote = data[index:index + 1]
        if quote not in (b"'", b'"'):
            raise XMLSourceError("Cannot locate quoted attribute value")
        value_start = index + 1
        value_end = data.find(quote, value_start, end)
        if value_end < 0:
            raise XMLSourceError("Unterminated attribute value")
        index = value_end + 1
        attributes.append((name, whitespace_start, index, value_start, value_end, quote))
    return qname, attributes


class XMLSource:
    def __init__(self, data: bytes):
        if not isinstance(data, bytes):
            raise XMLSourceError("XML input must be bytes")
        self.data = data
        self.root: Node | None = None
        self.nodes: list[Node] = []
        self.outer_markup: list[Markup] = []
        expanded_names: dict[str, str] = {}
        try:
            data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as error:
            raise XMLSourceError("Only strict UTF-8 XML is supported") from error
        if b"\x00" in data:
            raise XMLSourceError("NUL/UTF-16 XML is not supported")
        parser = expat.ParserCreate(namespace_separator=SEPARATOR)
        parser.namespace_prefixes = True
        parser.ordered_attributes = True
        parser.specified_attributes = True
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        stack: list[Node] = []
        pending_namespaces: dict[str, str] = {}

        def declaration(version, encoding, standalone):
            if encoding and encoding.lower().replace("-", "") != "utf8":
                raise XMLSourceError("XML encoding declaration must be UTF-8")
            if version != "1.0":
                raise XMLSourceError("Only XML 1.0 is supported")

        def forbidden(*args):
            raise XMLSourceError("DTD and declared/external entities are not supported")

        def namespace(prefix, uri):
            pending_namespaces[prefix or ""] = uri or ""

        def start(name, attrs):
            if len(stack) >= 256:
                raise XMLSourceError("XML nesting exceeds the supported 256-element depth")
            offset = parser.CurrentByteIndex
            end = _tag_end(data, offset)
            qname, tokens = _start_tokens(data, offset, end)
            parent_namespaces = stack[-1].nsmap if stack else {"xml": XML_NS}
            # Namespace environments are immutable after this callback. Most
            # Designer nodes inherit the same ~20 declarations, so sharing the
            # mapping avoids one large dictionary per XML node.
            nsmap = {**parent_namespaces, **pending_namespaces} if pending_namespaces else parent_namespaces
            pending_namespaces.clear()
            values = {expanded(attrs[i]): attrs[i + 1] for i in range(0, len(attrs), 2)}
            attributes = []
            for raw_name, astart, aend, vstart, vend, quote in tokens:
                semantic_name = _qname(raw_name, nsmap, attribute=True)
                if semantic_name.startswith("{" + XMLNS_NS + "}"):
                    prefix = raw_name[6:] if raw_name.startswith("xmlns:") else ""
                    value = nsmap.get(prefix, "")
                else:
                    value = values[semantic_name]
                attributes.append(Attribute(raw_name, semantic_name, value, astart, aend,
                                            vstart, vend, quote))
            if name not in expanded_names:
                expanded_names[name] = expanded(name)
            node = Node(self, expanded_names[name], qname, offset, end,
                        data[offset:end].endswith(b"/>"), nsmap, attributes,
                        parent=stack[-1] if stack else None)
            space = node.attribute("{" + XML_NS + "}space")
            node.preserve_space = (space.value == "preserve") if space else bool(stack and stack[-1].preserve_space)
            self.nodes.append(node)
            if stack:
                stack[-1].children.append(node)
            else:
                self.root = node
            stack.append(node)

        def finish(name):
            node = stack.pop()
            if node.self_closing:
                node.close_start = node.open_end
                node.end = node.open_end
            else:
                node.close_start = parser.CurrentByteIndex
                node.end = _tag_end(data, node.close_start)

        def text(value):
            if stack:
                stack[-1].text_parts.append(value)

        def markup(kind, value, terminator):
            offset = parser.CurrentByteIndex
            end = data.find(terminator, offset)
            if end < 0:
                raise XMLSourceError("Unterminated XML markup")
            mark = Markup(kind, value, offset, end + len(terminator),
                          len(stack[-1].children) if stack else 0)
            (stack[-1].markup if stack else self.outer_markup).append(mark)

        parser.XmlDeclHandler = declaration
        parser.StartDoctypeDeclHandler = forbidden
        parser.EntityDeclHandler = forbidden
        parser.ExternalEntityRefHandler = forbidden
        parser.StartNamespaceDeclHandler = namespace
        parser.StartElementHandler = start
        parser.EndElementHandler = finish
        parser.CharacterDataHandler = text
        parser.CommentHandler = lambda value: markup("comment", value, b"-->")
        parser.ProcessingInstructionHandler = lambda target, value: markup("pi", target + " " + value, b"?>")
        try:
            parser.Parse(data, True)
        except (expat.ExpatError, UnicodeError, KeyError, IndexError) as error:
            raise XMLSourceError("Invalid or unsupported XML: " + str(error)) from error
        if self.root is None:
            raise XMLSourceError("XML document has no root element")

    def line_indent(self, offset: int) -> bytes | None:
        line_start = self.data.rfind(b"\n", 0, offset) + 1
        prefix = self.data[line_start:offset]
        return prefix if not prefix.strip(b" \t\r") else None

    @property
    def newline(self) -> bytes:
        first = self.data.find(b"\n")
        return b"\r\n" if first > 0 and self.data[first - 1:first] == b"\r" else b"\n"

    @property
    def indent_unit(self) -> bytes:
        for node in self.root.walk():
            if node.parent:
                parent_indent = self.line_indent(node.parent.start)
                indent = self.line_indent(node.start)
                if parent_indent is not None and indent is not None and len(indent) > len(parent_indent):
                    return indent[len(parent_indent):]
        return b"\t"
