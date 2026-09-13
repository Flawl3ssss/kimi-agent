# План: Coomi-агент поверх Kimi Code (ACP)

Цель: агент с функциями Coomi, где мозг и tool-loop — Kimi Code 0.42.0, а
Coomi-специфика (память, план, суб-агенты, skills, workflows, решения, файлы,
картинки) подаётся нативными для Kimi механизмами: **MCP-сервером**,
**agent-профилем**, **hooks**, **config**. Свой код — только хост/клиент и UI.

Транспорт: `kimi acp` поверх stdio (JSON-RPC). Обоснование: Kimi Code сам
построен на ACP (его TUI и `kimi web` гоняют те же методы), полный клиент уже
реализован и проверен живым handshake. Альтернативы (`kimi web` REST,
`-p --output-format stream-json`) зафиксированы как запасные: REST — если
потребуется меньше кода, print — для батчей/CI.

## Фаза 0 — Разведка и источниковая база ✅
- Kimi Code native linux-arm64 0.42.0 (не legacy `kimi-cli`), конфиг под
  OpenAI-совместимый эндпоинт, `doctor`/`-p`/ACP-hanshake зелёные.
- Корпус: `vendor/CORPUS.md` (sparse-клон исходников `packages/acp-server`,
  58 зеркал доков en+zh, ACP-спека, python SDK, MCP SDK).
- Контракт выписан из исходников, а не из догадок: option-id пространства
  (`approve_once|approve_always|reject`, `plan_opt_i|plan_approve|plan_revise|plan_reject_and_exit`,
  `q0_opt_i|q0_skip`), реестр `ToolKind` (10 значений), `usage_update` = одна
  запись после хода, `stopReason: failed→end_turn`, elicitation только под
  `use_unstable_protocol=True`.

## Фаза 1 — Ядро (мост) ✅ код есть, фаза 3 чинит расхождения
`kimi_agent/bridge.py` — один процесс `kimi acp`, N сессий:
- `start()` initialize(capabilities: fs+terminal+elicitation.form) → кэш
  agentInfo/capabilities; stderr-кольцо; авто-реакция на падение процесса
  (`agent_exited`, сессии → detached, pending-решения → cancelled).
- `new_session/load/resume/fork/close/delete/list`, `prompt` (text+image+resource),
  `cancel`, `set_mode`, `set_config(model|thinking|mode)`, MCP-проброс из
  `config/mcp.json` в `session/new(mcpServers=…)`.
- `client.py`: DecisionBroker (политики `manual|auto-safe|auto-all` +
  whitelist-эвристика shell + session-grants), FileService (sandbox по
  allowlist корневых каталогов), TerminalService (реальные подпроцессы,
  polling-стриминг, kill/release).
- `events.py`: нормализация union-а `session/update` → плоский Event-поток
  (text/thinking/tool_call/tool_update/plan/commands/config/usage/mode/…)
  + ring-buffer на сессию и fan-out подписчикам.

## Фаза 2 — Coomi-функции как MCP-сервер (свой код = ровно это) ⏳ в работе
Контрактные решения подтверждены кодом: `_tool_name_from_title` понимает и
`BareName` (Kimi), и `Tool: detail`; questions помечаются в payload и не
получают авто-ответ по таймеру; `kill_terminal` возвращает типизированный ответ;
`session/delete` идёт raw-запросом; `set_config` валидирует значение по
`configOptions` до отправки.
`kimi_agent/tools/` (stdio MCPServer, mcp 2.2 `mcp.server.mcpserver.MCPServer`):
| группа | инструменты | зачем |
|---|---|---|
| память | `memory_write/read/search/list/delete` | persistent память Coomi, precedence local→project→global |
| план | `update_plan/get_plan` | структурированный план (`update_plan` в Coomi) |
| суб-агенты | `spawn_agent/wait_agent/close_agent` | изолированный контекст; реализуем через **отдельные ACP-сессии** того же Kimi (паритет с `spawn_agent`) |
| workflows | `create/list/get/save/delete/run_workflow` | сохраняемые пайплайны шагов |
| skills | `list_skills/read_skill/create_skill` | каталог skills + генерация новых |
| web | `web_search` | Serper-совместимый/эвристический поиск; `FetchURL` уже встроен в Kimi |
| мультимодальность | `show_image/view_image` | возврат пути/описания картинкы клиенту (UI рисует) |
| решения | `submit_answer` + `list_pending` | ответ на `approval_request`/`question_request` из UI (push-канал) |
| файлы | `import_inbox/export_file` | телефонный file picker (в Coomi — request_file_import/export) |
| среда | `runtime_doctor` | host/proot/guest факт-лист |
| циклы | `create_loop/get_loop/update_loop` | автономные objectives с бюджетом |

Все инструменты пишут аудит в `~/.coomi-kimi/`.

## Фаза 3 — Точная подгонка под контракт (из исходников)
- [x] `elicitation/create` требует `use_unstable_protocol=True` — включено.
- [x] `delete_session`: `ext_method()` шлёт `_session/delete` → нейдёт; нужен
  raw `send_request("session/delete", …)`.
- [x] `auto-safe` не должен автоодобрять **вопросы**: `AskUserQuestion` приходит
  как `request_permission` с kind=`other` и id `q0_opt_*` → запрещать авто-allow
  по namespace `q\d+_`/имени инструмента.
- [x] `SAFE_KINDS` не должен включать `other` (именно он у вопросов/не-инструментов).
- [x] `kill_terminal` → `acp.schema.KillTerminalResponse()` (не `None`).
- [x] `set_config("model", …)`: значение валидируется по `configOptions[].options`
  (значение = id из пикера, не алиас) + publish `info` вместо тихого проглатывания.
- [x] plan_review (выход из Plan mode): распознавать `plan_*` и отдавать
  `allow_once`-выбор, reject-ветку — по `plan_reject_and_exit`.
- [x] тесты на эти контракты (см. Фазу 5), ловят регресс.
- [x] **Дыра авто-одобрения (найдена на живой проверке, не в моках).** Kimi в
  `request_permission` шлёт `title` = голое имя инструмента (`"Bash"`) и **не шлёт
  `kind`** (`approval.ts::buildPermissionToolCallUpdate`); команда лежит только в
  `content` как проза «Requesting approval to Running: …». Прежний `auto-safe` на
  `kind is None` отвечал `allow` ⇒ под «безопасной» политикой проходила **любая**
  shell-команда, парсер к ней не добирался. Починено: `infer_tool_kind()` по
  фактическому реестру имён Kimi (`packages/agent-core-v2/src/agent/tools/**`),
  `_command_text()` вынимает команду из `content`, отсутствующий `kind` больше не
  «безопасно», в UI и в pending уходит `detail` с настоящей командой.
- [x] **Grant «Always allow» для Bash привязывался к заголовку** (= имя инструмента),
  т.е. одно одобрение `ls` открывало и будущую `rm -rf /`. Для execute-инструментов
  в grant теперь попадает **команда**, а проверка опасных паттернов поднята выше
  поиска по grant.
- [x] **Модель могла одобрять собственные permissions** через `mcp__coomi__submit_answer`.
  MCP-инструменты разложены по классам риска (`SAFE_MCP_TOOLS`,
  `EDITING_MCP_TOOLS`, `PRIVILEGED_MCP_TOOLS`, `SELF_ANSWERING_MCP_TOOLS`):
  `submit_answer`/`spawn_agent`/`run_workflow`/`run_loop_turn`/`close_agent`
  не автоодобряются и не закрываются grant'ом, read-only — свободны.
- [x] **Проверено, что MCP-вызовы вообще проходят через калитку**
  (`scripts/check_mcp_gated.py`, policy=manual): на `mcp__coomi__list_skills`
  приходит approval ⇒ градация инструментов — реальный контроль, а не мёртвый код.
  Пейлоад при этом `kind="other"`, `title="mcp__coomi__list_skills"`, текст —
  «Requesting approval to Approve …» (формулировка отличается от bash-ной, regex
  снятия обвязки учитывает обе).
- [x] **Эскалация выбора опции.** `_pick_option` для пейлоадов без поля `kind`
  использовал `option_id.startswith(tuple(kinds))`, а `tuple("allow_once")` — это
  мешок из букв: матчил почти любой id. «Разрешить один раз» мог молча вернуть
  `approve_always`, т.е. выдать постоянное разрешение там, где просили
  одноразовое. Фолбэк переписан на разбор id по токенам: полярность
  (allow/approve против reject/deny) + совпадение хвостового слова
  (`once`/`always`); нет совпадения → пустая строка → `cancelled` (fail closed).

## Фаза 4 — Фасад: как этим пользуются
- `rpc.py` — собственный JSON-RPC 2.0 сервер (тоже ACP-лексика: `initialize`,
  `session/new`, `session/prompt`, `session/update`, `session/request_permission`,
  `coomi/tools/*`), т.е. наш сервер — ACP-сервер для UI/клиентов.
- HTTP + **SSE** (`/events`) и **WebSocket** (`/ws`) для стрима; `POST /decide`
  для approvals/questions; REST-алиасы `/api/sessions`, `/api/prompt`, `/api/health`.
  Только `127.0.0.1` (урок proot), наружу — через хост-слой.
- `web/index.html` — чат: стрим текста/мыслей, tool-карточки, diff, plan-панель,
  usage, кнопки Approve/Always/Reject, вопросы с вариантами, выбор model/thinking/mode,
  загрузка картинок (ACP image block), replay истории.
- `repl.py` — rich-терминал: тот же поток, `!` shell, `/sessions`, `/mode`,
  `/model`, `/approve`, `/deny`, `/question`, `/plan`, `/interrupt`, `/exit`.
- `cli.py` — `coomi-kimi serve | chat | ask "…" | doctor | sessions`.
- `config/coomi-agent.md` — agent-профиль с системным промптом в духе Coomi
  (role, стиль, `tools`/`subagents` allowlist) + `[secondary_model]` пул;
  `SYSTEM.md`-альтернатива описана в README.

## Фаза 5 — Проверки
- [x] unit (`tests/`, **123 passed**): политика решений (shell-whitelist,
  `q0_opt_*` никогда не auto-allow, `plan_*`, Kimi-формат `title="Bash"`/`kind=null`,
  привязка grant к команде, запрет auto-approve на `submit_answer`/`spawn_agent`),
  FileService sandbox (traversal/allowlist), TerminalService (exit code/truncate/
  kill/release), store (память/план/workflow/skill), events-маппинг.
- [x] **Тесты на wiring, а не только на чистую функцию.** Первые регрессы звали
  `auto_decision` напрямую и потому не замечали, что `request_permission` может
  перестать передавать `content` — решение тогда молча возвращается к голому
  title `"Bash"`, а все тесты остаются зелёными. Добавлены прогоны через саму
  точку входа на настоящем объекте `acp.schema.ToolCall` с `kind=None`
  (`tests/test_policy.py::kimi_tool_call`), включая сквозной кейс «человек ответил
  allow без option_id» на опциях без `kind`.
- [x] **Мутационный контроль** (`scripts/mutation_check.py`): каждая починенная
  дыра временно вставляется обратно, и проверка требует, чтобы набор тестов падал.
  Первый прогон поймал 6 из 9 — три регрессии («решение по title», «grant раньше
  veto», «submit_answer без veto») зелёным набором не обнаруживались; после
  добавления трёх targeted-кейсов и мутации «call site перестаёт передавать
  `content`» — **10/10 пойманы**. Скрипт восстанавливает файл
  в `finally`, поэтому прерванный прогон не оставляет дерево мутированным.
- [x] contract smoke (`scripts/smoke_bridge.py`, реальный Kimi): SMOKE PASS —
  initialize → session/new → prompt с Bash → обратный `terminal/*` observed →
  histogram событий → 2 параллельные сессии; `usage 24990/204800`.
- [x] `scripts/smoke_decide.py` (policy=manual): approve/reject/question — все PASS,
  в pending видно `kind=execute` с полной командой в `detail`
  (`printf COOMI_DECIDE_OK > …/approve_side_effect.txt`). Оракул в этом скрипте —
  файл на диске, а не текст ответа: модель, корректно отказавшаяся выполнять
  команду, всё равно упоминает маркер, объясняя отказ, и наивная проверка по
  транскрипту давала ложный FAIL.
- [x] `scripts/smoke_e2e_mcp.py`: MCP-контур end-to-end — advertise `['coomi']`,
  34 инструмента, модель сама вызвала `memory_write` + `list_skills`, запись
  подтверждена с диска, `stopReason=end_turn`.
- [x] `scripts/check_autosafe_gate.py` — политика на живой модели, а не на моках:
  6/6 PASS (read-only без трения, `touch` → спрос с командой в `detail`,
  approve → реально исполнилось, `mcp__coomi__memory_search` свободен).
- [x] `python -m compileall` чист; `node --check web/app.js` OK.
- [ ] Playwright-скриншот UI — невозможен в этом окружении (chromium под proot не
  стартует, exit 127); UI проверен отдачей ассетов и `node --check`.

## Фаза 6 — Сборка и доставка
- [x] `requirements.txt` (pinned: acp 0.12.1, mcp 2.2.0, aiohttp 3.14.3, rich 15.0.0,
  pyyaml 6.0.3, pytest 9.1.1 + pytest-asyncio 1.4.0), `pyproject.toml`
  (console-script `coomi-kimi`), `run.sh` (создаёт `.venv` через uv или venv+pip,
  поднимает PATH к `~/.kimi-code/bin`, печатает шаблон `config.toml` если Kimi нет).
- [x] `README.md`: схема, обоснование «нужен ли ACP», таблица «функция Coomi → где
  реализовано», политики разрешений с мотивированным правилом на каждое, команды
  проверок, грабли, переменные окружения, связка с 90% автокомпактом.
- Опционально (нужен `GH_TOKEN`, выдан): APK-обёртка — Termux/Android host, который
  запускает `run.sh` и поднимает WebView на локальный сервер; сборка через Gradle
  (`/opt/coomi-dev` пуст → SDK ставим сами), подпись, экспорт APK через file-export.

## Риски / известные ограничения
- Kimi-internal: `session/fork` игнорирует `cwd/mcpServers`, `additionalDirectories`
  только на `session/new`; `providers/*`, `nes/*`, `document/*`, `elicitation/complete`
  = methodNotFound. Обходим: свои сессии + свой fork-логик при необходимости.
- `qwen3.8-flash` через OpenAI-шлюз: tools-паритет зависит от `supports_native_tools`
  (включено), но `image_in` для этой модели проверим отдельным smoke.
- Долгие tool-вызовы MCP: таймауты `startup_timeout_ms`/`tool_timeout_ms`
  поднимаем в `mcp.json` (суб-агенты/loop могут жить минуты).
- Секреты: `GH_TOKEN` в `~/.secrets/gh-token` (600), api-ключ провайдера только в
  `config.toml`/`providers.json`; в вывод и в git не печатаем.
- Автокомпакт Coomi: 90% от **фактического** лимита модели, а не от витринного —
  `context_window: 204800`, `auto_compact_token_limit: 184320` (первичные 230400
  считались от ложных 256000). `auto_compact_scope: total` и
  `effective_context_window_percent` не трогали. Движок периодически перезаписывает
  `providers.json`, значения нужно перепроверять после рестарта.
- Трешные «обрывы хода» в Coomi оказались не стримом провайдера, а переполнением
  контекста: в сессии лежат 3 вхождения
  `exceeds the model max token limit of 204800` при объявленных 256000.
- Шум `~/.kimi-code/logs/kimi-code.log`: пачки `EACCES ... cluster.meta.json.tmp-*`
  — Kimi делает hardlink, а `/home/coomi` это bind-mount f2fs из Android. На работу
  не влияет (0 ERROR в 16.8k строк), но маскирует полезный вывод в `grep`.

### Хронология правок плана
- v1 до разведки исходников (ошибка: начал кодить до фиксации контракта).
- v2 после чтения `packages/acp-server` и матрицы методов: добавлена Фаза 3
  (6 контрактных расхождений), уточнены option-пространства и unstable-elicitation.
- v3: живой контракт `request_permission` оказался беднее спеки (`kind` не
  заполняется), Фаза 3 пополнилась тремя пунктами безопасности; проверки расширены
  `scripts/check_autosafe_gate.py`, который гоняет политику на живой модели, а не
  только на моках.

### Выученные уроки (чтобы не наступаться снова)
1. ACP-спека ≠ то, что шлёт конкретный агент: `kind` декларирован, но Kimi его не
   заполняет. Политику надо строить на захваченных пейлоадах, а не на схеме.
2. `allow` при неизвестном поле = дыра. Неизвестное всегда уходит к человеку.
3. Grant обязан привязываться к тому, что реально опасно (команда), а не к ярлыку.
4. Инструмент, отвечающий за человека, не должен подчиняться grant'ам — иначе
   модель самодостаточна в обходе калитки.
5. `session/new(mcpServers=…)`: дискриминатор `type="http"` обязателен (SDK
   сериализует с `exclude_unset`), иначе Kimi падает на валидации.
6. `TerminalExitStatus.signal` по спеке — строка (`"SIGKILL"`), а не число.
7. `max_context_size` обязан равняться фактическому лимиту апстрима (204800);
   завышение выглядит как «обрыв стрима у провайдера».
8. Ошибки инструментов отдавать как `ToolError`, иначе MCP съедает текст и модель
   видит лишь `Error executing tool <name>`.
9. Долгие прогоны на живых моделях — только через `local_shell` с `yield_time_ms 0`:
   30-секундный shell-таймаут убивал зонды и оставлял сирот-процессы.
