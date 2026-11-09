/* Presentation only: full values, clipboard, accessible labels and table regions. */
(() => {
    const modal = document.getElementById('valueModal');
    const value = document.getElementById('full-value');
    const copy = document.getElementById('copy-value');
    const result = document.getElementById('copy-result');
    let returnFocus = null;

    document.addEventListener('click', event => {
        const button = event.target.closest('button[data-value]');
        if (!button) return;
        const parent = button.closest('.modal.show');
        // Bootstrap supports one modal at a time: temporarily hide the details modal.
        returnFocus = {button, parent};
        value.textContent = button.dataset.value;
        document.getElementById('value-title').textContent = button.dataset.label || 'Полное значение';
        result.textContent = '';
        const show = () => bootstrap.Modal.getOrCreateInstance(modal).show();
        if (parent) {
            parent.addEventListener('hidden.bs.modal', show, {once:true});
            parent.dataset.valuePreview = 'true';
            bootstrap.Modal.getInstance(parent).hide();
        } else show();
    });

    copy.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(value.textContent);
            result.textContent = 'Скопировано';
        } catch (_) {
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(value);
            selection.removeAllRanges();
            selection.addRange(range);
            result.textContent = 'Выделено. Нажмите Ctrl+C или Cmd+C';
        }
    });
    modal.addEventListener('hidden.bs.modal', () => {
        if (returnFocus?.parent?.isConnected) {
            const {parent,button} = returnFocus;
            parent.addEventListener('shown.bs.modal', () => button.focus(), {once:true});
            bootstrap.Modal.getOrCreateInstance(parent).show();
            delete parent.dataset.valuePreview;
        } else returnFocus?.button?.focus();
        returnFocus = null;
    });

    function labelControls() {
        document.querySelectorAll('.bi:not([aria-hidden])').forEach(icon => icon.setAttribute('aria-hidden','true'));
        document.querySelectorAll('label.form-label:not([for])').forEach((label, index) => {
            const control = label.parentElement.querySelector('input, select, textarea');
            if (!control) return;
            control.id ||= `field-${index}`;
            label.htmlFor = control.id;
        });
        const labels = {'bi-pencil':'Редактировать','bi-trash':'Удалить','bi-eye':'Подробнее','bi-arrow-clockwise':'Повторная обработка','bi-send':'Повторная отправка'};
        document.querySelectorAll('button:not([aria-label])').forEach(button => {
            if (button.textContent.trim()) return;
            const icon = button.querySelector('.bi');
            const label = button.title || Object.entries(labels).find(([name]) => icon?.classList.contains(name))?.[1];
            if (label) { button.setAttribute('aria-label', label); button.title ||= label; }
        });
        document.querySelectorAll('.table-responsive:not([role])').forEach(region => {
            region.tabIndex = 0;
            region.setAttribute('role','region');
            const table = region.querySelector('table');
            region.setAttribute('aria-label', table?.getAttribute('aria-label') || 'Таблица с горизонтальной прокруткой');
        });
    }
    labelControls();
    new MutationObserver(labelControls).observe(document.body, {childList:true,subtree:true});
})();
