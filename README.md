# SOV

SOV — проект набора взаимосвязанных Agent Skills и шаблонов артефактов для разработки с агентами в средах типа OpenCode. Цель, границы и предполагаемый порядок работы описаны в [спецификации принципов работы](docs/sov-spec.md). Контракты и шаблоны артефактов подготовлены; реализованы инструкции `sov-tasks` для файлового учёта задач, `sov-git` для работы с Git и GitHub, `sov-okf` для оформления документов по принятому профилю, `sov-spec` для обсуждения фичи и подготовки требований, `sov-review-spec` для независимого ревью продуктовых требований, `sov-plan` для технического планирования, `sov-research` для диагностики ошибок и `sov-implement` для реализации задач. Остальные скилы и команды SOV пока не реализованы.

## Навигация

- [Спецификация SOV](docs/sov-spec.md) — исходные принципы и намеченные интерфейсы.
- [Руководство для агентов](AGENTS.md) — правила разработки самого SOV и ведения задач.
- [Индекс документации](docs/index.md) и [индекс правил](rules/index.md).
- [Контракты и шаблоны артефактов](templates/README.md).
- [Скил учёта задач `sov-tasks`](skills/sov-tasks/SKILL.md) и [образец списка задач](templates/ROADMAP.md).
- [Скил Git и GitHub `sov-git`](skills/sov-git/SKILL.md).
- [Скил оформления документов `sov-okf`](skills/sov-okf/SKILL.md).
- [Скил подготовки требований `sov-spec`](skills/sov-spec/SKILL.md).
- [Скил ревью требований `sov-review-spec`](skills/sov-review-spec/SKILL.md).
- [Скил технического планирования `sov-plan`](skills/sov-plan/SKILL.md).
- [Скил диагностики ошибок `sov-research`](skills/sov-research/SKILL.md).
- [Скил реализации `sov-implement`](skills/sov-implement/SKILL.md).
- [MIT License](LICENSE).

Планирование разработки ведётся в Obsidian: `/home/aleksei/Yandex.Disk/obsidian-vault/B - работа Plums Lab/SOV/SOV.md`. Перед созданием задач обсудим дорожную карту и критерии с пользователем. Записи Obsidian не входят в Git-репозиторий.

## Планируемая структура

`skills/` предназначен для исходников скилов SOV, `templates/` содержит заготовки локальных артефактов. Продуктовые требования и решения будут размещаться в `docs/specs/` и `docs/adrs/`, правила разработки — в `rules/`.
