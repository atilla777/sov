# sov-tasks-view — проверенное чтение файловых задач

Read-only помощник `sov-tasks` для таблиц версии SOV с полем `Координатор`. Требует Python 3 и **закреплённый** `sov-state 0.3.0 (protocol v:1)` из того же выпуска. Не открывает файлы состояния напрямую и не записывает их; четыре чтения CLI (ROADMAP, ARCHIVE и повторная сверка обоих) составляют одну попытку согласованного снимка. Не создаёт захват и не выполняет CAS. Установка и переключение использования — [отдельная проверка](../../README.md#проверенное-чтение-файловых-задач).

```sh
python3 /abs/sov/cli/sov-tasks-view/view.py validate --state-dir /abs/project/.sov --cli /abs/sov/bin/sov-state
python3 /abs/sov/cli/sov-tasks-view/view.py get --id TASK-001 --state-dir /abs/project/.sov --cli /abs/sov/bin/sov-state
python3 /abs/sov/cli/sov-tasks-view/view.py owner --owner aleksei --state-dir /abs/project/.sov --cli /abs/sov/bin/sov-state
python3 /abs/sov/cli/sov-tasks-view/view.py queue --state-dir /abs/project/.sov --cli /abs/sov/bin/sov-state
```

Одна JSON-строка в stdout на каждый исход. Успех: `v:1`, `status:"ok"`, `command`, `roadmap_revision`, `archive_revision`. `validate` добавляет `valid:true`, `queue_count`, `archive_count`, `next_id`, `diagnostics:[]`; `get` — `task`; `owner` и `queue` — `tasks` (в порядке очереди, при отсутствии собственных задач — `[]`). Задача содержит `id`, `title`, `status`, `owner`, `coordinator`, `depends_on` (массив), `publication`, `location` (`queue` или `archive`), `line`, `position` (с 1, либо `null`), абсолютный `artifact_dir`, `structurally_available`, `reasons`, `external_start_conditions_checked:false`. Доступность означает **только** `planned`, отсутствие назначения и зависимости ROADMAP в `done`; ни опубликованность требований, ни условия начала, ни захват этим не подтверждаются. Для архива `structurally_available:false`. `get` не назначает задачу; `owner` не выбирает из нескольких и не подтверждает чужой или собственный UUID.

Ошибки возвращают `v:1`, `status:"error"`, `code`, `message`, `diagnostics` (массив объектов `file`, `line`, `code`, `message`, иногда `id`):

| Exit | code | Значение |
| --- | --- | --- |
| 12 | `invalid_input` | Некорректные аргументы/пути. |
| 13 | `transport_error` | Недоступный CLI, неверная версия/ответ, ошибка чтения или несовпадение revision с bytes. |
| 14 | `invalid_state` | Некорректный формат или семантика ROADMAP **или** ARCHIVE; диагностика собирается из обоих файлов. |
| 15 | `unstable_snapshot` | Оба revision не стабилизировались после трёх полных попыток чтения. |
| 16 | `recovery_required` | Полностью совпадающая терминальная строка в обоих файлах без входящих ссылок ROADMAP; сначала восстановить перенос через `sov-tasks`. |
| 17 | `not_found` | ID отсутствует в обоих файлах после полной проверки. |

Разные строки с одним ID, ссылка ROADMAP на ARCHIVE и зависимость от отсутствующего ID дают `invalid_state`. Чтение не является транзакцией двух файлов: повторная сверка revisions обнаруживает изменения между чтениями, а допустимый стабильный промежуточный дубликат даёт `recovery_required`. Не подменяйте этой диагностикой процедуру записи и повторное чтение непосредственно перед CAS в [`sov-tasks`](../../skills/sov-tasks/SKILL.md).

Проверка на временных копиях: `MISE_GO_VERSION=1.25.4 python3 -m unittest discover -s cli/sov-tasks-view -p 'test_*.py' -v` из корня SOV. Тесты сами собирают CLI; переменная `MISE_GO_VERSION` нужна только если Go доступен через mise.
