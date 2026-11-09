/* Reliable commands retain their idempotency key across an uncertain HTTP result. */
globalThis.PipelineUI = class PipelineUI {
    constructor(admin) {
        this.admin = admin;
        this.pending = new Map();
        this.running = new Map();
    }

    storageKey(operation, id) { return `project1-command:${operation}:${id}`; }

    loadPending(operation, id) {
        const key = this.storageKey(operation, id);
        if (this.pending.has(key)) return this.pending.get(key);
        try {
            const value = JSON.parse(sessionStorage.getItem(key));
            if (value && typeof value.key === 'string') return value;
        } catch (_) { /* Storage can be disabled by the browser. */ }
        return null;
    }

    savePending(operation, id, value) {
        const key = this.storageKey(operation, id);
        if (value) this.pending.set(key, value); else this.pending.delete(key);
        try {
            if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key);
        } catch (_) { /* The in-memory key still protects retries on this page. */ }
    }

    async command(operation, id) {
        const name = this.storageKey(operation, id);
        if (this.running.has(name)) return this.running.get(name);
        const work = this._command(operation, id);
        this.running.set(name, work);
        try { return await work; }
        finally { this.running.delete(name); }
    }

    async _command(operation, id) {
        if (!this.admin.pipelineEnabled) {
            this.admin.showAlert('Надёжный pipeline выключен. Команда не отправлена.', 'warning');
            return;
        }
        let command = this.loadPending(operation, id);
        if (!command) {
            const details = await this.admin.apiCall(`/attachments/${id}/details`);
            const bytes = crypto.getRandomValues(new Uint8Array(16));
            command = {key: Array.from(bytes, n => n.toString(16).padStart(2, '0')).join(''),
                expected_revision_id: details.current_revision_id || null};
            this.savePending(operation, id, command);
        }
        try {
            const result = await this.admin.apiCall(`/attachments/${id}/${operation}`, {
                method: 'POST', headers: {'Idempotency-Key': command.key},
                body: JSON.stringify({expected_revision_id: command.expected_revision_id})
            });
            this.savePending(operation, id, null);
            this.admin.showAlert('Команда сохранена. Результат SMTP проверяется отдельно по адресатам.', 'success');
            await this.admin.loadDocuments(document.getElementById('status-filter')?.value || '');
            return result;
        } catch (error) {
            // 409/422 are definite command rejection. A network/503 result may
            // conceal a committed command: its request key and payload survive.
            if ([400,403,404,409,422].includes(error.status)) this.savePending(operation, id, null);
            this.admin.showAlert(error.message || 'Результат команды неизвестен; повтор использует прежний ключ.', 'danger');
        }
    }

    revisionHTML(details) {
        const escape = value => this.admin.escapeHtml(value);
        const revision = details.current_revision;
        const legacy = `<h6>Старая история</h6><p>Получатели: ${this.admin.compactValue(details.sent_to_email || 'не записаны', 'Исторические получатели')}<br>` +
            `Время: ${escape(this.admin.formatTime(details.sent_at))}. Старая отметка не доказывает доставку в ящик.</p>`;
        if (!revision) return legacy + '<p>Документ ещё не имеет надёжной processing revision.</p>';
        const ai=revision.validation_result?.ai || {};
        const attempts=ai.attempts || [];
        const original=revision.validation_result?.deterministic_result;
        const periods=[...new Set((revision.validation_result?.series||[]).filter(s=>s.period).map(s=>`${s.period.start} — ${s.period.end}`))];
        const aiInfo=`<p>Режим: ${ai.state==='used'?'AI used':'deterministic only'}. AI: ${escape(ai.state||'disabled')}<br>`+
            `Исходный serial: ${escape(original?.calculator_number || (original?'не определён':revision.calculator_number) || 'не определён')}<br>`+
            `Период: ${escape(periods.join('; ')||'не доказан')}<br>`+
            `Причина AI: ${escape(ai.reason||'-')}; запросов: ${attempts.filter(a=>a.request_id&&!a.cache_hit).length}<br>`+
            `Модели: ${escape(attempts.map(a=>a.model||'-').join(', ')||'-')}<br>`+
            `Расход: ${escape(attempts.length?JSON.stringify(attempts.map(a=>a.usage||{cost:'unknown'})):'нет запросов')}</p>`;
        return legacy + aiInfo + `<h6>Текущая проверка</h6><p>${this.admin.getStatusBadge(revision.state)}<br>` +
            `Revision: ${this.admin.compactValue(revision.id, 'Revision UUID')}<br>Прибор: ${escape(revision.calculator_number || 'не определён')}<br>` +
            `Причина: ${escape(revision.reason || '-')}</p>`;
    }

    async historyHTML(id, skip=0) {
        const page = await this.admin.apiCall(`/attachments/${id}/deliveries?skip=${skip}&limit=25`);
        const escape = value => this.admin.escapeHtml(value);
        const labels = {pending:'Ожидает',in_flight:'Выполняется',accepted:'Принято SMTP',
            failed:'Отказ',uncertain:'Результат неизвестен',cancelled:'Отменено'};
        const deliveryLabels = {delivered:'Доставлено на почтовый сервер',deferred:'Задержано',
            bounced:'Возврат',blocked:'Заблокировано',failed:'Ошибка доставки',unknown:'Нет данных о доставке'};
        const rows = page.items.map(row => `<tr><td>${this.admin.compactValue(row.recipient, 'Адрес получателя')}</td>` +
            `<td>${row.role === 'client' ? 'Клиент' : 'Администратор'}</td>` +
            `<td>${escape(labels[row.state] || row.state)}</td>` +
            `<td>${escape(deliveryLabels[row.delivery?.status] || deliveryLabels.unknown)}` +
            (row.delivery?.reason ? `<br>${this.admin.compactValue(row.delivery.reason, 'Причина доставки')}` : '') +
            (row.delivery?.opened_at ? `<br><small>Сигнал открытия Brevo ${escape(this.admin.formatTime(row.delivery.opened_at))}</small>` : '') +
            (this.admin.currentUser?.role === 'admin' && row.delivery?.event_count > 0 ?
                `<br><button type="button" class="btn btn-sm btn-outline-secondary js-brevo-events" data-delivery-id="${escape(row.id)}" data-offset="0">События (${escape(row.delivery.event_count)})</button>` : '') +
            `</td><td>${row.read_confirmed_at ? 'Подтверждено MDN' : 'Нет подтверждения'}</td><td>${escape(row.attempts)}</td>` +
            `<td>${escape(row.smtp_provider || '—')}<br>${this.admin.compactValue((row.smtp_history||[]).map(a=>`${a.attempt}: ${a.provider}, ${a.state}, ${a.response_code||''} ${a.response||a.error_code||''}`).join('; '),'Попытки SMTP')}</td>` +
            `<td>${this.admin.compactValue(row.message_id, 'Message-ID')}</td><td>${this.admin.compactValue([row.error_code, row.error].filter(Boolean).join(' '), 'Ошибка SMTP')}</td>` +
            `<td>${escape(this.admin.formatTime(row.accepted_at))}</td></tr>`).join('');
        return `<h6>История отправок по адресатам</h6><p>Доставка означает принятие сервером получателя. Прочтение отмечается по MDN. ` +
            `Всего записей: ${page.total}.</p><div class="table-responsive"><table class="table table-sm" aria-label="История SMTP по адресатам">` +
            `<thead><tr><th>Адрес</th><th>Назначение</th><th>SMTP</th><th>Доставка</th><th>Прочтение</th><th>Попытки</th><th>Провайдер / ответ</th><th>Message-ID</th>` +
            `<th>Ошибка</th><th>SMTP accepted</th></tr></thead><tbody>${rows}</tbody></table></div>` +
            `<button class="btn btn-sm btn-outline-secondary js-history-page" data-offset="${Math.max(0,skip-25)}" ${skip===0?'disabled':''}>Назад</button> ` +
            `<button class="btn btn-sm btn-outline-secondary js-history-page" data-offset="${skip+25}" ${skip+25>=page.total?'disabled':''}>Далее</button>` +
            `<div class="brevo-event-history mt-2" aria-live="polite"></div>`;
    }

    async brevoEventsHTML(id, deliveryId, skip) {
        const page = await this.admin.apiCall(`/attachments/${encodeURIComponent(id)}/delivery-events?delivery_id=${encodeURIComponent(deliveryId)}&skip=${skip}&limit=25`);
        const escape = value => this.admin.escapeHtml(value);
        const rows = page.items.map(row => `<tr><td>${escape(this.admin.formatTime(row.at))}</td>` +
            `<td>${escape(row.provider)} / ${escape(row.smtp_attempt)}</td><td>${escape(row.recipient)}</td>` +
            `<td>${this.admin.compactValue(row.message_id, 'Message-ID приложения')}<br>${this.admin.compactValue(row.provider_message_id, 'Message-ID Brevo')}</td>` +
            `<td>${escape(row.event)}<br><small>${escape(row.processing_state)}</small></td><td>${escape(row.reason || '—')}</td></tr>`).join('');
        const button = (title, offset, disabled) => `<button type="button" class="btn btn-sm btn-outline-secondary js-brevo-events" data-delivery-id="${escape(deliveryId)}" data-offset="${offset}" ${disabled?'disabled':''}>${title}</button>`;
        return `<h6>События Brevo</h6><div class="table-responsive"><table class="table table-sm">` +
            `<thead><tr><th>Время</th><th>Провайдер / попытка</th><th>Получатель</th><th>Message-ID</th><th>Событие</th><th>Причина</th></tr></thead><tbody>${rows}</tbody></table></div>` +
            button('Назад', Math.max(0, skip-25), skip===0) + ' ' + button('Далее', skip+25, skip+25>=page.total);
    }

    bindHistory(modal, id) {
        modal.addEventListener('click', async event => {
            const events = event.target.closest('.js-brevo-events');
            if (events && !events.disabled) {
                const panel = modal.querySelector('.brevo-event-history');
                try { panel.innerHTML = await this.brevoEventsHTML(id, events.dataset.deliveryId, Number(events.dataset.offset)); }
                catch (error) { this.admin.showAlert(error.message, 'danger'); }
                return;
            }
            const button = event.target.closest('.js-history-page');
            if (!button || button.disabled) return;
            const box = modal.querySelector('.recipient-history');
            try { box.innerHTML = await this.historyHTML(id, Number(button.dataset.offset)); }
            catch (error) { this.admin.showAlert(error.message, 'danger'); }
        });
    }
};
