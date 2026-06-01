# Правила для агента

## Тестовые отправки
- **НИКОГДА** не отправлять тестовые письма реальным клиентам без явного разрешения.
- Все тесты — на реальных ящиках `dv@it37.ru`, `np@it37.ru`.
- Если сомневаешься — сначала спроси пользователя.
- **Запрещено** использовать эмодзи в коде, комментариях и документации.

## Важные соглашения
- Per-recipient SMTP: отдельное соединение (login + send + quit) на каждого получателя.
- После любого изменения кода **обязательно** пересобрать образ: `docker compose build worker && docker compose up -d`.
- `PYTHONPATH=/app:/app/backend` — импорты из `backend/` доступны.
- После пересоздания контейнера backend → перезапустить `ui` (nginx кеширует IP).

## Текущая архитектура

### Pipeline обработки вложения (без GPT)
```
email_client.fetch_emails_task (каждые 2 мин)
 → validate_pdf_attachment.delay(id) (pdf_validator)
  → если dates_ok + tables_ok (детерминировано):
    → status = approved
    → finalize_validation.delay(id) (pdf_validator, без GPT)
     → status = validated
     → send_pdf_attachment.delay(id) → клиентам из Object.email
  → если rejected (пустые ячейки / нет дат):
    → send_pdf_attachment.delay(id) → только админу (dv@it37.ru)
```

### Recovery (защита от потери задач при краше воркера)
Воркер использует Redis broker с дефолтным `acks_late=False` — задача удаляется из очереди при взятии, не дожидаясь завершения. При краше воркера во время `send_pdf_attachment` задача теряется безвозвратно.

Для страховки — `recovery.recover_stuck_attachments` (beat, каждые 10 мин):
- Ищет validated/rejected/approved аттачменты без `sent_to_email`, старше 10 мин
- Пропускает привязанные к `is_active=False`
- Для validated/rejected → `send_pdf_attachment.delay()`
- Для approved → `finalize_validation.delay()`

### Name-based inactive check (только без calculator_number)
В `email_sender.py` name-based fallback для определения `is_active` запускается только если `calculator_number` не извлечён. Иначе name-поиск слишком широк и может найти чужой неактивный филиал той же организации (например, СУ СК — 4 филиала, 2 активных, 2 нет).

### Safety net в email_sender.py
Если `reject_reason` не null, но статус `approved`/`validated` → принудительно `rejected` (только админу). Статус фиксируется `db.commit()` сразу при срабатывании и не затирается `sent` при успешной отправке.

### Статусы вложения
`new` → `approved` (детерминировано, PDF валиден) → `validated` → `sent`
`new` → `rejected` → `sent`

## Решённые проблемы

### 2026-05-26: Мёртвый GPT-пайплайн (b1)
 `gpt_validator.py` — `prepare_gpt_prompt` никогда не передавал промпт в AI. `_build_prompt()` запрашивал 3 поля, а код ожидал 6. GPT работал вхолостую.
 Удалён мёртвый метод. `_build_prompt()` теперь запрашивает все 6 полей + контекст. `_mock_response()` возвращает все 6 полей.

### 2026-05-26: RouterAI мёртвый код (b1)
 `celery_app.py` — неиспользуемые `ROUTERAI_API_KEY`, `ROUTERAI_BASE_URL`, `ROUTERAI_MODELS`.
 Удалены.

### 2026-05-27: Race condition commit vs delay (b1)
 `delay()` вызывался до `db.commit()` → воркер получал задачу, но данные в БД ещё не зафиксированы.
 `db.commit()` перенесён перед каждым `delay()`.

### 2026-05-27: RouterAI 401 / лимит / отключение GPT (b1→b2)
 Ключ первый — 401 Unauthorized.
 Новый ключ — работал, номесячный лимит 500 руб превышен (947 руб).
 Лимит увеличен пользователем, ключ заработал.
 **GPT полностью удалён из pipeline:**
 - `gpt_validator.py` удалён
 - `finalize_validation` перенесён в `pdf_validator.py` (только детерминированная логика)
 - `validate_with_gpt.delay()` заменён на `finalize_validation.delay()`
 - `gpt_validator` убран из `CELERY_IMPORTS` в `celery_app.py`
 - `AI_API_KEY` очищен в `.env`
 - `ai_client.py` остаётся мёртвым кодом (не используется)
 - `email_sender.py` — safety check: `db.commit()` сразу, `sent` не затирает `rejected`

### 2026-06-01: Пустые ячейки None/dash + gap detection + Номер прибора (b3)
 `validate_tables()` не ловил пустые ячейки, если pdfplumber возвращал None, пустую строку, одиночное тире.
 `validate_tables()` не проверял непрерывность дат — PDF без 31 мая 2026 проходил.
 У Поволжской номер 1111250 не извлекался — отсутствовал паттерн `Номер прибора:`.
 `_is_empty_cell()` — детекция None, "", "-", "–", "—", пробелов.
 Gap detection в `validate_tables()` — проверка пропусков между последовательными датами.
 `extract_calculator_number()` — добавлен паттерн `Номер прибора:\s*(\d{4,})`.

### 2026-06-01: Loss of send tasks on worker crash + name-based false positive (b3.1)
 Воркер упал во время `send_pdf_attachment` → задача потеряна из Redis (defaul `acks_late=False`). 10 validated + 15 rejected без отправки.
 Name-based inactive check в `email_sender.py` находил чужой неактивный филиал организации по общему названию (СУ СК Жиделева 5 скипалась из-за неактивного СУ СК Тейково).
 `worker/recovery.py` — beat-таска каждые 10 мин перезапускает зависшие отправки.
 Name-based inactive check — только если `calculator_number` не извлечён.

## Известные ограничения
- `attachment_processor.py` — дубликаты функций из `email_client.py`, пустая таска `process_message_attachments` (legacy).
- `email_sender.py` — safety check фиксирует статус через `db.commit()` сразу; при полном провале отправок изменение статуса фиксируется после исчерпания retry.
- `ai_client.py` — не используется после удаления GPT.
- `email_client_broken.py`, `attachment_processor_broken.py` — legacy, можно удалить.

## Архив
- `27.05.2026_2.06b1.tar.gz` — 26.05.2026 + утро 27.05 (GPT-фиксы, race condition, RouterAI ключ)
- `27.05.2026_2.06b2.tar.gz` — 27.05.2026, без GPT, safety check починена
- `01.06.2026_b2.tar.gz` — 01.06.2026, состояние ДО правок b3 (архив для отката)
- `01.06.2026_b3.tar.gz` — 01.06.2026, b3 (пустые ячейки, gap detection, Номер прибора)
- `01.06.2026_b3.1.tar.gz` — 01.06.2026, b3.1 (recovery + name-based inactive check fix)
