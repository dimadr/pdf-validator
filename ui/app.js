class EmailProcessorAdmin {
    constructor() {
        this.apiBase = '/api/v1';
        this.token = localStorage.getItem('token');
        this.currentUser = null;
        this.init();
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
            await this.loadAIConfig();
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
        try {
            const response = await fetch('/health');
            const statusEl = document.getElementById('connection-status');
            if (response.ok) {
                statusEl.innerHTML = '<i class="bi bi-circle-fill text-success"></i> Подключено';
            } else {
                statusEl.innerHTML = '<i class="bi bi-circle-fill text-danger"></i> Ошибка соединения';
            }
        } catch (error) {
            document.getElementById('connection-status').innerHTML = 
                '<i class="bi bi-circle-fill text-danger"></i> Нет соединения';
        }
    }

    async apiCall(endpoint, options = {}) {
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers
        };
        
        if (this.token) {
            headers['Authorization'] = `Bearer ${this.token}`;
        }
        
        try {
            const response = await fetch(`${this.apiBase}${endpoint}`, {
                headers,
                ...options
            });
            
            if (response.status === 401) {
                this.logout();
                throw new Error('Сессия истекла');
            }
            
            if (!response.ok) {
                let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
                
                try {
                    const errorData = await response.json();
                    if (errorData.detail) {
                        if (Array.isArray(errorData.detail)) {
                            errorMessage = errorData.detail.map(err => err.msg || err).join(', ');
                        } else if (typeof errorData.detail === 'string') {
                            errorMessage = errorData.detail;
                        }
                    }
                } catch (e) {
                }
                
                throw new Error(errorMessage);
            }
            
            return await response.json();
        } catch (error) {
            console.error(`API Error: ${endpoint}`, error);
            throw error;
        }
    }

    showAlert(message, type = 'info') {
        const alertDiv = document.createElement('div');
        alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
        alertDiv.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        document.querySelector('.container').insertBefore(alertDiv, document.querySelector('.container').firstChild);
        
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
            document.getElementById('sent-attachments').textContent = stats.sent;
        } catch (error) {
            console.error('Failed to load statistics:', error);
        }
    }

    async loadObjects() {
        try {
            const searchTerm = document.getElementById('search-objects')?.value || '';
            const periodFilter = document.getElementById('period-filter')?.value || '';
            const sortBy = document.getElementById('sort-by')?.value || '';
            
            let url = '/objects/';
            const params = new URLSearchParams();
            if (searchTerm) params.append('search', searchTerm);
            if (periodFilter) params.append('period', periodFilter);
            if (sortBy) params.append('sort_by', sortBy);
            if (params.toString()) url += '?' + params.toString();
            
            const objects = await this.apiCall(url);
            const tbody = document.querySelector('#objects-table tbody');
            tbody.innerHTML = '';
            
            objects.forEach(obj => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${obj.eldis_id || '-'}</td>
                    <td>${obj.object_date || '-'}</td>
                    <td>${obj.name}</td>
                    <td>${obj.calculator_number || '-'}</td>
                    <td>${obj.address || '-'}</td>
                    <td>${obj.email || '-'}</td>
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
                    <td>${source.email}</td>
                    <td>${source.name || '-'}</td>
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
        } catch (error) {
            console.error('Failed to load email sources:', error);
        }
    }

    async loadDocuments(statusFilter = '') {
        try {
            let endpoint = '/attachments';
            const params = [];
            
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
            
            const attachments = await this.apiCall(endpoint);
            const tbody = document.querySelector('#documents-table tbody');
            tbody.innerHTML = '';
            
            attachments.forEach(att => {
                const row = tbody.insertRow();
                const statusBadge = this.getStatusBadge(att.status);
                
                row.innerHTML = `
                    <td>${att.sent_filename || att.filename}</td>
                    <td>${att.calculator_number || '-'}</td>
                    <td>${att.object ? att.object.name : '-'}</td>
                    <td>${att.message ? att.message.from_email : '-'}</td>
                    <td>${statusBadge}</td>
                    <td>${att.reject_reason || '-'}</td>
                    <td>${new Date(att.created_at + 'Z').toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'})}</td>
                    <td>
                        <button class="btn btn-sm btn-outline-info" onclick="admin.showAttachmentDetails('${att.id}')">
                            <i class="bi bi-eye"></i>
                        </button>
                        ${att.status === 'rejected' ? 
                            `<button class="btn btn-sm btn-outline-warning" onclick="admin.reprocessAttachment('${att.id}')">
                                <i class="bi bi-arrow-clockwise"></i>
                            </button>` : ''}
                        ${att.status === 'validated' ? 
                            `<button class="btn btn-sm btn-outline-success" onclick="admin.resendAttachment('${att.id}')">
                                <i class="bi bi-send"></i>
                            </button>` : ''}
                    </td>
                `;
            });
        } catch (error) {
            console.error('Failed to load documents:', error);
        }
    }

    async loadReports() {
        try {
            const rejections = await this.apiCall('/reports/rejections');
            const tbody = document.querySelector('#rejections-table tbody');
            tbody.innerHTML = '';
            
            rejections.forEach(rej => {
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td>${rej.filename}</td>
                    <td><span class="badge bg-danger">${rej.reject_reason}</span></td>
                    <td>${rej.object_name || '-'}</td>
                    <td>${rej.message_subject || '-'}</td>
                    <td>${rej.from_email || '-'}</td>
                    <td>${new Date(rej.created_at + 'Z').toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'})}</td>
                `;
            });
        } catch (error) {
            console.error('Failed to load reports:', error);
        }
    }

    getStatusBadge(status) {
        const badges = {
            'new': 'secondary',
            'processing': 'warning',
            'validated': 'info',
            'sent': 'success',
            'rejected': 'danger'
        };
        const labels = {
            'new': 'Новый',
            'processing': 'В обработке',
            'validated': 'Проверен',
            'sent': 'Отправлен',
            'rejected': 'Отклонен'
        };
        const color = badges[status] || 'secondary';
        const label = labels[status] || status;
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

        // Сохранение email источника
        document.getElementById('save-email-source').addEventListener('click', async () => {
            const form = document.getElementById('email-source-form');
            const formData = new FormData(form);
            
            const data = {
                email: formData.get('email'),
                name: formData.get('name') || null
            };
            
            try {
                await this.apiCall('/email-sources/', {
                    method: 'POST',
                    body: JSON.stringify(data)
                });
                
                bootstrap.Modal.getInstance(document.getElementById('email-source-modal')).hide();
                form.reset();
                await this.loadEmailSources();
                this.showAlert('Email источник успешно добавлен', 'success');
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

        // Сохранение AI конфига
        document.getElementById('ai-config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const data = {
                router_ai_url: document.getElementById('router-ai-url').value || null,
                router_ai_key: document.getElementById('router-ai-key').value || null,
                neuro_api_url: document.getElementById('neuro-api-url').value || null,
                neuro_api_key: document.getElementById('neuro-api-key').value || null
            };
            
            try {
                await this.apiCall('/settings/ai-config', {
                    method: 'PUT',
                    body: JSON.stringify(data)
                });
                this.showAlert('Настройки сохранены', 'success');
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
                email_password: document.getElementById('email-password').value || null,
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
            
            const saveBtn = document.getElementById('save-email-source');
            saveBtn.onclick = async () => {
                const data = {
                    email: form.querySelector('input[name="email"]').value,
                    name: form.querySelector('input[name="name"]').value || null
                };
                
                try {
                    await this.apiCall(`/email-sources/${id}`, {
                        method: 'PUT',
                        body: JSON.stringify(data)
                    });
                    
                    modal.hide();
                    await this.loadEmailSources();
                    this.showAlert('Email источник обновлен', 'success');
                } catch (error) {
                    this.showAlert('Ошибка при обновлении email источника', 'danger');
                }
            };
        } catch (error) {
            this.showAlert('Ошибка при загрузке email источника', 'danger');
        }
    }

    async reprocessAttachment(id) {
        try {
            await this.apiCall(`/attachments/${id}/reprocess`, { method: 'POST' });
            this.showAlert('Переобработка запущена', 'success');
            setTimeout(() => this.loadDocuments(), 2000);
        } catch (error) {
            this.showAlert('Ошибка при запуске переобработки', 'danger');
        }
    }

    async resendAttachment(id) {
        try {
            await this.apiCall(`/attachments/${id}/resend`, { method: 'POST' });
            this.showAlert('Повторная отправка запущена', 'success');
            setTimeout(() => this.loadDocuments(), 2000);
        } catch (error) {
            this.showAlert('Ошибка при запуске повторной отправки', 'danger');
        }
    }

    async showAttachmentDetails(id) {
        try {
            const details = await this.apiCall(`/attachments/${id}/details`);
            
            let html = `
                <h6>Детали вложения</h6>
                <p><strong>Файл:</strong> ${details.filename}</p>
                <p><strong>Размер:</strong> ${details.file_size ? `${details.file_size} байт` : '-'}</p>
                <p><strong>Статус:</strong> ${this.getStatusBadge(details.status)}</p>
            `;
            
            if (details.reject_reason) {
                html += `<p><strong>Причина отказа:</strong> ${details.reject_reason}</p>`;
            }
            
            if (details.message) {
                html += `
                    <h6>Сообщение</h6>
                    <p><strong>От:</strong> ${details.message.from_email}</p>
                    <p><strong>Тема:</strong> ${details.message.subject || '-'}</p>
                `;
            }
            
            if (details.validation_result) {
                html += `
                    <h6>Результат проверки</h6>
                    <pre class="bg-light p-2">${JSON.stringify(details.validation_result, null, 2)}</pre>
                `;
            }
            
            // Создаем модальное окно
            const modalDiv = document.createElement('div');
            modalDiv.className = 'modal fade';
            modalDiv.innerHTML = `
                <div class="modal-dialog modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">Детали вложения</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
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
            const modal = new bootstrap.Modal(modalDiv);
            modal.show();
            
            modalDiv.addEventListener('hidden.bs.modal', () => {
                modalDiv.remove();
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
                    <td>${user.username}</td>
                    <td>${user.email || '-'}</td>
                    <td><span class="badge bg-${user.role === 'admin' ? 'danger' : 'primary'}">${user.role === 'admin' ? 'Админ' : 'Оператор'}</span></td>
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
                    <td>${email.email}</td>
                    <td>${email.description || '-'}</td>
                    <td><span class="badge bg-${email.is_active ? 'success' : 'secondary'}">${email.is_active ? 'Активен' : 'Неактивен'}</span></td>
                    <td>
                        <button class="btn btn-sm btn-outline-danger" onclick="admin.deleteTrustedEmail('${email.id}')">
                            <i class="bi bi-trash"></i>
                        </button>
                    </td>
                `;
            });
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

    async loadAIConfig() {
        try {
            const config = await this.apiCall('/settings/ai-config');
            document.getElementById('router-ai-url').value = config.router_ai_url || '';
            document.getElementById('router-ai-key').value = config.router_ai_key || '';
            document.getElementById('neuro-api-url').value = config.neuro_api_url || '';
            document.getElementById('neuro-api-key').value = config.neuro_api_key || '';
        } catch (error) {
            console.error('Failed to load AI config:', error);
        }
    }

    async loadEmailConfig() {
        try {
            const config = await this.apiCall('/settings/email-config');
            document.getElementById('imap-server').value = config.imap_server || '';
            document.getElementById('imap-port').value = config.imap_port || '';
            document.getElementById('imap-ssl').checked = config.imap_ssl !== false;
            document.getElementById('smtp-server').value = config.smtp_server || '';
            document.getElementById('smtp-port').value = config.smtp_port || '';
            document.getElementById('smtp-ssl').checked = config.smtp_ssl !== false;
            document.getElementById('email-username').value = config.email_username || '';
            document.getElementById('email-password').value = '';
            document.getElementById('email-from-name').value = config.email_from_name || '';
        } catch (error) {
            console.error('Failed to load email config:', error);
        }
    }
}

// Инициализация приложения
const admin = new EmailProcessorAdmin();