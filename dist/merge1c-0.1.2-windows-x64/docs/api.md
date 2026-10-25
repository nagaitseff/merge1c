# Python API

Пакет работает на Python 3.11+ без обязательных сторонних библиотек. Для использования исходного дерева добавьте его `src` в `PYTHONPATH` или установите пакет способом, описанным в README. Основные функции доступны непосредственно из `merge1c`.

Есть три уровня работы:

| Уровень | Вызовы | Результат |
| --- | --- | --- |
| Один файл | `merge_xml`, `merge_bsl`, `inspect_xml`, `inspect_bsl` | Байты кандидата и структурные замечания |
| Три каталога | `merge_directories`, `validate_directory` | Новое дерево файлов без журнала сессии |
| Продолжаемая сессия | `prepare_directories`, `prepare_repository`, `Session` | Снимки входов, кандидат, журнал, ручные решения и внешние проверки |

`base` — общее основание, `local` — ваша доработанная версия, `incoming` — новая поставка или входящая ветка. Перестановка сторон меняет размещение добавлений и выбор текста, на который накладываются правки.

## Результаты и замечания

`Issue(code, message, severity="conflict", path="", details={})` описывает замечание. Его `blocking` равен `True` для `conflict` и `error`. Предупреждение `warning` само по себе не блокирует кандидат. `Change(code, message, path="", details={})` описывает выполненную операцию.

`MergeResult` содержит `content: bytes | None`, `issues` и `changes`. Его свойство `resolved` требует `content is not None` и отсутствия блокирующих замечаний; `b""` допустим. Наличие `content` отдельно от `resolved` не означает успех: XML-движок может вернуть входящий кандидат вместе с конфликтами. При конфликте BSL результат часто содержит `content = None`.

`MergeResult.to_dict()` возвращает `resolved`, SHA-256 содержимого, замечания и изменения. Само содержимое в этот словарь не включается. Сохраняйте `result.content` отдельно.

**`resolved` подтверждает только завершённость встроенного объединения и структурных проверок.** В этих функциях нет запуска 1С, компиляции модулей или функциональных тестов.

## Объединение одного файла

Сигнатуры одинаковы для XML и BSL:

```text
merge_xml(base: bytes, local: bytes, incoming: bytes, *, path: str = "") -> MergeResult
merge_bsl(base: bytes, local: bytes, incoming: bytes, *, path: str = "") -> MergeResult
inspect_xml(data: bytes, *, path: str = "") -> list[Issue]
inspect_bsl(data: bytes, *, path: str = "") -> list[Issue]
```

Аргумент `path` нужен для сообщений; эти функции не читают и не записывают файл по указанному пути. Передавайте исходные байты, чтобы сохранить BOM, окончания строк, комментарии и оформление. Не декодируйте и не сериализуйте XML повторно перед вызовом.

Пример независимых правок XML и разных методов BSL:

```python
from merge1c import merge_bsl, merge_xml

base_xml = b'<Form version="2.20"><A>old</A><B>old</B></Form>\n'
local_xml = base_xml.replace(b"<A>old</A>", b"<A>local</A>")
incoming_xml = base_xml.replace(b"<B>old</B>", b"<B>vendor</B>")
xml_result = merge_xml(base_xml, local_xml, incoming_xml, path="Ext/Form.xml")
assert xml_result.resolved, xml_result.to_dict()
assert xml_result.content == b'<Form version="2.20"><A>local</A><B>vendor</B></Form>\n'

base_bsl = (
    "Процедура Первая()\n    А = 1;\nКонецПроцедуры\n\n"
    "Процедура Вторая()\n    Б = 1;\nКонецПроцедуры\n"
).encode("utf-8")
local_bsl = base_bsl.replace("А = 1".encode(), "А = 2".encode())
incoming_bsl = base_bsl.replace("Б = 1".encode(), "Б = 3".encode())
bsl_result = merge_bsl(base_bsl, local_bsl, incoming_bsl, path="Ext/Module.bsl")
assert bsl_result.resolved, bsl_result.to_dict()
assert "А = 2".encode() in bsl_result.content
assert "Б = 3".encode() in bsl_result.content
```

XML объединяется правками исходных диапазонов входящего текста. Поддерживаются отдельные свойства, известные контейнеры и области ID форм; неизвестная структура не становится автоматически безопасной только потому, что XML синтаксически корректен. Несовпадение типа/UUID корневого объекта и версии формата выгрузки требует решения. Изменение формата выгрузки не выполняется как часть слияния.

BSL обрабатывается ограниченным лексическим анализатором и индексом методов. Строки, комментарии и директивы различаются, поэтому слова `Процедура` внутри строки не создают ложный метод. Отдельные изменения разных методов можно совместить, сохранив порядок входящей версии и привязку своих добавлений. Разные изменения одного метода требуют ручного решения, даже если обычное построчное слияние могло бы их совместить. Изменение сигнатуры существующего метода учитывается как изменение этого метода.

Это не полный парсер или компилятор BSL: не проверяются типы, все конструкции языка, доступность API платформы, запросы, динамический код и поведение обработчиков. Конкурирующие структурные изменения с условными объявлениями методов или неподдержанными директивами отклоняются. Исключительность ветвей одной цепочки `#Если` учитывается при поиске дублей; логическая совместимость произвольных независимых условий не доказывается.

Проверка одного файла без объединения:

```python
from merge1c import inspect_bsl, inspect_xml

issues = inspect_xml(b"<Form><A></Form>", path="Ext/Form.xml")
assert any(issue.blocking for issue in issues)

source = "Процедура Обработать()\nКонецПроцедуры\n".encode("utf-8")
assert not any(issue.blocking for issue in inspect_bsl(source, path="Module.bsl"))
```

## Настройки в Python

```text
load_config(path: str | Path | None = None, *, local_path: str | Path | None = None,
            profile: str = "vendor_update") -> Config
```

`load_config` проверяет TOML, применяет профиль и локальные пути. В отличие от CLI, эта функция не ищет файлы автоматически: передайте оба пути явно. Без `path` используются значения по умолчанию, а `project_root` становится текущим каталогом. При переданном пути `project_root` — каталог именно проектного файла.

Объект `Config` хранит итоговые значения, а не исходную структуру TOML. Основные поля: `profile`, `incoming_role`, `source_roots`, `platform_version`, `project_root`, `session_root`, `git_executable`, `git_minimum`, `manual_tool`, `required_validators`, `require_platform`, `tools`. `session_root` — строка из `[storage] session_root`; её значение по умолчанию — `""`. `tools` сопоставляет имя с `Tool`. Поля `Config` и `Tool` защищены от присваивания; коллекции корней, аргументов и обязательных валидаторов приведены к кортежам, таблица инструментов доступна только для чтения.

`Config.to_dict()` возвращает сериализуемый снимок, а `fingerprint` — его SHA-256. Проверенные TOML-правила предпочтительно получать через `load_config`; прямой конструктор `Config(...)` удобен для собственных интеграций, но не повторяет всю проверку схемы TOML. Подробности схемы и контрактов инструментов — в [configuration.md](configuration.md).

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from merge1c import load_config

with TemporaryDirectory(prefix="merge1c-api-config-", dir=".") as temporary:
    root = Path(temporary).resolve()
    project = root / "merge1c.toml"
    project.write_text(
        'schema_version = 1\n'
        '[project]\nsource_roots = ["src/cf"]\n'
        '[profiles.review]\nincoming_role = "incoming"\n'
        '[profiles.review.validation]\nrequire_platform = false\n',
        encoding="utf-8",
    )
    config = load_config(project, profile="review")
    assert config.source_roots == ("src/cf",)
    assert config.project_root == str(root)
    assert config.incoming_role == "incoming"
    assert config.require_platform is False
    assert len(config.fingerprint) == 64
```

## Три каталога без сессии

```text
merge_directories(base, local, incoming, output, *, config=None, progress=None) -> DirectoryResult
validate_directory(directory, config=None) -> list[Issue]
```

Пути принимают `str` или `pathlib.Path`. Выходной каталог должен быть новым или пустым. Он не может совпадать с входом, находиться внутри входа или содержать его. Исходники не изменяются; после объединения проверяется, не поменялись ли входы во время работы.

Сопоставление перемещённых метаданных использует тип и собственный UUID объекта в соответствующем контейнере выгрузки. Сходство строк XML не служит основанием объявить разные объекты одним. Дубли UUID, коллизии путей, разные переименования и удаление с изменением отмечаются в замечаниях.

`DirectoryResult` содержит `output`, `issues`, `changes`, `files`, `seconds`, `input_hashes`, `file_sources`, `validation_issues`. `file_sources` хранит связь итоговых путей с путями трёх входов, в том числе после перемещения. `resolved` проверяет отсутствие блокирующих замечаний, а `to_dict()` даёт краткий отчёт без входных хешей и полной карты файлов. Счётчик `files` отражает число рассмотренных итоговых путей, а не число автоматически разрешённых конфликтов.

`validate_directory` проверяет выбранные XML/BSL, UUID объектов и простые статические привязки обработчиков `Event`/`Action` из `Ext/Form.xml` к соседнему `Ext/Form/Module.bsl`. Это не анализ всех межмодульных ссылок и не проверка динамически создаваемых полей формы.

Вызов `progress(event)` получает словарь с фазой `scan`, `merge`, `validate` или `complete`. Для `merge` передаются также `path`, `current`, `total`; для `complete` — итоговые счётчики. Обработчик работает синхронно: его исключение прерывает вызов.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from merge1c import merge_directories, validate_directory

with TemporaryDirectory(prefix="merge1c-api-dirs-", dir=".") as temporary:
    root = Path(temporary)
    for side in ("base", "local", "incoming"):
        (root / side).mkdir()
    base = b'<Form version="2.20"><A>old</A><B>old</B></Form>\n'
    (root / "base" / "Form.xml").write_bytes(base)
    (root / "local" / "Form.xml").write_bytes(base.replace(b"<A>old</A>", b"<A>local</A>"))
    (root / "incoming" / "Form.xml").write_bytes(base.replace(b"<B>old</B>", b"<B>vendor</B>"))
    events = []
    result = merge_directories(
        root / "base", root / "local", root / "incoming", root / "output",
        progress=events.append,
    )
    assert result.resolved, result.to_dict()
    assert events[0]["phase"] == "scan"
    assert events[-1]["phase"] == "complete"
    assert not any(issue.blocking for issue in validate_directory(root / "output"))
```

Не используйте этот уровень как замену сессии, если нужно продолжать ручные решения, хранить результаты внешних проверок и затем создавать ветку кандидата. `merge_directories` не запускает настроенные внешние инструменты и не имеет признака платформенной готовности.

## Сессия из каталогов

```text
prepare_directories(base, local, incoming, session=None, *, config=None, progress=None) -> Session
Session(directory) -> Session
```

Функция `prepare_directories` создаёт новую сессию. Метод `Session.prepare` отсутствует: `Session(path)` открывает уже подготовленный каталог. Сессия должна быть отдельной от всех трёх входов и не существовать до вызова.

Аргумент `session` необязателен. Без него создаётся уникальный подкаталог `config.session_root`; пустая настройка выбирает `merge1c-sessions` внутри системного временного каталога. Относительный `session_root` считается от `config.project_root`. Явный `session` имеет приоритет; относительный путь этого аргумента считается от текущего рабочего каталога. Фактически выбранный абсолютный путь доступен как `session.path`.

До копирования входов проверяются реальные операции в выбранном месте хранения: создание, запись, чтение, замена, переименование и удаление. При отказе автоматического перехода на другой каталог нет. Правила доступа не меняются. Подробности и самостоятельная проверка через CLI описаны в [справочнике настроек](configuration.md#каталог-сессий-и-права-доступа).

В `inputs/base`, `inputs/local`, `inputs/incoming` сохраняются копии источников; `candidate` содержит результат. `session.json` хранит снимок настроек и состояние, `report.json` — сводку, `events.jsonl` — журнал событий. Не меняйте сохранённые входы и служебный JSON вручную. Для ручного решения редактируйте файл в `candidate` и принимайте его через `resolve`.

Пример создаёт обычную сессию с обязательной платформенной проверкой, но не запускает 1С:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from merge1c import Session, prepare_directories

with TemporaryDirectory(prefix="merge1c-api-session-", dir=".") as temporary:
    root = Path(temporary)
    for side in ("base", "local", "incoming"):
        (root / side).mkdir()
        (root / side / "Form.xml").write_bytes(b'<Form version="2.20"/>\n')
    session = prepare_directories(
        root / "base", root / "local", root / "incoming", root / "session",
    )
    report = session.check()
    assert report["structural_checks"] == "passed"
    assert report["platform"] == "not_run"
    assert report["status"] == "prepared"
    assert report["ready"] is False
    reopened = Session(root / "session")
    assert reopened.candidate == session.candidate
```

### Проверки и готовность

`session.report()` возвращает сводку, не запуская внешние команды. `session.check(run_tools=False)` повторяет встроенную проверку и сохраняет состояние. С `run_tools=True` дополнительно запускаются обязательные валидаторы и, при `require_platform=True`, адаптеры платформы.

Основные поля отчёта:

| Поле | Значения и смысл |
| --- | --- |
| `status` | `conflicts` — есть блокирующие замечания; `prepared` — проверки ещё не позволяют допуск; `ready` — выполнены настроенные условия |
| `structural_checks` | `pending`, `failed` или `passed` |
| `candidate_current` | Проверено ли текущее содержимое кандидата |
| `inputs_current` | Совпадают ли сохранённые входы с исходными хешами |
| `required_tools` | `passed` или `pending` |
| `platform` | `passed` либо `not_run`: нет принятого актуального результата платформы |
| `stale_tools` | Инструменты, чья идентичность изменилась после проверки |
| `ready` | Логический итог всех настроенных условий |
| `issues`, `changes`, `resolutions`, `tools` | Замечания, действия, ручные решения, результаты внешних запусков |

`platform = "not_run"` также остаётся при отклонённом, устаревшем или не относящемся к текущему кандидату результате; подробности находятся в `tools`. Для принятой платформенной проверки нужны код возврата 0, JSON `status = "passed"` и точное совпадение `platform_version` с настройкой проекта.

После изменения кандидата прежние внешние результаты теряют силу. Также учитывается изменение исполняемого файла и отслеживаемых файлов аргументов инструмента. Проверка не фиксирует автоматически всё внешнее окружение; границы этого контроля описаны в справочнике настроек.

Для осознанного статического ревью можно передать `Config(require_platform=False)`. Тогда `ready` может стать `True` без 1С, если пройдены встроенные и обязательные проверки. Это позволяет и ветку статического кандидата в Git-сессии, но не означает платформенную проверку. Не заменяйте этой настройкой фактическую проверку конфигурации, которую планируется использовать.

### Ручное решение

```text
session.resolve(relative_path, *, take=None, tool_name=None) -> dict
```

Должен существовать нерешённый файловый конфликт по указанному пути. Выберите ровно один способ:

- `take="base"`, `"local"` или `"incoming"` — принять сохранённую сторону; если на этой стороне файл отсутствовал, принять удаление.
- `take="candidate"` — принять текущую ручную правку файла в `candidate`.
- `tool_name="имя"` — запустить настроенный resolver, который создаст отдельный результат.

Путь относительный, внутри кандидата, с разделителями `/` или `\`; выход через `..`, абсолютный путь и буква диска запрещены. Python API требует явное `tool_name`: выбор `manual_tool` по умолчанию выполняет CLI при `resolve --tool`.

Решение снимает замечания слияния для этого файла, записывает историю с хешами и сбрасывает прежние внешние проверки. Затем выполняется `check()`; неверная структура результата остаётся блокирующей. `take="candidate"` — явное утверждение вашего ручного решения, а не автоматическое доказательство его правильности.

## Сессия из Git

```text
prepare_repository(repository, incoming, session=None, *, config=None, progress=None) -> Session
session.create_branch(branch, *, message="Объединение исходников 1С") -> str
```

`prepare_repository` использует закоммиченный `HEAD` как `local`, указанный ref/коммит как `incoming` и единственное общее основание Git как `base`. Незакоммиченные файлы не включаются; о таком состоянии выдаётся предупреждение. Истории без общего основания и с несколькими основаниями сейчас отклоняются. Требуется Git не ниже настроенной версии, минимум 2.50.

Каталог сессии должен быть новым и находиться вне исходного репозитория. Выбор необязательного `session` и проверка места хранения работают так же, как у `prepare_directories`. Дополнительно перед экспортом в `progress` передаётся событие `{"phase": "storage", "session": "абсолютный путь", "directory": "родительский каталог"}`. Это событие относится к подготовке Git-сессии; `merge_directories` его не выдаёт.

Готовая сессия проходит тот же цикл разрешения и проверки. `create_branch` требует `ready`, неизменившуюся исходную вершину `HEAD` и новое допустимое имя ветки; возвращает OID созданного коммита. Исходная ветка и рабочие файлы не переключаются на кандидат. Сессия, подготовленная из трёх каталогов, этого метода создания Git-ветки не поддерживает.

Сессии 0.1.1, в снимке настроек которых ещё нет `session_root`, открываются с сохранением проверки прежнего хеша. Новое значение каталога не добавляется в старый снимок как причина отказа. Текущие настройки из TOML по-прежнему не подменяют зафиксированные правила существующей сессии.

## Внешние инструменты для авторов адаптеров

Низкоуровневый запуск доступен из `merge1c.tools`:

```text
run_tool(tool: Tool, values: dict[str, str], *, cwd: Path) -> ToolResult
tool_identity(tool: Tool, values: dict[str, str], *, cwd: Path) -> dict
```

`Tool` импортируется из `merge1c`. `ToolResult` содержит `status` (`passed`, `failed`, `timeout`), `command`, `returncode`, `seconds`, `stdout`, `stderr`, `report`, `identity`, `cwd`; метод `to_dict()` сериализует эти поля. `returncode` может отсутствовать при ошибке запуска или тайм-ауте. Эти функции не входят в список экспортов верхнего уровня пакета.

Сам по себе `run_tool` не проверяет весь кандидат, неизменность входных снимков или версию платформы. Эти действия выполняет `Session`. Для штатного процесса используйте `session.check(run_tools=True)` и `session.resolve(..., tool_name=...)`.

## Ошибки и границы применения

Ожидаемые ошибки настроек представлены `ConfigError`, работы с каталогами — `MergeError`, состояния сессии — `SessionError`, операций Git — `GitError`, выбора или проверки места хранения — `StorageError`. Все эти классы экспортированы из `merge1c`; `StorageError`, как и `ConfigError`, наследует `ValueError`. Ошибки файловой системы также могут выходить как стандартный `OSError`. Конфликт содержимого обычно возвращается в `issues`, а не исключением.

Ссылки файловой системы, Windows junction и имена, различающиеся только регистром, отклоняются. Из снимков исключается только служебное имя `.git`. Остальные файлы, включая пути `.merge1c` и `__pycache__`, сохраняются: правила игнорирования самого Python-проекта не применяются к пользовательским исходникам. Неизвестные двоичные форматы не разбираются: односторонняя правка переносится, разные одновременные изменения требуют выбора файла.

Примеры выше проверяют Python API на небольших самостоятельных данных. Они не запускают реальные 1С и Beyond Compare и не подтверждают корректность конкретной ERP, СППР, формы, СКД или конфигурации после обновления.
