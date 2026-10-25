"""A small, self-contained example of form and module integration."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from merge1c import Config, prepare_directories


def form(extra: str = "") -> bytes:
    own = '<InputField name="НашеПоле" id="2"><DataPath>НашРеквизит</DataPath></InputField>' if extra == "local" else ""
    vendor = '<InputField name="ТиповоеПоле" id="2"><DataPath>ТиповойРеквизит</DataPath></InputField>' if extra == "incoming" else ""
    attribute = '<Attribute name="НашРеквизит" id="2"/>' if extra == "local" else '<Attribute name="ТиповойРеквизит" id="2"/>' if extra == "incoming" else ""
    text = f'''<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">
    <!-- Типовая структура формы -->
    <Events><Event name="OnOpen">ПриОткрытии</Event></Events>
    <ChildItems>
        <InputField name="НачальноеПоле" id="1"><DataPath>НачальныйРеквизит</DataPath></InputField>
        {vendor}{own}
    </ChildItems>
    <Attributes>
        <Attribute name="НачальныйРеквизит" id="1"/>
        {attribute}
    </Attributes>
</Form>
'''
    return b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")


def module(side: str) -> bytes:
    value = "2" if side == "incoming" else "1"
    text = f'''#Область ОбработчикиСобытийФормы

&НаКлиенте
Процедура ПриОткрытии(Отказ)
    Значение = {value};
КонецПроцедуры

'''
    if side == "local":
        text += "&НаКлиенте\nПроцедура НашаКоманда()\n    Значение = 10;\nКонецПроцедуры\n\n"
    text += "#КонецОбласти\n"
    return b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")


def run(root: Path) -> Path:
    root = root.resolve()
    if root.exists():
        raise ValueError(f"Нужен новый каталог: {root}")
    root.mkdir(parents=True)
    for side in ("base", "local", "incoming"):
        location = root / side / "CommonForms/Пример/Ext"
        location.mkdir(parents=True)
        (location / "Form.xml").write_bytes(form(side))
        (location / "Form").mkdir()
        (location / "Form/Module.bsl").write_bytes(module(side))
    session = prepare_directories(root / "base", root / "local", root / "incoming", root / "session",
                                  config=Config(require_platform=False))
    report = session.report()
    if not report["ready"]:
        raise RuntimeError(report["issues"])
    candidate = session.candidate / "CommonForms/Пример/Ext/Form.xml"
    xml = ET.fromstring(candidate.read_bytes())
    fields = [node.attrib["name"] for node in xml.find("{*}ChildItems")]
    assert fields == ["НачальноеПоле", "ТиповоеПоле", "НашеПоле"]
    ids = [node.attrib["id"] for node in xml.find("{*}ChildItems")]
    assert len(ids) == len(set(ids))
    assert b"\r\n" in candidate.read_bytes() and candidate.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "<!-- Типовая структура формы -->".encode("utf-8") in candidate.read_bytes()
    return session.path


def main() -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Небольшой пример объединения формы и BSL")
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    root = options.output or Path(tempfile.mkdtemp(prefix="merge1c-demo-parent-")) / "example"
    try:
        session = run(root)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Ошибка: {exc}")
        return 1
    print(f"Сессия: {session}")
    print(f"Результат: {session / 'candidate'}")
    print("Типовое поле осталось выше нашего. Коллизия ID согласована. Комментарии, BOM и CRLF сохранены.")
    print("Выполнены структурные проверки искусственного примера. Платформа 1С не запускалась.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

