"""Synthetic XML merge benchmark; it does not predict an ERP update time.

Run from a checkout: python examples/benchmark.py --records 1000 5000
Inputs are generated in memory. No repositories or configuration files change.
Wall time is measured without tracemalloc. A separate run records traced peak
allocations (excluding the already constructed input byte strings).
"""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import platform
import sys
import time
import tracemalloc


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from merge1c.xmlmerge import merge_xml
from merge1c.xmlsource import XMLSource


FORM_NAMESPACES = {
    "": "http://v8.1c.ru/8.3/xcf/logform",
    "app": "http://v8.1c.ru/8.2/managed-application/core",
    "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
    "dcscor": "http://v8.1c.ru/8.1/data-composition-system/core",
    "dcssch": "http://v8.1c.ru/8.1/data-composition-system/schema",
    "dcsset": "http://v8.1c.ru/8.1/data-composition-system/settings",
    "ent": "http://v8.1c.ru/8.1/data/enterprise",
    "lf": "http://v8.1c.ru/8.2/managed-application/logform",
    "style": "http://v8.1c.ru/8.1/data/ui/style",
    "sys": "http://v8.1c.ru/8.1/data/ui/fonts/system",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "v8ui": "http://v8.1c.ru/8.1/data/ui",
    "web": "http://v8.1c.ru/8.1/data/ui/colors/web",
    "win": "http://v8.1c.ru/8.1/data/ui/colors/windows",
    "xr": "http://v8.1c.ru/8.3/xcf/readable",
    "xs": "http://www.w3.org/2001/XMLSchema",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


def _field(name: str, attribute: str, first_id: int) -> str:
    return (
        f'\t\t<InputField name="{name}" id="{first_id}">\n'
        f'\t\t\t<DataPath>{attribute}</DataPath>\n'
        '\t\t\t<Width>20</Width>\n'
        f'\t\t\t<ContextMenu name="{name}КонтекстноеМеню" id="{first_id + 1}"/>\n'
        f'\t\t\t<ExtendedTooltip name="{name}РасширеннаяПодсказка" id="{first_id + 2}"/>\n'
        '\t\t</InputField>\n'
    )


def _attribute(name: str, identifier: int) -> str:
    return (
        f'\t\t<Attribute name="{name}" id="{identifier}">\n'
        '\t\t\t<Type><v8:Type>xs:string</v8:Type></Type>\n'
        '\t\t</Attribute>\n'
    )


def make_inputs(records: int, additions: int = 20) -> tuple[bytes, bytes, bytes]:
    """N fields + N attributes; each side independently adds colliding IDs.

    Every field also has ContextMenu and ExtendedTooltip IDs in the UI scope.
    Both sides add their records into the same middle sibling gap. The fixture
    uses Cyrillic identifiers, UTF-8 BOM, tabs, and CRLF like Designer exports.
    """
    if records < 1 or additions < 1:
        raise ValueError("records and additions must be positive")
    fields = [_field(f"Поле{i:05}", f"Реквизит{i:05}", 3 * i + 1) for i in range(records)]
    attributes = [_attribute(f"Реквизит{i:05}", i + 1) for i in range(records)]

    def build(prefix=""):
        own_fields, own_attributes = [], []
        if prefix:
            for i in range(additions):
                own_fields.append(_field(f"{prefix}Поле{i:05}", f"{prefix}Реквизит{i:05}", 3 * records + 3 * i + 1))
                own_attributes.append(_attribute(f"{prefix}Реквизит{i:05}", records + i + 1))
        middle = records // 2
        namespaces = ' '.join((f'xmlns:{prefix}' if prefix else 'xmlns') + f'="{uri}"'
                              for prefix, uri in FORM_NAMESPACES.items())
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Form {namespaces} version="2.20">\n'
            '\t<!-- Synthetic fixture: existing vendor bytes must survive -->\n'
            '\t<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>\n'
            '\t<ChildItems>\n' + ''.join(fields[:middle] + own_fields + fields[middle:]) + '\t</ChildItems>\n'
            '\t<Attributes>\n' + ''.join(attributes[:middle] + own_attributes + attributes[middle:]) + '\t</Attributes>\n'
            '</Form>\n'
        )
        return b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")

    return build(), build("Наше"), build("Типовое")


def without_local_additions(content: bytes) -> bytes:
    """Strip only the accepted synthetic local nodes to audit incoming bytes."""
    source = XMLSource(content)
    spans = []
    for collection in source.root.children:
        if collection.name not in {"ChildItems", "Attributes"}:
            continue
        for child in collection.children:
            if not child.value("name").startswith("Наше"):
                continue
            indent = source.line_indent(child.start)
            start = child.start - len(indent or b"")
            end = child.end
            if content[end:end + 2] == b"\r\n":
                end += 2
            elif content[end:end + 1] == b"\n":
                end += 1
            spans.append((start, end))
    result = []
    previous = 0
    for start, end in sorted(spans):
        result.append(content[previous:start])
        previous = end
    result.append(content[previous:])
    return b"".join(result)


def run_case(records: int, additions: int = 20) -> dict:
    base, local, incoming = make_inputs(records, additions)
    gc.collect()
    started = time.perf_counter()
    result = merge_xml(base, local, incoming, path="synthetic/Ext/Form.xml")
    wall = time.perf_counter() - started
    if not result.resolved:
        raise RuntimeError(json.dumps(result.to_dict(), ensure_ascii=False))
    preserved = without_local_additions(result.content) == incoming
    if not preserved:
        raise RuntimeError("An unrelated incoming byte changed in the synthetic fixture")
    remaps = sum(change.code == "xml.id_remapped" for change in result.changes)
    gc.collect()
    tracemalloc.start()
    traced_started = time.perf_counter()
    measured = merge_xml(base, local, incoming, path="synthetic/Ext/Form.xml")
    traced_seconds = time.perf_counter() - traced_started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if not measured.resolved or measured.content != result.content:
        raise RuntimeError("The measured run did not produce the same resolved bytes")
    return {
        "records_per_collection": records,
        "local_additions_per_collection": additions,
        "incoming_additions_per_collection": additions,
        "incoming_bytes": len(incoming),
        "result_bytes": len(result.content),
        "seconds_without_tracing": round(wall, 6),
        "seconds_with_tracemalloc": round(traced_seconds, 6),
        "peak_traced_mib": round(peak / 1024 / 1024, 3),
        "local_id_remaps": remaps,
        "incoming_bytes_preserved_outside_local_insertions": preserved,
        "resolved": result.resolved,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="+", type=int, default=[1000, 5000])
    parser.add_argument("--additions", type=int, default=20)
    arguments = parser.parse_args(argv)
    print(json.dumps({"benchmark": "synthetic_xml_only", "python": platform.python_version(),
                      "platform": platform.platform(),
                      "memory": "tracemalloc peak during merge; prebuilt inputs excluded",
                      "scope": "No Git, file scan, 1C platform loading, database update, or business tests"},
                     ensure_ascii=False), flush=True)
    for records in arguments.records:
        print(json.dumps(run_case(records, arguments.additions), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
