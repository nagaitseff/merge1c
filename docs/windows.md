# PowerShell и cmd

Команды merge1c работают в Windows PowerShell 5.1, PowerShell 7 и cmd.exe. Объединение выполняет Python, поэтому выбор оболочки не меняет правила XML, BSL и Git. Различаются запись путей, получение кода завершения и обработка вывода самой оболочкой.

## Что проверено

Проверки проведены на Windows 11, Python 3.12.14, Windows PowerShell 5.1.26100.9711 и PowerShell 7.6.5. Для cmd использован штатный cmd.exe этой системы.

| Проверка | Windows PowerShell 5.1 | PowerShell 7.6.5 | cmd |
|---|---|---|---|
| Запуск `python -m merge1c` | Да | Да | Да |
| Полное объединение, затем `check` | Да | Да | Да |
| Пробелы, кириллица и `&` внутри пути в кавычках | Да | Да | Да |
| Сохранение BOM, CRLF и исходных файлов | Да | Да | Да |
| Коды 0, 1 и 2 | Да | Да | Да |
| JSON с кириллицей при старой кодовой странице 866 | Да, захват через оболочку | Да, захват через оболочку | Да, перенаправление |
| Внешний Python-валидатор, путь с кириллицей и пробелами | Да | Да | Да |

Тесты находятся в `tests/test_shells.py`. Они создают отдельные входы и сессии, сравнивают байты результата и запускают настоящие оболочки. Если оболочка отсутствует, её проверка пропускается. Испытаний на всех выпусках PowerShell 7 не было.

Запуск установленного консольного файла `merge1c.exe --version` отдельно проверен в Windows PowerShell 5.1 и PowerShell 7. Работа настоящего Beyond Compare остаётся отдельной проверкой; здесь проверялся протокол запуска внешних команд.

## Обычные команды одинаковы

Сначала [установите пакет](installation.md) командой `python -m pip install` из готового wheel или каталога проекта. Одна запись в PATH не делает `python -m merge1c` доступным.

Если Python доступен под именем `python`, эти строки подходят для всех трёх оболочек:

```text
python -m merge1c --version
python -m merge1c inspect "C:\Исходники 1С\Выгрузка"
python -m merge1c prepare master --repo "C:\Репозитории\ERP" --profile develop_sync --session "C:\Сессии объединения\develop"
```

После установки можно писать `merge1c` вместо `python -m merge1c`. Первый вариант зависит от доступности каталога Scripts в PATH. Команда `python --version` должна показывать установленный Python 3.11 или новее.

Если путь к исполняемому файлу указан явно и содержит пробелы, PowerShell требует оператор вызова `&`:

```powershell
& "C:\Program Files\Python312\python.exe" -m merge1c --version
```

В cmd оператор перед путём не нужен:

```bat
"C:\Program Files\Python312\python.exe" -m merge1c --version
```

Это различие относится к [правилам разбора PowerShell](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing) и [cmd](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd), а не к механизму объединения.

## Как проверять результат команды

Успешная подготовка сессии ещё не означает прохождение обязательных платформенных проверок. Для автоматизации используйте `check`, как описано в [порядке работы](usage.md).

В PowerShell сохраните код сразу после запуска:

```powershell
python -m merge1c check "C:\Сессии объединения\develop" --run-tools
$mergeExitCode = $LASTEXITCODE
if ($mergeExitCode -ne 0) {
    throw "Проверка merge1c не завершена успешно. Код: $mergeExitCode"
}
```

В интерактивном cmd код показывает следующая строка:

```bat
python -m merge1c check "C:\Сессии объединения\develop" --run-tools
echo %ERRORLEVEL%
```

В сценарии `.cmd` можно передать код вызывающему процессу:

```bat
python -m merge1c check "C:\Сессии объединения\develop" --run-tools
exit /b %ERRORLEVEL%
```

Не оборачивайте получение `%ERRORLEVEL%` в общий блок скобок вместе с предыдущей командой: cmd может раскрыть переменную до её выполнения. В PowerShell само по себе `$ErrorActionPreference = 'Stop'` не заменяет проверку `$LASTEXITCODE` внешней программы.

## JSON и кодировки

Машинный JSON в stdout использует ASCII-совместимую запись: русские буквы могут быть представлены как `\u0410`. JSON-парсер восстанавливает обычную строку. Это защищает имена и пути от неверного декодирования в старых настройках консоли. Файлы сессии, включая `report.json`, сохраняются программой в UTF-8 с читаемым русским текстом.

В PowerShell можно сразу разобрать вывод:

```powershell
$report = python -m merge1c status "C:\Сессии объединения\develop" --json | ConvertFrom-Json
$report.ready
```

Для чтения сохранённого отчёта укажите его кодировку:

```powershell
$report = Get-Content -LiteralPath "C:\Сессии объединения\develop\report.json" -Raw -Encoding UTF8 | ConvertFrom-Json
```

Windows PowerShell 5.1 при `>` создаёт UTF-16LE. Поэтому `merge1c ... --json > report.json` в этой оболочке не гарантирует файл UTF-8, даже если сама программа выводит UTF-8. Это поведение описано в [документации Microsoft о кодировках](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding). PowerShell 7.4 и новее [сохраняет байты stdout внешней программы](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_redirection) при таком перенаправлении.

Для файла UTF-8 проще использовать готовый `report.json` из сессии. Если нужен отдельный файл из stdout в PowerShell 5.1, используйте `Set-Content -Encoding UTF8`; этот вариант добавляет BOM. В cmd перенаправление `>` сохраняет байты, полученные от программы.

При захвате обычных русских сообщений через PowerShell можно задать UTF-8 в текущем сеансе, не меняя профиль и системные настройки:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
```

Исходные XML и BSL программа читает и записывает как байты; кодовая страница терминала не меняет их содержимое. Собственные `.ps1` с кириллицей для Windows PowerShell 5.1 сохраняйте в UTF-8 с BOM. Это правило для текста сценария, а не указание перекодировать выгрузку 1С.

### Сообщения о ходе работы

Прогресс идёт в stderr отдельно от итогового stdout. Windows PowerShell 5.1 может оформлять такой поток как `NativeCommandError`, добавляя имя программы и сведения о строке сценария. При захвате вывода и `$ErrorActionPreference = 'Stop'` даже обычное сообщение о ходе работы может остановить сценарий. Это особенность [обработки внешних программ PowerShell](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_preference_variables), а не признак конфликта исходников.

Поэтому не используйте файл, полученный через `2>` или `2>&1` в Windows PowerShell 5.1, как гарантированно чистый JSONL для `--json-progress`. Для машинного чтения используйте `events.jsonl` сессии или запускайте Python API с прямым захватом stdout/stderr. При написании старого PowerShell-сценария учитывайте его режим обработки ошибок и отдельно проверяйте код внешней программы.

В автоматическом сценарии можно отключить только вывод прогресса:

```powershell
$report = python -m merge1c prepare master --repo "C:\Репозитории\ERP" --session "C:\Сессии объединения\develop" --quiet --json | ConvertFrom-Json
$mergeExitCode = $LASTEXITCODE
```

`--quiet` не отключает сохранение `events.jsonl` и не убирает итоговый отчёт. Этот режим проверен с `$ErrorActionPreference = 'Stop'` в Windows PowerShell 5.1. Одновременно указывать `--quiet` и `--json-progress` нельзя.

## Внешние программы

Пути инструментов и каждый аргумент задаются отдельно в TOML. Внутри аргумента не нужны дополнительные кавычки вокруг пути с пробелами. Библиотека передаёт список аргументов непосредственно процессу, без построения команды PowerShell или cmd. Такой способ соответствует [рекомендациям Python по запуску процессов](https://docs.python.org/3/library/subprocess.html).

Примеры настроек находятся в [справочнике](configuration.md). Выбор оболочки, из которой запущен merge1c, не меняет порядок четырёх файлов, передаваемых Beyond Compare.

Прямой запуск `.bat` и `.cmd` в качестве `Tool.executable` не поддерживается: Windows может неявно передать такую команду cmd и изменить смысл аргументов. Для своей обёртки укажите явный интерпретатор, например `python.exe` с файлом `.py` или `powershell.exe` с `-File` и файлом `.ps1`. Интерпретатор и сценарий должны быть известными программами проекта; пользовательские пути по-прежнему передаются отдельными аргументами.
