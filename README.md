# coomi-kimi — Coomi-equivalent coding agent on top of Kimi Code

Мозг и tool-loop — **Kimi Code 0.42.0** (native binary). Всё остальное — тонкий
хост на Python: ACP-клиент, MCP-сервер с инструментами и веб-морда. Никакого
своего «агентского» LLM-цикла здесь нет и быть не должно: он принадлежит Kimi.

```
        ┌────────────── UI (web/chat, REPL, JSON-RPC clients) ──────────────┐
        │                    aiohttp: / /ws /rpc /api/* /decide             │
        └───────────────▲───────────────────────────────▲───────────────────┘
                        │ session/update stream         │ decisions
                 ┌──────┴───────────────┐               │
                 │  kimi_agent.rpc/.server │            │
                 │  + DecisionBroker      │◄── request_permission ──┐
                 │  + FileService         │◄── fs/*, terminal/* ────┤ reverse ACP
                 │  + TerminalService     │                          │
                 └──────┬────────────────┘                          │
                        │ ACP over stdio (JSON-RPC 2.0)             │
                 ┌──────▼───────────────────────┐                   │
                 │  kimi acp   (Kimi Code 0.42) │── tool calls ─────┘
                 └──────┬───────────────────────┘
                        │ MCP (streamable HTTP, 127.0.0.1)
                 ┌──────▼───────────────────────┐
                 │  kimi_agent.mcp_host + tools/│  34 Coomi-инструмента
                 └──────────────────────────────┘
```

## Нужен ли ACP

Да, и это не галочка «для совместимости» — ACP выбран как единственный транспорт,
который даёт **обе** стороны канала:

1. **Прямой запрос** (мы → Kimi): `initialize`, `session/new|prompt|cancel|…`.
2. **Обратный запрос** (Kimi → нам): `session/request_permission`,
   `fs/read_text_file`, `fs/write_text_file`, `terminal/*`, `elicitation/create`.

Именно обратная сторона и есть «функции Coomi»: без approvals, собственного
файлового sandbox и собственных terminal-вызовов остаётся только печатать то, что
Kimi и так напечатал. Альтернативы зафиксированы как запасные:

* `kimi web` (REST) — меньше кода, но reverse-RPC там другие/ограниченные;
* `kimi -p --output-format stream-json` — для батчей и CI, однопоточный, без
  interactивных решений.

`kimi acp` отдаёт нативные для Kimi `session/update`-события, поэтому UI не нужно
никто и ничто эмулировать.

## Установка

1. **Kimi Code** (не legacy `kimi-cli`) — бинарь в `~/.kimi-code/bin/kimi`,
   провайдер в `~/.kimi-code/config.toml`:

   ```toml
   default_model = "myprefix/model-name"
   default_permission_mode = "manual"
   telemetry = false

   [providers.myprefix]
   type = "openai"
   base_url = "https://example.com/v1"
   api_key  = "sk-..."

   [models."myprefix/model-name"]
   provider = "myprefix"
   model = "model-name"
   max_context_size = 204800   # РЕАЛЬНЫЙ лимит апстрима, не витринное число
   max_output_size = 8192
   capabilities = ["thinking", "image_in", "tool_use"]
   reasoning_key = "reasoning_content"
   ```

   `max_context_size` завышать нельзя: при 256000 против фактических 204800 Kimi
   ронял ход с `exceeds the model max token limit of 204800`, а выглядело это как
   «обрыв стрима у провайдера».

2. **Хост**: `./run.sh` — сам создаст `.venv` (uv или venv+pip) и поставит
   `requirements.txt`. Вручную:

   ```bash
   uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
   ```

3. **Профиль и MCP для Kimi**: `./run.sh install` кладёт
   `config/coomi-agent.md` → `~/.kimi-code/agents/coomi.md` и `config/mcp.json` →
   `~/.kimi-code/mcp.json`. Формат последней секции — `transport: "http"` **без**
   поля `type`.

## Запуск

```bash
./run.sh                      # веб-консоль на http://127.0.0.1:8765
./run.sh chat                 # тот же контур в терминале (rich)
./run.sh ask "собери список файлов в /tmp"
./run.sh doctor               # версии, capabilities, методы, состояние сессий
PERMISSION=manual ./run.sh    # спрашивать перед каждым tool-вызовом
```

Хост по умолчанию слушает `127.0.0.1`: в proot-контейнере наружу торчит только
петля, и перевести адрес можно только осознанно (`COOMI_KIMI_HOST`).

## Функции Coomi → где они реализованы

| Функция Coomi | Здесь | Механизм |
|---|---|---|
| tool-loop, план ходов, retry | Kimi Code | нативный цикл агента |
| память (`memory_*`) | `tools/`: 5 инструментов | JSON-хранилище с precedence local → project → global |
| план (`update_plan`) | `tools/update_plan` + `get_plan` | публикуется как ACP `plan`-событие, UI рисует панель |
| суб-агенты (`spawn_agent`) | `tools/spawn_agent/wait_agent/close_agent/agent_status` | **отдельные ACP-сессии того же Kimi**, со своим профилем |
| skills | `list_skills/read_skill/create_skill` | каталог `~/.coomi/skills` + профильный каталог агента |
| workflows | `create/list/get/save/delete/run_workflow` | валидация шагов и зависимостей, последовательный прогон |
| автономные циклы | `create_loop/get_loop/update_loop/list_loops/run_loop_turn` | objective + бюджет токенов/ходов |
| approvals / вопросы | `client.DecisionBroker` | reverse `session/request_permission` + `elicitation/create` |
| файлы, sandbox | `client.FileService` | `fs/*` поверх allowlist корневых каталогов |
| терминал | `client.TerminalService` | `terminal/*`: реальные подпроцессы, polling-стриминг, kill/release |
| картинки | `show_image`/`view_image` + ACP image block | в чат идёт `ContentBlock(image)`, модель видит их при `image_in` |
| web | `web_search` (Bing/DDG, общий бюджет 20 s) | `FetchURL` уже встроен в Kimi |
| телефонный file picker | `import_file` / `export_file` | inbox-каталог, который читает хост-слой |
| UI | `web/` + `/ws` | стрим текста/мыслей, tool-карточки, diff, plan, usage, кнопки решений |
| RPC для клиентов | `rpc.py` | своя ACP-лексика + `coomi/*` (22 метода) |

Итого 34 инструмента, advertise-ятся как MCP-сервер `coomi`, имена доходят до
модели в виде `mcp__coomi__<tool>`.

## Политики разрешений

`PERMISSION=auto-safe` (по умолчанию) · `manual` · `auto-all`/`yolo`.

Kimi в `session/request_permission` присылает **только** `title` = голое имя
инструмента (`"Bash"`) и человекочитаемый `content` (`"Requesting approval to
Running: …"`) — поля `kind` в его пейлоаде нет
(`packages/acp-server/src/approval.ts::buildPermissionToolCallUpdate`). Поэтому
хост:

* выводит `kind` из имени инструмента (`Bash`→execute, `Read`→read,
  `Write|Edit`→edit, `Glob|Grep`→search, `FetchURL`→fetch; реестр имён —
  `packages/agent-core-v2/src/agent/tools/**`);
* достаёт саму **команду** из `content` (снимая служебную фразу) и принимает
  решение по ней, а не по заголовку;
* **никогда** не считает отсутствующий `kind` безопасным.

Дальше:

* read-only команды (`ls`, `cat`, `git status`…) проходят молча;
* `cmd && rm -rf /` строгий по самому слабому сегменту; перенаправления, пайпы,
  `$(…)`, `python -c`, `npm`, `git push`, `curl -o` — всегда к человеку;
* «Always allow» для `Bash` запоминает **команду**, а не инструмент, иначе одно
  одобрение `ls` открыло бы и `rm -rf /`;
* собственные MCP-инструменты разложены по классам риска:
  read-only (`memory_search`, `web_search`, `list_skills`, …) — свободны;
  пишущие состояние (`memory_write`, `update_plan`, `create_skill`, …) — allow с
  запоминанием на сессию;
  `spawn_agent`, `close_agent`, `run_workflow`, `run_loop_turn` — только человек;
  `submit_answer` — **никогда** автоматически и без права на grant: этот инструмент
  отвечает за человека, и разрешив его один раз, модель начала бы сама одобрять
  все свои последующие permissions.

Один прогон на живой модели это подтверждает (`scripts/check_autosafe_gate.py`):
`ls -la /etc` не спросили, `touch /tmp/…` встал в очередь с
`kind=execute, detail="touch /tmp/gate_written_marker"`, после `approve_once`
файл появился, `mcp__coomi__memory_search` прошёл без трения.

MCP-вызовы, кстати, проходят через ту же калитку — это проверено отдельно
(`scripts/check_mcp_gated.py`, policy=manual): на `mcp__coomi__list_skills`
приходит approval с `kind="other"`. Без этого градация инструментов была бы
мёртвым кодом.

## Проверки

```bash
export COOMI_KIMI_HOME=$(mktemp -d)
./.venv/bin/python -m pytest tests/ -q          # 123 unit-тестов (политика, sandbox,
                                               # terminal, store, tools, events)
./.venv/bin/python scripts/mutation_check.py    # каждая починенная дыра вставляется
                                               # обратно: набор обязан падать (10/10)
PERMISSION=auto-safe COOMI_KIMI_PORT=8765 ./run.sh &
COOMI_KIMI_URL=http://127.0.0.1:8765 ./.venv/bin/python scripts/check_autosafe_gate.py
PERMISSION=manual COOMI_KIMI_PORT=8775 COOMI_KIMI_BRIDGE_PORT=8776 ./run.sh &
COOMI_KIMI_URL=http://127.0.0.1:8775 ./.venv/bin/python scripts/smoke_decide.py
./.venv/bin/python scripts/smoke_bridge.py       # контракт ACP на живом Kimi
./.venv/bin/python scripts/smoke_e2e_mcp.py      # модель сама зовёт mcp__coomi__*
```

```bash
# нужен сервер под manual; доказывает, что градация MCP-инструментов — живой
# контроль, а не мёртвый код
COOMI_KIMI_URL=http://127.0.0.1:8775 ./.venv/bin/python scripts/check_mcp_gated.py
```

`smoke_bridge` засчитывает обратный `terminal/*`-вызов и параллельные сессии,
`smoke_decide` — approve/reject/вопрос через `coomi/decide`,
`smoke_e2e_mcp` — что advertize, tool-лист и фактический вызов `memory_write`
работают end-to-end.

## Ограничения и известные грабли

* `session/fork` в Kimi игнорирует `cwd`/`mcpServers`;
  `additionalDirectories` принимаются только на `session/new`.
* `providers/*`, `nes/*`, `document/*`, `elicitation/complete` — methodNotFound;
  обходим своими сессиями и raw-запросом `session/delete`.
* SDK сериализует `session/new(mcpServers=…)` с `exclude_unset`, поэтому
  дискриминатор обязателен: `HttpMcpServer(type="http")`. Без `type` Kimi падает
  с `validation error for HttpMcpServer`.
* `TerminalExitStatus.signal` по спеке — **строка** (`"SIGKILL"`), а не число:
  отрицательный rc маппится явно, иначе клиент получает `{exit_code: null,
  signal: null}`.
* Ошибки инструментов отдаются как `ToolError`, иначе `mcp` оборачивает их в
  `Error executing tool <name>` и модель не видит текста.
* **`COOMI_KIMI_HOME` не изолирует конфиг Kimi.** Kimi Code читает
  `config.toml` из `KIMI_CODE_HOME`, поэтому ручной вызов
  `config.write_provider_config()` без `export KIMI_CODE_HOME=$(mktemp -d)`
  перезаписывает живой `~/.kimi-code/config.toml` (в этой сессии — снёс
  рабочий конфиг; `.bak` при этом стал копией тестовой заглушки, так что
  откатываться пришлось не по нему). Перед любым ручным прогоном писателя
  конфига — отдельный `KIMI_CODE_HOME`.
* `~/.kimi-code` на bind-mount f2fs из Android → hardlink в cache-store не
  проходит (`EACCES` в логе пачками). Это шум, на работу не влияет.
* Playwright-скриншоты UI в этом окружении невозможны: chromium под proot не
  стартует (exit 127).
* APK собран и воспроизводится из репозитория: см. «Android / APK» ниже.

## Android / APK

Мозг тот же (`kimi_agent`, ACP поверх `kimi acp`), обёртка — тонкий Android-слой:
WebView на консоль агента плюс foreground-сервис, который держит процесс.

```
android/                 Gradle-проект (AGP 8.5.2, Kotlin 1.9.24, wrapper 8.7)
  app/assets/*.tar.gz.bin   rootfs (67 МБ) + deps (13 МБ) + код агента (72 КБ)
  app/jniLibs/arm64-v8a/    libproot.so (bionic), libkimi.so (качается на CI)
  app/src/main/java/…/      RuntimeSpec, Bootstrap, TarReader/TarExtractor,
                            AgentService, MainActivity, AgentStatus
.github/workflows/       build-apk.yml: payload → kimi-binary → build
scripts/build_rootfs.sh  образ rootfs из пакетов этого гостя (163 пакета)
scripts/package_payload.sh  упаковка payload в assets/
```

Порядок на телефоне: `Bootstrap` распаковывает три tar.gz своим `TarExtractor`
(proot не может разпаковать собственный rootfs, а toybox `tar` не тянет pax/GNU
заголовки), копирует `libkimi.so` → `rootfs/usr/local/bin/kimi`, ставит маркер
готовности. Дальше `AgentService` поднимает проот-гость командой из
`RuntimeSpec.buildCommand()` и считает порт 8765 открытым только когда к нему
реально удаётся подключиться.

Три вещи здесь неочевидны и были найдены билдом, а не чтением доков:

* **`targetSdk = 28` намеренно.** С 29 Android запрещает `execve()` по путям
  внутри app-data, а весь runtime лежит именно там — тот же приём у Termux.
* **`.bin` в именах ассетов.** aapt2/AssetManager обрабатывают суффикс `.gz`
  специально: разжимают ассет и срезают расширение. С `rootfs.tar.gz` в APK
  попадал 245 МБ несжатого `rootfs.tar`, который `GZIPInputStream` прочесть не
  может, и APK весел 349 МБ вместо 150. CI теперь сверяет магические байты
  (`1f 8b`) и размер каждого ассета с исходным файлом.
* **`buildFeatures.buildConfig = true`** — в AGP 8 он выключен по умолчанию, а
  `AgentService` передаёт в гость `BuildConfig.VERSION_NAME`.

Сборка на телефоне невозможна в принципе: `aapt2` публикуется только под
x86-64 Linux. Поэтому компиляция идёт на GitHub Actions, а раннер arm64-бинарь
только скачивает и проверяет по sha256 — не исполняет.

Локально проверить компиляцию Kotlin нельзя: apt/dpkg в этом госте сломан
(`dpkg --configure -a` → `Permission denied` на `/var/lib/dpkg/status-old`),
JDK/Kotlin поставить нечем. Единственный контур — CI; для разбора тар-потока без
JVM написан `scripts/simulate_tar.py` (порт `TarReader`/`TarExtractor`).

## Переменные

| Имя | По умолчанию | Назначение |
|---|---|---|
| `COOMI_KIMI_BIN` | `kimi` (и `~/.kimi-code/bin/kimi`) | путь к бинарю Kimi Code |
| `COOMI_KIMI_HOST` / `_PORT` | `127.0.0.1` / `8765` | адрес фасада |
| `COOMI_KIMI_BRIDGE_PORT` | `port + 1` | MCP-мост для `session/new` |
| `COOMI_KIMI_PERMISSION` | `auto-safe` | `manual` / `auto-safe` / `auto-all` / `yolo` |
| `COOMI_KIMI_HOME` | `~/.coomi-kimi` | данные: память, планы, workflows, skills, loops |
| `COOMI_KIMI_WORKSPACE` | текущий каталог | рабочая директория сессий |
| `COOMI_KIMI_ROOTS` | workspace, home | allowlist FileService |

## Связка с приложением Coomi

Автокомпакт контекста в самом Coomi (`/workspace/.coomi/config/providers.json`,
провайдер `vv`) выставлен на 90% от **фактического** лимита модели:
`context_window: 204800`, `auto_compact_token_limit: 184320`,
`auto_compact_scope: "total"`. Первичное значение 230400 было 90% от ложных
256000 — именно переполнение контекста, а не «обрыв на провайдере», стояло за
трёшными остановками хода (`exceeds the model max token limit of 204800`).
`~/.kimi-code/config.toml` синхронизирован на те же 204800.
