# Изолированная проверка UI

Стенд запускается только на loopback `127.0.0.1` со случайным портом. Реальные исходники UI обслуживаются вместе с синтетическим API из `fixture.cjs`. PostgreSQL, Redis, worker, IMAP, SMTP и RouterAI не запускаются и не вызываются. Все адреса — `example.invalid`, токен — фикстура.

Нужны Node.js, Microsoft Edge и Playwright 1.62.1. Зависимости устанавливаются только на тестовом ПК:

```powershell
npm ci --prefix testing/ui --ignore-scripts
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.testing-artifacts/browsers'
npx --prefix testing/ui playwright install webkit
node testing/ui/run-ui.cjs --before
node testing/ui/run-ui.cjs
node testing/ui/run-ui.cjs --webkit
node --test tests/test_ui_rc.cjs
```

Команды выполняются из корня RC. Исходный UI извлекается из `7ed5d40` в игнорируемый каталог. Его внешние CSS/JS/шрифты подменяются локальными побайтовыми копиями тех же версий; DOM и стили baseline не меняются. Новый UI полностью использует локальные ресурсы. Любой другой внешний запрос блокируется и проваливает проверку.

Проверяются семь представлений на 1280/1440/1920 × 960 px, детали документа, внутренний скролл, UUID, клавиатура, полные значения, clipboard и fallback, поиск/фильтры/сортировка/пагинация, формы, AI OFF/ON/OFF и ошибка API, роль оператора, экранирование HTML и пустые результаты. Смена AI выполняется только в памяти фикстуры. Ручные resend/reprocess не вызываются.

Результаты и PNG записываются в `evidence/ui/`. При ошибке сценария итог FAIL и ненулевой exit code. Сервер и браузеры закрываются в `finally`; новые профили браузера временные. Это браузерная приёмка frontend; проверку на физическом Mac она не заменяет.
