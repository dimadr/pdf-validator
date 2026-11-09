class EmailProcessorAdmin {
    constructor() {
        this.apiBase = '/api/v1';
        this.token = localStorage.getItem('token');
        this.currentUser = null;
        this.pipelineEnabled = false;
        this.pipeline = new PipelineUI(this);
        this.objectSkip = this.documentSkip = this.reportSkip = 0;
        this.pageSize = 100;
        this.init();
    }

    escapeHtml(str) {
        return String(str ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    compactValue(value, label = 'Полное значение') {
        const text = String(value ?? '');
        if (!text) return '<span class="text-muted">—</span>';
        const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(text);
        const preview = uuid ? text.slice(0, 8) + '…' + text.slice(-4) : text;
        return `<button type="button" class="cell-value${uuid ? ' identifier' : ''}" data-value="${this.escapeHtml(text)}" data-label="${this.escapeHtml(label)}" title="${this.escapeHtml(text)}" aria-label="${this.escapeHtml(label + ': ' + text)}"><span>${this.escapeHtml(preview)}</span><i class="bi bi-chevron-expand" aria-hidden="true"></i></button>`;
    }

    renderEmptyState(tableId, count, columns) {
        if (count) return;
        document.querySelector(`#${tableId} tbody`).innerHTML = `<tr><td class="empty-state" colspan="${columns}">Ничего не найдено. Измените условия поиска или добавьте запись.</td></tr>`;
    }

    async init() {
        await this.checkConnection();
        await this.checkAuth();
        this.setupEventListeners();
        
        if (this.token) {
            await this.loadStatistics();
            await this.loadObjects();
            await this.loadEmailSources();
            await this.loadDocuments();
            await this.loadReports();
            await this.loadUsers();
            await this.loadTrustedEmails();
            await this.loadEmailConfig();
            
            setInterval(() => this.loadStatistics(), 30000);
        }
    }

    async checkAuth() {
        if (this.token) {
            try {
                this.currentUser = await this.apiCall('/auth/me', {
                    headers: { 'Authorization': `Bearer ${this.token}` }
                });
                this.showLoggedInState();
            } catch (error) {
                this.logout();
            }
        } else {
            this.showLoginModal();
        }
    }

    showLoggedInState() {
        document.getElementById('loginModal').querySelector('.btn-close')?.remove();
        bootstrap.Modal.getOrCreateInstance(document.getElementById('loginModal')).hide();
        document.getElementById('user-info').style.display = 'inline';
        document.getElementById('username').textContent = this.currentUser.username;
        document.getElementById('logout-btn').style.display = 'inline';
        
        // Show/hide settings based on role
        const isAdmin = this.currentUser.role === 'admin';
        
        // Hide settings tab for non-admin
        if (!isAdmin) {
            const settingsTab = document.querySelector('#settings-tab');
            if (settingsTab) settingsTab.style.display = 'none';
            
            // Also try other selectors for settings
            const settingsLinks = document.querySelectorAll('[data-bs-target="#settings"]');
            settingsLinks.forEach(el => el.style.display = 'none');
        }
        
        // Hide "Добавить объект" button for non-admin
        const addObjectBtn = document.querySelector('#objectModal')?.closest('.d-flex')?.querySelector('.btn-primary');
        if (!isAdmin) {
            // Find the "Добавить объект" button
            const objectsTab = document.getElementById('objects');
            if (objectsTab) {
                const buttons = objectsTab.querySelectorAll('.btn-primary');
                buttons.forEach(btn => {
                    if (btn.getAttribute('data-bs-target') === '#objectModal') {
                        btn.style.display = 'none';
                    }
                });
            }
        }
    }

    showLoginModal() {
        bootstrap.Modal.getOrCreateInstance(document.getElementById('loginModal'), { backdrop: 'static' }).show();
    }

    async login(username, password) {
        const response = await fetch(`${this.apiBase}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Ошибка входа');
        }
        
        const data = await response.json();
        this.token = data.access_token;
        localStorage.setItem('token', this.token);
        
        this.currentUser = await this.apiCall('/auth/me', {
            headers: { 'Authorization': `Bearer ${this.token}` }
        });
        
        this.showLoggedInState();
        window.location.reload();
    }

    logout() {
        this.token = null;
        this.currentUser = null;
        localStorage.removeItem('token');
        document.getElementById('user-info').style.display = 'none';
        document.getElementById('logout-btn').style.display = 'none';
        this.showLoginModal();
    }

    async checkConnection() {
        const status = document.getElementById('connection-status');
        try {
            const response = await fetch('/health/ready');
            if (!response.ok) throw new Error('API/schema not ready');
            const result = await response.json();
            this.pipelineEnabled = Boolean(result.pipeline_v2_enabled);
            status.textContent = this.pipelineEnabled ? 'API готов; pipeline v2 включён' : 'API готов; pipeline v2 выключен';
        } catch (_) {
            this.pipelineEnabled = false;
            status.textContent = 'API или схема не готовы';
        }
    }

    async apiCall(endpoint, options = {}) {
        const {page = false, ...requestOptions} = options;
        const headers = {'Content-Type': 'application/json', ...(requestOptions.headers || {})};
        if (this.token) headers.Authorization = `Bearer ${this.token}`;
        const response = await fetch(`${this.apiBase}${endpoint}`, {...requestOptions, headers});
        if (!response.ok) {
            let message = `HTTP ${response.status}`;
            try {
                const body = await response.json();
                message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || message);
            } catch (_) { /* Keep the HTTP status if no JSON error was returned. */ }
            const error = new Error(message);
            error.status = response.status;
            if (response.status === 401) this.logout();
            throw error;
        }
        const body = await response.json();
        return page ? {items: body, total: Number(response.headers.get('X-Total-Count') || body.length)} : body;
    }

    formatTime(value) {
        if (!value) return '-';
        const qualified = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z';
        return new Date(qualified).toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'});
    }

    renderPager(tableId, total, skip, callback) {
        let pager = document.getElementById(tableId + '-pager');
        if (!pager) {
            pager = document.createElement('div');
            pager.id = tableId + '-pager';
            pager.className = 'table-pager';
            pager.setAttribute('role', 'navigation');
            pager.setAttribute('aria-label', 'Страницы таблицы');
            const table = document.getElementById(tableId);
            (table.closest('.table-responsive') || table).insertAdjacentElement('afterend', pager);
        }
        pager.replaceChildren();
        const back = document.createElement('button');
        back.className = 'btn btn-sm btn-outline-secondary';
        back.textContent = 'Назад'; back.disabled = skip === 0;
        back.onclick = () => callback(Math.max(0, skip - this.pageSize));
        const next = document.createElement('button');
        next.className = back.className;
        next.textContent = 'Далее'; next.disabled = skip + this.pageSize >= total;
        next.onclick = () => callback(skip + this.pageSize);
        const label = document.createElement('span');
        label.textContent = `${total ? skip + 1 : 0}–${Math.min(skip + this.pageSize, total)} из ${total}`;
        pager.append(back, label, next);
    }

    showAlert(message, type = 'info') {
        const alertDiv = document.createElement('div');
        alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
        alertDiv.textContent = message;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-close';
        btn.setAttribute('data-bs-dismiss', 'alert');
        alertDiv.appendChild(btn);
        document.getElementById('notifications').appendChild(alertDiv);
        
        setTimeout(() => {
            if (alertDiv.parentNode) {
                alertDiv.remove();
            }
        }, 5000);
    }

    async loadStatistics() {
        try {
            const stats = await this.apiCall('/reports/summary');
            document.getElementById('total-attachments').textContent = stats.total_attachments;
            document.getElementById('processed-attachments').textContent = stats.processed;
            document.getElementById('rejected-attachments').textContent = stats.rejected;
            document.getElementById('sent-attachments').textContent = stats.smtp_accepted;
            const historicalSent = document.getElementById('legacy-sent-attachments');
            if (historicalSent) historicalSent.textContent = stats.legacy_sent;
            const pipeline = await this.apiCall('/pipeline/status');
            this.pipelineEnabled = Boolean(pipeline.pipeline_v2_enabled);
            const states = pipeline.recipient_states;
            const box = document.getElementById('pipeline-alerts');
            box.textContent = `Pipeline v2: ${this.pipelineEnabled ? 'включён' : 'выключен'}. ` +
                `SMTP-записи (включая уведомления администратору): принято ${states.accepted || 0}, ` +
                `ожидает ${states.pending || 0}, выполняется ${states.in_flight || 0}, ` +
                `отказ ${states.failed || 0}, неизвестно ${states.uncertain || 0}. ` +
                `Outbox ожидает: ${pipeline.pending_outbox}; технических ошибок: ${pipeline.technical_errors}. ` +
                'SMTP accepted не доказывает доставку в ящик.';
            box.className = (states.uncertain || pipeline.technical_errors) ? 'alert alert-warning' : 'alert alert-secondary';
        } catch (error) {
            console.error('Failed to load statistics:', error);
        }
    }

    async loadObjects(resetPage = true) {
        if (resetPage) this.objectSkip = 0;
        try {
            const searchTerm = document.getElementById('search-objects')?.value || '';
            const periodFilter = document.getElementById('period-filter')?.value || '';
            const sortBy = document.getElementById('sort-by')?.value || '';
            
            let url = '/objects/';
            const params = new URLSearchParams({skip: this.objectSkip, limit: this.pageSize});
            if (searchTerm) params.append('search', searchTerm);
            if (periodFilter) params.append('period', periodFilter);
            if (sortBy) params.append('sort_by', sortBy);
            if (params.toString()) url += '?' + params.toString();
            
            const page = await this.apiCall(url, {page: true});
            const objects = page.items;
            this.renderPager('objects-table', page.total, this.objectSkip, skip => { this.objectSkip = skip; this.loadObjects(false); });
            const tbody = document.querySelector('#objects-table tbody');
            tbody.innerHTML = '';
            
            objects.forEach(obj => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${this.compactValue(obj.eldis_id, 'ID объекта') || '-'}</td>
                    <td>${this.escapeHtml(obj.object_date) || '-'}</td>
                    <td>${this.compactValue(obj.name, 'Название объекта')}</td>
                    <td>${this.escapeHtml(obj.calculator_number) || '-'}</td>
                    <td>${this.compactValue(obj.address, 'Адрес') || '-'}</td>
                    <td>${this.compactValue(obj.email, 'Email получателей') || '-'}</td>
                    <td>
                        <button class="btn btn-sm btn-link" onclick="admin.toggleObjectStatus('${obj.id}', ${obj.is_active})" title="${obj.is_active ? 'Деактивировать' : 'Активировать'}">
                            <span class="badge bg-${obj.is_active ? 'success' : 'secondary'}">${obj.is_active ? 'Активен' : 'Неактивен'}</span>
                        </button>
                    </td>
                    <td>
                        <button class="btn btn-sm btn-outline-primary" onclick="admin.editObject('${obj.id}')">
                            <i class="bi bi-pencil"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-danger" onclick="admin.deleteObject('${obj.id}')">
                            <i class="bi bi-trash"></i>
                        </button>
                    </td>
                `;
            });
            this.renderEmptyState('objects-table', objects.length, 8);
        } catch (error) {
            console.error('Failed to load objects:', error);
        }
    }

    async loadEmailSources() {
        try {
            const sources = await this.apiCall('/email-sources/');
            const tbody = document.querySelector('#email-sources-table tbody');
            tbody.innerHTML = '';
            
            sources.forEach(source => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${this.compactValue(source.email, 'Email источника')}</td>
                    <td>${this.compactValue(source.name, 'Название источника') || '-'}</td>
                    <td><span class="badge bg-${source.is_active ? 'success' : 'secondary'}">${source.is_active ? 'Активен' : 'Неактивен'}</span></td>
                    <td>
                        <button class="btn btn-sm btn-outline-primary" onclick="admin.editEmailSource('${source.id}')">
                            <i class="bi bi-pencil"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-danger" onclick="admin.deleteEmailSource('${source.id}')">
                            <i class="bi bi-trash"></i>
                        </button>
                    </td>
                `;
            });
            this.renderEmptyState('email-sources-table', sources.length, 4);
        } catch (error) {
            console.error('Failed to load email sources:', error);
        }
    }

    async loadDocuments(statusFilter = '', resetPage = true) {
        if (resetPage) this.documentSkip = 0;
        try {
            let endpoint = '/attachments';
            const params = [`skip=${this.documentSkip}`, `limit=${this.pageSize}`];
            
            const searchTerm = document.getElementById('search-documents')?.value || '';
            if (searchTerm) {
                params.push(`search=${encodeURIComponent(searchTerm)}`);
            }
            
            if (statusFilter) {
                params.push(`status=${statusFilter}`);
            }
            
            const sortBy = document.getElementById('sort-attachments')?.value || 'created_at';
            params.push(`sort_by=${sortBy}`);
            params.push('sort_order=desc');
            
            if (params.length > 0) {
                endpoint += '?' + params.join('&');
            }
            
            const page = await this.apiCall(endpoint, {page: true});
            const attachments = page.items;
            this.renderPager('documents-table', page.total, this.documentSkip, skip => { this.documentSkip = skip; this.loadDocuments(statusFilter, false); });
            const tbody = document.querySelector('#documents-table tbody');
            tbody.innerHTML = '';
            
            attachments.forEach(att => {
                const row = tbody.insertRow();
                const statusBadge = this.getStatusBadge(this.documentStatus(att));
                const reason=att.current_revision ? (att.current_revision.state==='validated'?'':att.current_revision.reason) : att.reject_reason;
                
                row.innerHTML = `
                    <td>${this.compactValue(att.sent_filename || att.filename, 'Имя файла')}</td>
                    <td>${this.escapeHtml(att.current_revision ? att.current_revision.calculator_number : att.calculator_number) || '-'}</td>
                    <td>${att.current_revision ? this.compactValue(att.current_revision.object_name || 'не определён', 'Объект') : (att.object ? this.compactValue(att.object.name, 'Объект') : '-')}</td>
                    <td>${this.deliverySender(att)}</td>
                    <td>${statusBadge}${this.confirmationIndicators(att)}</td>
                    <td>${reason ? this.compactValue(reason, 'Причина отказа') : '—'}</td>
                    <td>${new Date(att.created_at + 'Z').toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'})}</td>
                    <td>
                        <button class="btn btn-sm btn-outline-info" onclick="admin.showAttachmentDetails('${att.id}')">
                            <i class="bi bi-eye"></i>
                        </button>
                        ${this.pipelineEnabled && this.currentUser?.role === 'admin' && att.status !== 'archived' && att.current_revision?.state !== 'discarded_inactive' ?
                            `<button class="btn btn-sm btn-outline-warning" onclick="admin.reprocessAttachment('${att.id}')">
                                <i class="bi bi-arrow-clockwise"></i>
                            </button>` : ''}
                        ${this.pipelineEnabled && this.currentUser?.role === 'admin' && att.status !== 'archived' && (att.current_revision?.state === 'validated' || (!att.current_revision && ['validated','sent'].includes(att.status))) ?
                            `<button class="btn btn-sm btn-outline-success" onclick="admin.resendAttachment('${att.id}')">
                                <i class="bi bi-send"></i>
                            </button>` : ''}
                    </td>
                `;
            });
            this.renderEmptyState('documents-table', attachments.length, 8);
        } catch (error) {
            console.error('Failed to load documents:', error);
        }
    }

    async loadReports(resetPage = true) {
        if (resetPage) this.reportSkip = 0;
        try {
            const page = await this.apiCall(`/reports/rejections?skip=${this.reportSkip}&limit=${this.pageSize}`, {page: true});
            const rejections = page.items;
            this.renderPager('rejections-table', page.total, this.reportSkip, skip => { this.reportSkip = skip; this.loadReports(false); });
            const tbody = document.querySelector('#rejections-table tbody');
            tbody.innerHTML = '';
            
            rejections.forEach(rej => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${this.compactValue(rej.filename, 'Имя файла')}</td>
                    <td>${this.compactValue(rej.reject_reason, 'Причина отказа')}</td>
                    <td>${this.compactValue(rej.object_name, 'Объект') || '-'}</td>
                    <td>${this.compactValue(rej.message_subject, 'Тема письма') || '-'}</td>
                    <td>${new Date(rej.created_at + 'Z').toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'})}</td>
                `;
            });
            this.renderEmptyState('rejections-table', rejections.length, 5);
        } catch (error) {
            console.error('Failed to load reports:', error);
        }
    }

    deliverySender(att) {
        const labels={brevo:'Brevo',timeweb:'Timeweb'};
        const providers=att.client_delivery?.providers || [];
        return providers.length?providers.map(p=>this.escapeHtml(labels[p]||p)).join('<br>'):'—';
    }

    confirmationIndicators(att) {
        const {confirmed, brevo_delivered, read_confirmed, total} = att.client_delivery?.confirmation || {};
        if (!total) return '';
        const accepted = att.client_delivery.counts?.accepted || 0;
        const sources = [brevo_delivered ? `Brevo Delivered — ${brevo_delivered}` : '',
                         read_confirmed ? `MDN — ${read_confirmed}` : ''].filter(Boolean).join(', ');
        const delivery = confirmed > 0
            ? `Доставка подтверждена для ${confirmed} из ${total} адресатов${sources ? `. ${sources}` : ''}`
            : `Доставка не подтверждена. Принято SMTP для ${accepted} из ${total} адресатов`;
        const read = read_confirmed > 0
            ? `Прочтение подтверждено по MDN для ${read_confirmed} из ${total} адресатов`
            : 'Уведомлений о прочтении (MDN) нет';
        const indicator = (signal, label, text, full) => {
            const description = this.escapeHtml(text);
            return `<span class="confirmation-indicator" data-signal="${signal}" role="img" aria-label="${description}" title="${description}">
                <span class="confirmation-dot ${full ? 'confirmation-present' : 'confirmation-missing'}" aria-hidden="true"></span>${label}</span>`;
        };
        return `<div class="confirmation-indicators">${indicator('delivery','Доставка',delivery,confirmed === total)}
            ${indicator('read','Прочтение',read,read_confirmed === total)}</div>`;
    }

    documentStatus(att) {
        if(att.status==='archived')return 'archived';
        const state=att.current_revision?.state || att.status;
        if(state!=='validated')return state;
        const counts=att.client_delivery?.counts ?? att.recipient_state_counts?.client ?? {};
        const total=Object.values(counts).reduce((a,b)=>a+Number(b),0);
        if(total>0 && counts.accepted===total)return 'validated_sent';
        if(counts.uncertain)return 'validated_uncertain';
        if(counts.in_flight)return 'validated_sending';
        if(counts.accepted)return 'validated_partial';
        if(counts.failed)return 'validated_failed';
        if(counts.pending)return 'validated_pending';
        return 'validated';
    }

    getStatusBadge(status) {
        const badges = {
            'new': 'secondary',
            'processing': 'warning',
            'validated': 'info',
            'sent': 'secondary',
            'review_required': 'warning',
            'technical_error': 'danger',
            'rejected': 'danger',
            'validated_sent':'success','validated_partial':'warning','validated_uncertain':'warning',
            'validated_failed':'danger','validated_sending':'info','validated_pending':'secondary'
        };
        const labels = {
            'new': 'Новый',
            'processing': 'В обработке',
            'validated': 'Проверен',
            'sent': 'Старая отметка отправки',
            'received': 'Получен', 'extracting': 'Извлечение', 'review_required': 'Требует проверки',
            'technical_error': 'Техническая ошибка', 'discarded_inactive': 'Неактивен: отправка запрещена',
            'archived': 'В корзине',
            'rejected': 'Отклонен',
            'validated_sent':'Проверен и отправлен','validated_partial':'Проверен, отправлен частично',
            'validated_uncertain':'Проверен, отправка неизвестна','validated_failed':'Проверен, ошибка отправки',
            'validated_sending':'Проверен, отправляется','validated_pending':'Проверен, ожидает отправки'
        };
        const color = badges[status] || 'secondary';
        const label = labels[status] || this.escapeHtml(status);
        return `<span class="badge bg-${color}">${label}</span>`;
    }

    setupEventListeners() {
        // Сброс формы при закрытии модалки
        document.getElementById('objectModal').addEventListener('hidden.bs.modal', () => {
            const form = document.getElementById('object-form');
            form.reset();
            form.removeAttribute('data-mode');
            form.removeAttribute('data-object-id');
        });

        // Сохранение объекта
        document.getElementById('save-object').addEventListener('click', async () => {
            const form = document.getElementById('object-form');
            if (!form.reportValidity()) return;
            const mode = form.getAttribute('data-mode');
            const objectId = form.getAttribute('data-object-id');
            const formData = new FormData(form);
            
            const data = {
                eldis_id: formData.get('eldis_id') || null,
                object_date: formData.get('object_date') || null,
                name: formData.get('name'),
                calculator_number: formData.get('calculator_number') || null,
                address: formData.get('address') || null,
                email: formData.get('email') || null
            };
            
            try {
                if (mode === 'edit' && objectId) {
                    // Редактирование
                    await this.apiCall(`/objects/${objectId}`, {
                        method: 'PUT',
                        body: JSON.stringify(data)
                    });
                    this.showAlert('Объект обновлен', 'success');
                } else {
                    // Создание нового
                    await this.apiCall('/objects/', {
                        method: 'POST',
                        body: JSON.stringify(data)
                    });
                    this.showAlert('Объект успешно добавлен', 'success');
                }
                
                bootstrap.Modal.getInstance(document.getElementById('objectModal')).hide();
                form.reset();
                form.removeAttribute('data-mode');
                form.removeAttribute('data-object-id');
                await this.loadObjects();
            } catch (error) {
                this.showAlert(`Ошибка при ${mode === 'edit' ? 'обновлении' : 'добавлении'} объекта`, 'danger');
            }
        });

        document.getElementById('email-source-modal').addEventListener('hidden.bs.modal', () => {
            const form = document.getElementById('email-source-form');
            form.reset();
            form.removeAttribute('data-source-id');
        });

        // Сохранение email источника
        document.getElementById('save-email-source').addEventListener('click', async () => {
            const form = document.getElementById('email-source-form');
            if (!form.reportValidity()) return;
            const sourceId = form.getAttribute('data-source-id');
            const formData = new FormData(form);
            
            const data = {
                email: formData.get('email'),
                name: formData.get('name') || null
            };
            
            try {
                await this.apiCall(sourceId ? `/email-sources/${sourceId}` : '/email-sources/', {
                    method: sourceId ? 'PUT' : 'POST',
                    body: JSON.stringify(data)
                });
                
                bootstrap.Modal.getInstance(document.getElementById('email-source-modal')).hide();
                form.reset();
                await this.loadEmailSources();
                this.showAlert(sourceId ? 'Email источник обновлен' : 'Email источник успешно добавлен', 'success');
            } catch (error) {
                // Показываем детальное сообщение об ошибке
                let errorMessage = 'Ошибка при добавлении email источника';
                
                if (error.message.includes('HTTP 400')) {
                    errorMessage = 'Неверный формат email адреса';
                } else if (error.message.includes('already exists')) {
                    errorMessage = 'Такой email уже существует';
                } else if (error.message.includes('value is not a valid email')) {
                    errorMessage = 'Введите корректный email адрес';
                }
                
                this.showAlert(errorMessage, 'danger');
            }
        });

        // Фильтр документов
        document.getElementById('status-filter').addEventListener('change', (e) => {
            this.loadDocuments(e.target.value);
        });

        // Поиск документов
        document.getElementById('search-documents')?.addEventListener('input', (e) => {
            this.loadDocuments(document.getElementById('status-filter').value);
        });

        // Сортировка документов
        document.getElementById('sort-attachments')?.addEventListener('change', (e) => {
            this.loadDocuments(document.getElementById('status-filter').value);
        });

        // Поиск объектов
        document.getElementById('search-objects')?.addEventListener('input', (e) => {
            this.loadObjects();
        });

        // Кнопки обновления
        document.getElementById('refresh-documents').addEventListener('click', () => {
            this.loadDocuments(document.getElementById('status-filter').value);
        });

        document.getElementById('refresh-reports').addEventListener('click', () => {
            this.loadReports();
        });

        // Вход
        document.getElementById('login-btn').addEventListener('click', async () => {
            const username = document.getElementById('login-username').value;
            const password = document.getElementById('login-password').value;
            const errorEl = document.getElementById('login-error');
            
            try {
                errorEl.style.display = 'none';
                await this.login(username, password);
            } catch (error) {
                errorEl.textContent = error.message;
                errorEl.style.display = 'block';
            }
        });

        // Выход
        document.getElementById('logout-btn').addEventListener('click', () => {
            this.logout();
        });

        // Сохранение пользователя
        document.getElementById('save-user').addEventListener('click', async () => {
            const form = document.getElementById('user-form');
            const userId = form.getAttribute('data-user-id');
            const formData = new FormData(form);
            
            const data = {
                username: formData.get('username'),
                email: formData.get('email') || null,
                role: formData.get('role')
            };
            
            const password = formData.get('password');
            if (password) {
                data.password = password;
            }
            
            try {
                if (userId) {
                    await this.apiCall(`/auth/users/${userId}`, {
                        method: 'PUT',
                        body: JSON.stringify(data)
                    });
                    this.showAlert('Пользователь обновлен', 'success');
                } else {
                    data.password = password;
                    await this.apiCall('/auth/register', {
                        method: 'POST',
                        body: JSON.stringify(data)
                    });
                    this.showAlert('Пользователь добавлен', 'success');
                }
                bootstrap.Modal.getInstance(document.getElementById('userModal')).hide();
                form.reset();
                form.removeAttribute('data-user-id');
                await this.loadUsers();
            } catch (error) {
                this.showAlert(error.message, 'danger');
            }
        });

        // Сброс формы пользователя при закрытии модалки
        document.getElementById('userModal').addEventListener('hidden.bs.modal', () => {
            const form = document.getElementById('user-form');
            form.reset();
            form.removeAttribute('data-user-id');
            document.querySelector('#userModal .modal-title').textContent = 'Добавить пользователя';
            document.getElementById('user-password').required = true;
            document.getElementById('password-hint').style.display = 'none';
        });

        // Сохранение доверенного email
        document.getElementById('save-trusted-email').addEventListener('click', async () => {
            const form = document.getElementById('trusted-email-form');
            const formData = new FormData(form);
            
            const data = {
                email: formData.get('email'),
                description: formData.get('description') || null
            };
            
            try {
                await this.apiCall('/settings/trusted-emails', {
                    method: 'POST',
                    body: JSON.stringify(data)
                });
                bootstrap.Modal.getInstance(document.getElementById('trustedEmailModal')).hide();
                form.reset();
                await this.loadTrustedEmails();
                this.showAlert('Email добавлен', 'success');
            } catch (error) {
                this.showAlert(error.message, 'danger');
            }
        });

        // Сохранение email конфига
        document.getElementById('email-config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const data = {
                imap_server: document.getElementById('imap-server').value || null,
                imap_port: parseInt(document.getElementById('imap-port').value) || null,
                imap_ssl: document.getElementById('imap-ssl').checked,
                smtp_server: document.getElementById('smtp-server').value || null,
                smtp_port: parseInt(document.getElementById('smtp-port').value) || null,
                smtp_ssl: document.getElementById('smtp-ssl').checked,
                email_username: document.getElementById('email-username').value || null,
                email_from_name: document.getElementById('email-from-name').value || null
            };
            
            try {
                await this.apiCall('/settings/email-config', {
                    method: 'PUT',
                    body: JSON.stringify(data)
                });
                this.showAlert('Настройки почты сохранены', 'success');
            } catch (error) {
                this.showAlert(error.message, 'danger');
            }
        });
    }

    async toggleObjectStatus(id, currentStatus) {
        const newStatus = !currentStatus;
        const action = newStatus ? 'активировать' : 'деактивировать';
        if (!confirm(`Вы уверены, что хотите ${action} этот объект?`)) return;
        
        try {
            await this.apiCall(`/objects/${id}`, {
                method: 'PUT',
                body: JSON.stringify({ is_active: newStatus })
            });
            await this.loadObjects();
            this.showAlert(`Объект ${newStatus ? 'активирован' : 'деактивирован'}`, 'success');
        } catch (error) {
            this.showAlert('Ошибка при изменении статуса', 'danger');
        }
    }

    async deleteObject(id) {
        if (!confirm('Удалить этот объект?')) return;
        
        try {
            await this.apiCall(`/objects/${id}`, { method: 'DELETE' });
            await this.loadObjects();
            this.showAlert('Объект удален', 'success');
        } catch (error) {
            this.showAlert('Ошибка при удалении объекта', 'danger');
        }
    }

    async editObject(id) {
        try {
            const obj = await this.apiCall(`/objects/${id}`);
            
            const form = document.getElementById('object-form');
            form.reset();
            
            // Устанавливаем режим редактирования
            form.setAttribute('data-mode', 'edit');
            form.setAttribute('data-object-id', id);
            
            form.querySelector('input[name="eldis_id"]').value = obj.eldis_id || '';
            form.querySelector('input[name="object_date"]').value = obj.object_date || '';
            form.querySelector('input[name="name"]').value = obj.name;
            form.querySelector('input[name="calculator_number"]').value = obj.calculator_number || '';
            form.querySelector('input[name="address"]').value = obj.address || '';
            form.querySelector('input[name="email"]').value = obj.email || '';
            
            const modalEl = document.getElementById('objectModal');
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        } catch (error) {
            this.showAlert('Ошибка при загрузке объекта', 'danger');
        }
    }

    async deleteEmailSource(id) {
        if (!confirm('Удалить этот email источник?')) return;
        
        try {
            await this.apiCall(`/email-sources/${id}`, { method: 'DELETE' });
            await this.loadEmailSources();
            this.showAlert('Email источник удален', 'success');
        } catch (error) {
            this.showAlert('Ошибка при удалении email источника', 'danger');
        }
    }

    async editEmailSource(id) {
        try {
            const source = await this.apiCall(`/email-sources/${id}`);
            
            const form = document.getElementById('email-source-form');
            form.reset();
            
            form.querySelector('input[name="email"]').value = source.email;
            form.querySelector('input[name="name"]').value = source.name || '';
            
            const modalEl = document.getElementById('email-source-modal');
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
            
            form.setAttribute('data-source-id', id);
        } catch (error) {
            this.showAlert('Ошибка при загрузке email источника', 'danger');
        }
    }

    async reprocessAttachment(id) {
        try { return await this.pipeline.command('reprocess', id); }
        catch (error) { this.showAlert(error.message, 'danger'); }
    }

    async resendAttachment(id) {
        try { return await this.pipeline.command('resend', id); }
        catch (error) { this.showAlert(error.message, 'danger'); }
    }

    async showAttachmentDetails(id) {
        try {
            const details = await this.apiCall(`/attachments/${id}/details`);
            
            let html = `
                <h6>Детали вложения</h6>
                <p><strong>Файл:</strong> ${this.escapeHtml(details.filename)}</p>
                <p><strong>Размер:</strong> ${details.file_size ? `${details.file_size} байт` : '-'}</p>
                    <p><strong>Статус старой записи:</strong> ${this.getStatusBadge(details.status)}</p>
            `;
            
            if (details.reject_reason) {
                html += `<p><strong>Причина отказа:</strong> ${this.escapeHtml(details.reject_reason)}</p>`;
            }
            
            if (details.message) {
                html += `
                    <h6>Сообщение</h6>
                    <p><strong>От:</strong> ${this.escapeHtml(details.message.from_email)}</p>
                    <p><strong>Тема:</strong> ${this.escapeHtml(details.message.subject) || '-'}</p>
                `;
            }
            
            if (details.validation_result) {
                html += `
                    <h6>Результат проверки</h6>
                    <pre class="bg-light p-2">${this.escapeHtml(JSON.stringify(details.validation_result, null, 2))}</pre>
                `;
            }
            
            html += this.pipeline.revisionHTML(details);
            html += `<div class="recipient-history">${await this.pipeline.historyHTML(id)}</div>`;

            // Создаем модальное окно
            const modalDiv = document.createElement('div');
            modalDiv.className = 'modal fade';
            modalDiv.innerHTML = `
                <div class="modal-dialog modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">Детали вложения</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Закрыть"></button>
                        </div>
                        <div class="modal-body">
                            ${html}
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Закрыть</button>
                        </div>
                    </div>
                </div>
            `;
            
            document.body.appendChild(modalDiv);
            this.pipeline.bindHistory(modalDiv, id);
            const modal = new bootstrap.Modal(modalDiv);
            modal.show();
            
            modalDiv.addEventListener('hidden.bs.modal', () => {
                if (!modalDiv.dataset.valuePreview) modalDiv.remove();
            });
            
        } catch (error) {
            this.showAlert('Ошибка при загрузке деталей вложения', 'danger');
        }
    }

    async loadUsers() {
        try {
            const users = await this.apiCall('/auth/users');
            const tbody = document.querySelector('#users-table tbody');
            tbody.innerHTML = '';
            
                users.forEach(user => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${this.escapeHtml(user.username)}</td>
                    <td>${this.compactValue(user.email, 'Email пользователя') || '-'}</td>
                    <td><span class="badge bg-secondary">${user.role === 'admin' ? 'Админ' : 'Оператор'}</span></td>
                    <td><span class="badge bg-${user.is_active ? 'success' : 'secondary'}">${user.is_active ? 'Активен' : 'Неактивен'}</span></td>
                    <td>
                        <button class="btn btn-sm btn-outline-primary" onclick="admin.editUser('${user.id}')">
                            <i class="bi bi-pencil"></i>
                        </button>
                        ${user.id !== this.currentUser?.id ? 
                            `<button class="btn btn-sm btn-outline-danger" onclick="admin.deleteUser('${user.id}')">
                                <i class="bi bi-trash"></i>
                            </button>` : ''}
                    </td>
                `;
            });
            this.renderEmptyState('users-table', users.length, 5);
        } catch (error) {
            console.error('Failed to load users:', error);
        }
    }

    async deleteUser(id) {
        if (!confirm('Удалить этого пользователя?')) return;
        
        try {
            await this.apiCall(`/auth/users/${id}`, { method: 'DELETE' });
            await this.loadUsers();
            this.showAlert('Пользователь удален', 'success');
        } catch (error) {
            this.showAlert('Ошибка при удалении пользователя', 'danger');
        }
    }

    async editUser(id) {
        try {
            const user = await this.apiCall(`/auth/users/${id}`);
            
            const form = document.getElementById('user-form');
            form.setAttribute('data-user-id', id);
            
            document.querySelector('#userModal .modal-title').textContent = 'Редактировать пользователя';
            document.getElementById('user-password').required = false;
            document.getElementById('password-hint').style.display = 'inline';
            
            document.getElementById('user-username').value = user.username;
            document.getElementById('user-email').value = user.email || '';
            document.getElementById('user-role').value = user.role;
            document.getElementById('user-password').value = '';
            
            const modalEl = document.getElementById('userModal');
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        } catch (error) {
            this.showAlert('Ошибка при загрузке пользователя', 'danger');
        }
    }

    async loadTrustedEmails() {
        try {
            const emails = await this.apiCall('/settings/trusted-emails');
            const tbody = document.querySelector('#trusted-emails-table tbody');
            tbody.innerHTML = '';
            
            emails.forEach(email => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${this.compactValue(email.email, 'Доверенный email')}</td>
                    <td>${this.compactValue(email.description, 'Описание') || '-'}</td>
                    <td><span class="badge bg-${email.is_active ? 'success' : 'secondary'}">${email.is_active ? 'Активен' : 'Неактивен'}</span></td>
                    <td>
                        <button class="btn btn-sm btn-outline-danger" onclick="admin.deleteTrustedEmail('${email.id}')">
                            <i class="bi bi-trash"></i>
                        </button>
                    </td>
                `;
            });
            this.renderEmptyState('trusted-emails-table', emails.length, 4);
        } catch (error) {
            console.error('Failed to load trusted emails:', error);
        }
    }

    async deleteTrustedEmail(id) {
        if (!confirm('Удалить этот адрес?')) return;
        
        try {
            await this.apiCall(`/settings/trusted-emails/${id}`, { method: 'DELETE' });
            await this.loadTrustedEmails();
            this.showAlert('Адрес удален', 'success');
        } catch (error) {
            this.showAlert('Ошибка при удалении адреса', 'danger');
        }
    }

    async loadEmailConfig() {
        await this.loadSMTPConfig();
        await this.loadAIConfig();
        try {
            const config = await this.apiCall('/settings/email-config');
            document.getElementById('imap-server').value = config.imap_server || '';
            document.getElementById('imap-port').value = config.imap_port || '';
            document.getElementById('imap-ssl').checked = config.imap_ssl !== false;
            document.getElementById('smtp-server').value = config.smtp_server || '';
            document.getElementById('smtp-port').value = config.smtp_port || '';
            document.getElementById('smtp-ssl').checked = config.smtp_ssl !== false;
            document.getElementById('email-username').value = config.email_username || '';
            document.getElementById('email-from-name').value = config.email_from_name || '';
            if (config.editable === false) {
                const form = document.getElementById('email-config-form');
                form.querySelectorAll('input, button').forEach(control => control.disabled = true);
                if (!document.getElementById('mail-config-managed')) {
                    const note = document.createElement('p');
                    note.id = 'mail-config-managed';
                    note.className = 'alert alert-info';
                    note.textContent = 'Эффективные параметры окружения. Изменение выполняется при отдельном внедрении; эта форма не применяет настройки.';
                    form.prepend(note);
                }
            }
        } catch (error) {
            console.error('Failed to load email config:', error);
        }
    }
    async loadSMTPConfig() {
        if (this.currentUser?.role !== 'admin') return;
        const select=document.getElementById('smtp-provider-select');
        const apply=document.getElementById('smtp-provider-apply');
        const status=document.getElementById('smtp-provider-status');
        let active;
        const render=value=>{
            active=value.provider;select.value=active;
            document.getElementById('smtp-active-provider').textContent=active==='brevo'?'Brevo':'Timeweb';
            for(const option of select.options) option.disabled=!value.available[option.value];
            const change=value.last_change;
            status.textContent=`Brevo: ${value.brevo_used_24h} / ${value.brevo_limit_24h} за 24 часа.`+
                (change?` Изменено ${this.formatTime(change.created_at)}: ${change.actor}; ${change.previous} → ${change.provider}.`:'');
        };
        try {
            render(await this.apiCall('/settings/smtp-provider'));
            apply.onclick=async()=>{
                const provider=select.value;
                if(provider===active)return;
                if(!window.confirm(`Переключить SMTP: ${active==='brevo'?'Brevo':'Timeweb'} → ${provider==='brevo'?'Brevo':'Timeweb'}?`)) {select.value=active;return;}
                apply.disabled=true;select.disabled=true;
                try {
                    render(await this.apiCall('/settings/smtp-provider',{method:'PUT',body:JSON.stringify({provider,expected_provider:active,confirmed:true})}));
                    this.showAlert('SMTP-провайдер изменён','success');
                } catch(error) {
                    this.showAlert(error.message,'danger');
                    render(await this.apiCall('/settings/smtp-provider'));
                } finally {apply.disabled=false;select.disabled=false;}
            };
        } catch(error) {apply.disabled=true;status.textContent='Настройка SMTP недоступна';}
    }
    async loadAIConfig() {
        const toggle=document.getElementById('ai-fallback-toggle');
        if (!toggle || this.currentUser?.role !== 'admin') return;
        const render = value => {
            toggle.checked=value.requested;
            document.getElementById('ai-requested').textContent=value.requested?'ВКЛ':'ВЫКЛ';
            const labels={disabled:'Выключен',enabled:'Включён; доступность провайдера проверяется при запросе',
                missing_key:'Нет API-ключа',force_disabled:'Аварийно заблокирован',provider_unavailable:'Провайдер недоступен; обычная обработка продолжает работать'};
            document.getElementById('ai-availability').textContent=labels[value.availability]||'Провайдер недоступен';
        };
        try {
            render(await this.apiCall('/settings/ai-fallback'));
            toggle.onchange=async () => {
                toggle.disabled=true;
                try {render(await this.apiCall('/settings/ai-fallback',{method:'PUT',body:JSON.stringify({enabled:toggle.checked})}));}
                catch(error){this.showAlert(error.message,'danger');render(await this.apiCall('/settings/ai-fallback'));}
                finally{toggle.disabled=false;}
            };
        } catch(error) {toggle.disabled=true;document.getElementById('ai-availability').textContent='Настройка AI недоступна';}
    }
}

// Инициализация приложения
const admin = new EmailProcessorAdmin();
