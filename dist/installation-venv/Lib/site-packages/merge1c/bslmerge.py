"""Conservative, source-preserving BSL integration.

This module is a bounded lexer and method index, not a BSL compiler. It merges
whole methods and, when their order is unchanged, the text between methods.
Ambiguous conditional declarations require a manual decision. Platform syntax
checking remains a separate validation step.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import re
from itertools import pairwise

from .models import Change, Issue, MergeResult


_START = {"процедура": "procedure", "procedure": "procedure",
          "функция": "function", "function": "function"}
_END = {"конецпроцедуры": "procedure", "endprocedure": "procedure",
        "конецфункции": "function", "endfunction": "function"}
_VAR = {"перем", "var"}
_VALUE = {"знач", "val"}
_EXPORT = {"экспорт", "export"}
_DIRECTIVES = {
    "область": "region", "region": "region",
    "конецобласти": "endregion", "endregion": "endregion",
    "если": "if", "if": "if", "иначеесли": "elsif", "elsif": "elsif",
    "иначе": "else", "else": "else", "конецесли": "endif", "endif": "endif",
    "вставка": "insert", "insert": "insert",
    "конецвставки": "endinsert", "endinsert": "endinsert",
    "удаление": "delete", "delete": "delete",
    "конецудаления": "enddelete", "enddelete": "enddelete",
}
_SYMBOLS = set(".,;:()[]+-*/%=<>?&~|")
_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _Method:
    name: str
    key: str
    start: int
    end: int
    declaration: int
    kind: str
    region: tuple[tuple[str, int], ...]
    guards: tuple[tuple[int, int], ...]
    raw: str


@dataclass
class _Source:
    text: str
    bom: bool
    tokens: list[_Token] = field(default_factory=list)
    methods: list[_Method] = field(default_factory=list)
    # Region path -> (position after opening line, start of closing line).
    regions: dict[tuple[tuple[str, int], ...], tuple[int, int]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    unsupported: bool = False

    @property
    def names(self) -> list[str]:
        return [method.key for method in self.methods]

    def encoded(self, text: str) -> bytes:
        return (_BOM if self.bom else b"") + text.encode("utf-8")

    def gaps(self) -> list[tuple[int, int, str]]:
        result = []
        last = 0
        for method in self.methods:
            result.append((last, method.start, self.text[last:method.start]))
            last = method.end
        result.append((last, len(self.text), self.text[last:]))
        return result

    def scaffold(self) -> tuple[tuple[str, str], ...]:
        """Only use this comparison to ignore layout around added/moved methods."""
        result = []
        index = 0
        for token in self.tokens:
            while index < len(self.methods) and token.start >= self.methods[index].end:
                index += 1
            if index < len(self.methods):
                method = self.methods[index]
                if method.start <= token.start < method.end:
                    continue
            if token.kind != "space":
                result.append((token.kind, token.text))
        return tuple(result)


def _issue(code: str, message: str, path: str, *, severity: str = "error", **details) -> Issue:
    return Issue(code, message, severity, path, details)


def _line_start(text: str, position: int) -> int:
    return max(text.rfind("\n", 0, position), text.rfind("\r", 0, position)) + 1


def _line_end(text: str, position: int) -> int:
    ends = [index for index in (text.find("\n", position), text.find("\r", position)) if index >= 0]
    if not ends:
        return len(text)
    end = min(ends)
    return end + (2 if text.startswith("\r\n", end) else 1)


def _line_content_end(text: str, position: int) -> int:
    end = _line_end(text, position)
    if text[max(position, end - 2):end] == "\r\n":
        return end - 2
    if end > position and text[end - 1] in "\r\n":
        return end - 1
    return end


def _previous_line_start(text: str, start: int) -> int:
    previous_end = start - 1
    if previous_end > 0 and text[previous_end] == "\n" and text[previous_end - 1] == "\r":
        previous_end -= 1
    return _line_start(text, previous_end)


def _lex(text: str, path: str) -> tuple[list[_Token], list[Issue]]:
    tokens = []
    issues = []
    index = 0
    size = len(text)
    while index < size:
        start = index
        char = text[index]
        if char in " \t\f\r\n":
            index += 1
            while index < size and text[index] in " \t\f\r\n":
                index += 1
            kind = "space"
        elif text.startswith("//", index):
            index = _line_content_end(text, index)
            kind = "comment"
        elif char == '"':
            index += 1
            closed = False
            while index < size:
                if text[index] == '"':
                    if index + 1 < size and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                if text[index] in "\r\n":
                    if text[index] == "\r" and index + 1 < size and text[index + 1] == "\n":
                        index += 1
                    next_line = index + 1
                    while next_line < size and text[next_line] in " \t":
                        next_line += 1
                    if next_line >= size or text[next_line] != "|":
                        issues.append(_issue(
                            "BSL_STRING_CONTINUATION", "Unsupported or invalid multiline string continuation.",
                            path, offset=start))
                        break
                    index = next_line + 1
                    continue
                index += 1
            if not closed:
                issues.append(_issue("BSL_UNCLOSED_STRING", "Unclosed BSL string literal.", path, offset=start))
                # Stop: treating the remaining string as code could invent declarations.
                tokens.append(_Token("string", text[start:], start, size))
                break
            kind = "string"
        elif char == "'":
            index += 1
            while index < size and text[index] not in "'\r\n":
                index += 1
            if index >= size or text[index] != "'":
                issues.append(_issue("BSL_UNCLOSED_DATE", "Unclosed date literal.", path, offset=start))
                break
            if not text[start + 1:index].isascii() or not text[start + 1:index].isdigit():
                issues.append(_issue("BSL_INVALID_DATE_LITERAL", "Unsupported date literal.", path, offset=start))
            index += 1
            kind = "date"
        elif char == "#":
            if text[_line_start(text, index):index].strip():
                issues.append(_issue("BSL_DIRECTIVE_POSITION", "A preprocessor directive must start a line.", path, offset=start))
            index = _line_content_end(text, index)
            kind = "directive"
        elif char.isalpha() or char == "_":
            index += 1
            while index < size and (text[index].isalnum() or text[index] == "_"):
                index += 1
            kind = "identifier"
        elif char.isdecimal() and char.isascii():
            index += 1
            while index < size and text[index].isdecimal() and text[index].isascii():
                index += 1
            kind = "number"
        elif char in _SYMBOLS:
            index += 1
            kind = "symbol"
        else:
            index += 1
            kind = "unknown"
            issues.append(_issue("BSL_UNKNOWN_CHARACTER", "Unsupported character outside a string or comment.",
                                 path, offset=start, character=char))
        tokens.append(_Token(kind, text[start:index], start, index))
    return tokens, issues


def _attached_start(text: str, declaration: int) -> int:
    """Attach contiguous documentation and one-line compiler annotations only.

    Regions and blank lines form boundaries; module scaffolding is not moved as
    a side effect of replacing a method.
    """
    start = _line_start(text, declaration)
    while start:
        previous_start = _previous_line_start(text, start)
        previous = text[previous_start:start].strip()
        if previous.startswith("//") or previous.startswith("&"):
            start = previous_start
        else:
            break
    return start


def _exclusive(left: tuple[tuple[int, int], ...], right: tuple[tuple[int, int], ...]) -> bool:
    right_map = dict(right)
    return any(key in right_map and right_map[key] != branch for key, branch in left)


def _source(data: bytes, path: str) -> _Source:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return _Source("", False, issues=[_issue("BSL_ENCODING", "BSL must be valid UTF-8 (with optional BOM).",
                                                 path, offset=exc.start)])
    source = _Source(text, data.startswith(_BOM))
    source.tokens, source.issues = _lex(text, path)
    significant = [token for token in source.tokens if token.kind not in {"space", "comment"}]
    # Guard identity is local to this source, not used for matching across revisions.
    conditions: list[list[int | bool]] = []
    region_stack: list[tuple[tuple[tuple[str, int], ...], int]] = []
    region_counts: dict[tuple, int] = defaultdict(int)
    region_path: tuple[tuple[str, int], ...] = ()
    extension_stack: list[str] = []
    delimiter_stack: list[_Token] = []
    active = None
    declarations: dict[tuple[str, str], list[tuple[tuple[int, int], ...]]] = defaultdict(list)
    method_definitions: dict[str, list[_Method]] = defaultdict(list)
    deleted = 0

    def guards() -> tuple[tuple[int, int], ...]:
        return tuple((int(item[0]), int(item[1])) for item in conditions)

    def declare(scope: str, name: str, token: _Token, category: str) -> None:
        key = (scope, name.casefold())
        current_guards = guards()
        for previous_guards in declarations[key]:
            if not _exclusive(previous_guards, current_guards):
                source.issues.append(_issue(
                    "BSL_DUPLICATE_" + category, "Repeated declaration in the same possible compilation scope.",
                    path, name=name, offset=token.start, scope=scope))
                break
        declarations[key].append(current_guards)

    for index, token in enumerate(significant):
        if token.kind == "directive":
            match = re.match(r"#\s*([^\s]+)(.*)", token.text, re.DOTALL)
            word = match.group(1).casefold() if match else ""
            command = _DIRECTIVES.get(word)
            remainder = match.group(2).split("//", 1)[0].strip() if match else ""
            if command == "if":
                conditions.append([token.start, 0, False])
            elif command in {"elsif", "else"}:
                if not conditions or conditions[-1][2]:
                    source.issues.append(_issue("BSL_PREPROCESSOR_BALANCE", "Unexpected conditional branch.", path, offset=token.start))
                else:
                    conditions[-1][1] = int(conditions[-1][1]) + 1
                    if command == "else":
                        conditions[-1][2] = True
            elif command == "endif":
                if conditions:
                    conditions.pop()
                else:
                    source.issues.append(_issue("BSL_PREPROCESSOR_BALANCE", "Unexpected end of a conditional.", path, offset=token.start))
            elif command == "region":
                count_key = (region_path, remainder.casefold())
                occurrence = region_counts[count_key]
                region_counts[count_key] += 1
                region_path = region_path + ((remainder.casefold(), occurrence),)
                region_stack.append((region_path, _line_end(text, token.end)))
            elif command == "endregion":
                if region_stack:
                    closing_path, opening_end = region_stack.pop()
                    source.regions[closing_path] = (opening_end, _line_start(text, token.start))
                    region_path = region_path[:-1]
                else:
                    source.issues.append(_issue("BSL_REGION_BALANCE", "Unexpected end of a region.", path, offset=token.start))
            elif command in {"insert", "delete"}:
                source.unsupported = True
                extension_stack.append(command)
                if command == "delete":
                    deleted += 1
            elif command in {"endinsert", "enddelete"}:
                source.unsupported = True
                expected = command[3:]
                if not extension_stack or extension_stack[-1] != expected:
                    source.issues.append(_issue("BSL_PREPROCESSOR_BALANCE", "Unexpected extension directive end.", path, offset=token.start))
                else:
                    extension_stack.pop()
                    if expected == "delete":
                        deleted -= 1
            else:
                source.unsupported = True
                source.issues.append(_issue("BSL_UNSUPPORTED_DIRECTIVE", "Unsupported preprocessor directive.",
                                            path, severity="warning", directive=word, offset=token.start))
            continue
        if deleted:
            continue
        if token.kind == "symbol":
            if token.text in "([":
                delimiter_stack.append(token)
            elif token.text in ")]":
                expected = "(" if token.text == ")" else "["
                if delimiter_stack and delimiter_stack[-1].text == expected:
                    delimiter_stack.pop()
                else:
                    source.issues.append(_issue("BSL_DELIMITER_BALANCE", "Unexpected closing delimiter.", path, offset=token.start))
        if token.kind != "identifier":
            continue
        word = token.text.casefold()
        previous = significant[index - 1] if index else None
        if previous is not None and previous.text == ".":
            continue
        if word in _START:
            if active is not None:
                source.issues.append(_issue("BSL_NESTED_METHOD", "Nested method declaration is not supported.", path, offset=token.start))
                continue
            if (index + 2 >= len(significant) or significant[index + 1].kind != "identifier"
                    or significant[index + 2].text != "("):
                source.issues.append(_issue("BSL_METHOD_HEADER", "Invalid or unsupported method declaration.", path, offset=token.start))
                continue
            name_token = significant[index + 1]
            active = (token, name_token, region_path, guards())
            # Parameter names share a scope with explicit local variable declarations.
            scope = "method@" + str(token.start)
            parameter_index = index + 3
            expecting_parameter = True
            depth = 1
            while parameter_index < len(significant) and depth:
                part = significant[parameter_index]
                if part.kind == "directive" or part.text == "&":
                    # Conditional signatures and parameter annotations need the
                    # external grammar; do not invent a parameter symbol index.
                    source.unsupported = True
                if part.text == "(":
                    depth += 1
                elif part.text == ")":
                    depth -= 1
                elif depth == 1 and part.text == ",":
                    expecting_parameter = True
                elif depth == 1 and expecting_parameter and part.kind == "identifier":
                    if part.text.casefold() not in _VALUE:
                        declare(scope, part.text, part, "PARAMETER")
                        expecting_parameter = False
                parameter_index += 1
        elif word in _END:
            if active is None:
                source.issues.append(_issue("BSL_METHOD_BALANCE", "Method end without a declaration.", path, offset=token.start))
                continue
            opening, name_token, opening_region, opening_guards = active
            if _START[opening.text.casefold()] != _END[word]:
                source.issues.append(_issue("BSL_METHOD_BALANCE", "Method end does not match its declaration.", path, offset=token.start))
            if opening_guards != guards() or opening_region != region_path:
                source.unsupported = True
            end = _line_end(text, token.end)
            tail = text[token.end:end].strip()
            if tail.startswith(";"):
                tail = tail[1:].strip()
            if tail and not tail.startswith("//"):
                source.issues.append(_issue("BSL_METHOD_LAYOUT", "Unsupported code after a method end on the same line.", path, offset=token.end))
            start = _attached_start(text, opening.start)
            method = _Method(name_token.text, name_token.text.casefold(), start, end, opening.start,
                             _START[opening.text.casefold()], opening_region, opening_guards, text[start:end])
            for other in method_definitions[method.key]:
                if not _exclusive(other.guards, method.guards):
                    source.issues.append(_issue("BSL_DUPLICATE_METHOD", "Repeated method name in the same possible compilation scope.",
                                                path, name=method.name, offset=method.declaration))
                    break
            method_definitions[method.key].append(method)
            source.methods.append(method)
            active = None
        elif word in _VAR:
            scope = "module" if active is None else "method@" + str(active[0].start)
            variable_index = index + 1
            expecting_variable = True
            while variable_index < len(significant):
                part = significant[variable_index]
                if part.text == ";":
                    break
                if expecting_variable and part.kind == "identifier":
                    if part.text.casefold() in {*_START, *_END, *_VAR, *_EXPORT}:
                        break
                    declare(scope, part.text, part, "VARIABLE")
                    expecting_variable = False
                elif part.text == ",":
                    expecting_variable = True
                elif part.kind != "identifier" or part.text.casefold() not in _EXPORT:
                    break
                variable_index += 1
    if active is not None:
        source.issues.append(_issue("BSL_METHOD_BALANCE", "Unclosed method declaration.", path, offset=active[0].start))
    if conditions or extension_stack:
        source.issues.append(_issue("BSL_PREPROCESSOR_BALANCE", "Unclosed preprocessor block.", path))
    if region_stack:
        source.issues.append(_issue("BSL_REGION_BALANCE", "Unclosed region.", path))
    if delimiter_stack:
        source.issues.append(_issue("BSL_DELIMITER_BALANCE", "Unclosed delimiter.", path, offset=delimiter_stack[-1].start))
    for left, right in pairwise(source.methods):
        if left.end > right.start:
            source.issues.append(_issue("BSL_OVERLAPPING_METHODS", "Method source ranges overlap.", path))
    return source


def inspect_bsl(data: bytes, *, path: str = "") -> list[Issue]:
    """Inspect lexical structure and local declarations, not full BSL semantics."""
    return _source(data, path).issues


def method_names(data: bytes) -> set[str]:
    """Return casefolded method names; an invalid index yields an empty set."""
    source = _source(data, "")
    if any(issue.blocking for issue in source.issues):
        return set()
    return {method.key for method in source.methods}


def _selected(base: str, local: str, incoming: str) -> str | None:
    if local == base or local == incoming:
        return incoming
    if incoming == base:
        return local
    return None


def _without_final_eol(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith(("\n", "\r")):
        return text[:-1]
    return text


def _method_equal(left: str, right: str) -> bool:
    # Moving an EOF method into the middle requires a final line boundary; that
    # boundary is layout, rather than a change of the method's implementation.
    return _without_final_eol(left) == _without_final_eol(right)


def _method_selected(base: str, local: str, incoming: str) -> str | None:
    if _method_equal(local, base) or _method_equal(local, incoming):
        return incoming
    if _method_equal(incoming, base):
        ending = incoming[len(_without_final_eol(incoming)):]
        return _without_final_eol(local) + ending
    return None


def _insertion_text(source: _Source, method: _Method, local: _Source, position: int) -> str:
    eol = "\r\n" if "\r\n" in source.text else ("\r" if "\r" in source.text else "\n")
    block = method.raw
    if position and source.text[position - 1] not in "\r\n":
        block = eol + block
    elif method.start and local.text[_previous_line_start(local.text, method.start):method.start].strip() == "":
        # Transfer a separating blank line without changing the existing bytes.
        before = source.text[:position]
        if before and not before.endswith(("\n\n", "\n\r\n", "\r\r")):
            block = eol + block
    if position < len(source.text) and block and not block.endswith(("\n", "\r")):
        block += eol
    return block


def merge_bsl(base: bytes, local: bytes, incoming: bytes, *, path: str = "") -> MergeResult:
    """Merge independent method changes onto incoming, preserving source slices."""
    ancestor, customized, vendor = (_source(data, path) for data in (base, local, incoming))
    issues = []
    for side, source in (("base", ancestor), ("local", customized), ("incoming", vendor)):
        issues.extend(Issue(issue.code, issue.message, issue.severity, issue.path,
                            {**issue.details, "side": side}) for issue in source.issues)
    if any(issue.blocking for issue in issues):
        return MergeResult(None, issues)
    if customized.text == ancestor.text or customized.text == vendor.text:
        return MergeResult(incoming, issues)
    if vendor.text == ancestor.text:
        return MergeResult(vendor.encoded(customized.text), issues,
                           [Change("BSL_LOCAL_CHANGE", "Applied the local source changes.", path)])
    if any(source.unsupported or any(method.guards for method in source.methods)
           for source in (ancestor, customized, vendor)):
        issues.append(_issue("BSL_CONDITIONAL_STRUCTURE", "Concurrent edits with conditional declarations or unsupported directives require manual integration.",
                             path, severity="conflict"))
        return MergeResult(None, issues)

    base_methods = {method.key: method for method in ancestor.methods}
    local_methods = {method.key: method for method in customized.methods}
    incoming_methods = {method.key: method for method in vendor.methods}
    patches: list[tuple[int, int, str]] = []
    changes = []

    # Whole-method changes, including documentation and annotations.
    for name, previous in base_methods.items():
        left = local_methods.get(name)
        right = incoming_methods.get(name)
        if left is None and right is None:
            continue
        if left is None:
            if not _method_equal(right.raw, previous.raw):
                issues.append(_issue("BSL_DELETE_EDIT", "Local deletion conflicts with incoming method changes.",
                                     path, severity="conflict", method=previous.name))
            else:
                patches.append((right.start, right.end, ""))
                changes.append(Change("BSL_METHOD_REMOVED", "Removed the locally deleted method.", path, {"method": previous.name}))
        elif right is None:
            if not _method_equal(left.raw, previous.raw):
                issues.append(_issue("BSL_DELETE_EDIT", "Incoming deletion conflicts with local method changes.",
                                     path, severity="conflict", method=previous.name))
        else:
            selected = _method_selected(previous.raw, left.raw, right.raw)
            if selected is None:
                issues.append(_issue("BSL_METHOD_CONFLICT", "Both sides changed the same method.",
                                     path, severity="conflict", method=previous.name))
            elif selected != right.raw:
                patches.append((right.start, right.end, selected))
                changes.append(Change("BSL_METHOD_CHANGED", "Transferred a local method change.", path, {"method": previous.name}))

    # Content outside methods is merged only with unambiguous source boundaries.
    if ancestor.names == customized.names == vendor.names:
        for base_gap, local_gap, incoming_gap in zip(ancestor.gaps(), customized.gaps(), vendor.gaps(), strict=True):
            selected = _selected(base_gap[2], local_gap[2], incoming_gap[2])
            if selected is None:
                issues.append(_issue("BSL_SCAFFOLD_CONFLICT", "Both sides changed the same text outside methods.", path, severity="conflict"))
            elif selected != incoming_gap[2]:
                patches.append((incoming_gap[0], incoming_gap[1], selected))
                changes.append(Change("BSL_SCAFFOLD_CHANGED", "Transferred a local change outside methods.", path))
    else:
        if customized.scaffold() != ancestor.scaffold() and customized.scaffold() != vendor.scaffold():
            issues.append(_issue("BSL_SCAFFOLD_CONFLICT", "Text outside methods changed alongside method additions, deletions or moves; source anchors are ambiguous.",
                                 path, severity="conflict"))
        # A scaffold contains region directives but deliberately omits methods.
        # Equal scaffolds therefore do not prove equal method placement. Keep
        # incoming moves with local body edits, but never discard a distinct
        # local move when gap boundaries cannot be merged independently.
        shared = set(base_methods) & set(local_methods) & set(incoming_methods)
        base_order = [name for name in ancestor.names if name in shared]
        local_order = [name for name in customized.names if name in shared]
        incoming_order = [name for name in vendor.names if name in shared]
        moved_regions = [name for name in base_order
                         if local_methods[name].region != base_methods[name].region
                         and local_methods[name].region != incoming_methods[name].region]
        if moved_regions or (local_order != base_order and local_order != incoming_order):
            issues.append(_issue("BSL_PLACEMENT_CONFLICT", "Local method moves have ambiguous source anchors and require manual integration.",
                                 path, severity="conflict", methods=moved_regions,
                                 base_order=base_order, local_order=local_order, incoming_order=incoming_order))

    # Insert local additions before the next surviving ancestral neighbour. All
    # incoming additions already in that interval remain before local additions.
    for index, method in enumerate(customized.methods):
        if method.key in base_methods:
            continue
        if method.key in incoming_methods:
            if not _method_equal(method.raw, incoming_methods[method.key].raw):
                issues.append(_issue("BSL_ADD_ADD", "Different methods were added under the same name.",
                                     path, severity="conflict", method=method.name))
            continue
        following = next((item for item in customized.methods[index + 1:]
                          if item.key in base_methods and item.key in incoming_methods
                          and item.region == method.region
                          and incoming_methods[item.key].region == method.region), None)
        if following is not None:
            position = incoming_methods[following.key].start
        elif method.region and method.region in vendor.regions:
            position = vendor.regions[method.region][1]
        elif not method.region and vendor.methods:
            root_methods = [item for item in vendor.methods if not item.region]
            if not root_methods:
                issues.append(_issue("BSL_ADDITION_ANCHOR", "No unambiguous root-level insertion anchor.",
                                     path, severity="conflict", method=method.name))
                continue
            position = root_methods[-1].end
        elif not method.region and not vendor.text.strip():
            position = len(vendor.text)
        else:
            issues.append(_issue("BSL_ADDITION_ANCHOR", "No unambiguous insertion container for a local method.",
                                 path, severity="conflict", method=method.name))
            continue
        patches.append((position, position, _insertion_text(vendor, method, customized, position)))
        changes.append(Change("BSL_METHOD_ADDED", "Transferred a local method after incoming additions at its anchor.",
                              path, {"method": method.name}))
    if any(issue.blocking for issue in issues):
        return MergeResult(None, issues, changes)

    patches.sort(key=lambda item: (item[0], item[1] != item[0]))
    parts = []
    cursor = 0
    for start, end, replacement in patches:
        if start < cursor:
            issues.append(_issue("BSL_PATCH_OVERLAP", "Overlapping source edits require manual integration.", path, severity="conflict"))
            return MergeResult(None, issues, changes)
        parts.extend((vendor.text[cursor:start], replacement))
        cursor = end
    parts.append(vendor.text[cursor:])
    result = vendor.encoded("".join(parts))
    result_issues = inspect_bsl(result, path=path)
    issues.extend(result_issues)
    return MergeResult(None if any(issue.blocking for issue in result_issues) else result, issues, changes)
