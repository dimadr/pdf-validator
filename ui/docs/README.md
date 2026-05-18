# UI документация

## Структура

```
ui/
├── index.html          # Главная страница
├── app.js              # JavaScript логика
├── nginx.conf          # Конфигурация nginx
├── favicon.svg         # Иконка
├── ssl-setup.html      # Инструкция по SSL
├── mkcert-ca.crt       # CA сертификат
├── install-ca.sh       # Установка CA
└── install-ca-windows.ps1 # Установка CA (Windows)
```

## Вкладки интерфейса

### Объекты
- Список всех объектов
- Фильтр по периоду (`period-filter`)
- Сортировка (`sort-by`: name, eldis_id, object_date, calculator_number, created_at)
- Кнопка "Добавить объект" (только admin)

### Разрешённые email
- Email источники для приёма писем
- Статус (активен/отключён)

### Документы
- Список всех вложений
- Фильтр по статусу
- Статусы: new, processing, validated, rejected, sent

### Отчёты
- Отклонённые вложения
- Причина отказа
- Экспорт в CSV

### Настройки (только admin)
- **Пользователи** - управление пользователями
- **Доверенные адреса** - email для приёма
- **AI/API** - настройки RouterAI и NeuroApi
- **Почта** - IMAP/SMTP настройки

## JavaScript модули

### EmailProcessorAdmin (app.js)

```javascript
class EmailProcessorAdmin {
    apiBase = '/api/v1';
    
    // Инициализация
    async init()
    
    // Аутентификация
    async login(username, password)
    logout()
    checkAuth()
    
    // Загрузка данных
    async loadStatistics()
    async loadObjects()
    async loadEmailSources()
    async loadDocuments()
    async loadReports()
    async loadUsers()
    async loadTrustedEmails()
    async loadAIConfig()
    async loadEmailConfig()
    
    // Объекты
    async createObject(data)
    async editObject(id)
    async updateObject(id, data)
    async deleteObject(id)
    
    // Email источники
    async createEmailSource(data)
    async updateEmailSource(id, data)
    async deleteEmailSource(id)
    
    // Документы
    async loadDocumentDetails(id)
    async resendAttachment(id)
    async reprocessAttachment(id)
    
    // Пользователи
    async createUser(data)
    async updateUser(id, data)
    async deleteUser(id)
    
    // Настройки
    async saveAIConfig(data)
    async saveEmailConfig(data)
    
    // UI утилиты
    showAlert(message, type)
    renderObjects(objects)
    renderDocuments(documents)
    formatDate(date)
}
```

## Ролевая модель в UI

### admin
- Видны все вкладки включая "Настройки"
- Видны кнопки "Добавить", "Редактировать", "Удалить"
- Полный доступ

### operator
- Скрыта вкладка "Настройки"
- Скрыта кнопка "Добавить объект"
- Только просмотр

```javascript
// В showLoggedInState()
const isAdmin = this.currentUser.role === 'admin';
if (!isAdmin) {
    // Скрыть настройки
    document.querySelector('#settings-tab').style.display = 'none';
    // Скрыть кнопку "Добавить объект"
}
```

## API Endpoints (через ui)

```javascript
// Все запросы через this.apiCall(path, options)
// Автоматически добавляет Authorization header

// Объекты
GET    /objects
POST   /objects
GET    /objects/{id}
PUT    /objects/{id}
DELETE /objects/{id}

// Документы
GET    /attachments
GET    /attachments/{id}
POST   /attachments/{id}/resend
POST   /attachments/{id}/reprocess

// Email источники
GET    /email-sources
POST   /email-sources
PUT    /email-sources/{id}
DELETE /email-sources/{id}

// Настройки
GET    /settings/trusted-emails
POST   /settings/trusted-emails
PUT    /settings/trusted-emails/{id}
DELETE /settings/trusted-emails/{id}
GET    /settings/ai-config
PUT    /settings/ai-config
GET    /settings/email-config
PUT    /settings/email-config

// Пользователи
GET    /auth/users
POST   /auth/register
PUT    /auth/users/{id}
DELETE /auth/users/{id}
```

## Фильтры

### Documents
```html
<select id="status-filter">
    <option value="">Все статусы</option>
    <option value="new">Новые</option>
    <option value="processing">В обработке</option>
    <option value="validated">Проверены</option>
    <option value="sent">Отправлены</option>
    <option value="rejected">Отклонены</option>
</select>
```

### Objects
```html
<input type="text" id="period-filter" placeholder="Период (23-24)">
<select id="sort-by">
    <option value="">Сортировка</option>
    <option value="name">По названию</option>
    <option value="eldis_id">По ID</option>
    <option value="object_date">По периоду</option>
    <option value="calculator_number">По № вычислителя</option>
    <option value="created_at">По дате создания</option>
</select>
```

## Модальные окна

- `#objectModal` - добавить/редактировать объект
- `#emailSourceModal` - добавить email источник
- `#userModal` - добавить/редактировать пользователя
- `#trustedEmailModal` - добавить доверенный email

## Развёртывание

### Docker
UI запускается через nginx, проксирует на backend.

### Локальная разработка
```bash
# Установка CA сертификата
./install-ca.sh  # Linux
.\install-ca-windows.ps1  # Windows

# Запуск
# Открыть https://pdf.local в браузере
```
