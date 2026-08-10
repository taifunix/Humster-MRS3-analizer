# Документация Humster MRS3

## Читать в таком порядке

Новая рабочая сессия читает [AGENTS.md](../AGENTS.md) → [PRD.md](../PRD.md) → [progress.md](../progress.md) → активную спецификацию. Этого достаточно для большинства задач; архив открывается только по ссылке из active-doc.

## Актуальные документы

| Раздел | Документ | Когда читать |
| --- | --- | --- |
| Контекст | [PRD](../PRD.md) | всегда после AGENTS |
| Оперативный статус | [Progress](../progress.md) | всегда после PRD |
| Текущая поставка | [v0.7 legacy selection](specs/2026-08-10-v07-legacy-selection.md) | для работы над v0.7 |
| Event rules | [Event filter and shortlist](specs/v07-event-filter-and-shortlist.md) | при selector/event-filter изменениях |
| Post-test calibration | [Source-potential calibration](specs/v07-posttest-calibration-source-potential.md) | только после накопления results |
| Анализатор Портфеля — Queued | [Portfolio Analyzer v0.4](specs/2026-08-09-portfolio-analyzer-v04.md) | отдельной команде после проверки входных данных |
| Решения | [ADR-0001](decisions/0001-repository-and-documentation-model.md) | при вопросах структуры/workflow |

## Правила обновления

- Новая функция или изменение поведения: active spec → тесты → реализация → `progress.md` → review → commit.
- Изменение границ или зависимости: обновить PRD.
- Архитектурное/safety решение: создать ADR.
- Публичное использование изменилось: обновить корневой README только после проверки.
- Исторические документы не редактировать для описания v0.7; вместо этого создать новую спецификацию или ADR.

## Архив

[archive/](archive/README.md) хранит v0.6 baseline, handoff и source-материалы. Он не определяет текущие требования; queued Portfolio Analyzer находится в `docs/specs/`.
