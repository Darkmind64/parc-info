/**
 * ParcInfo — Liste Tools (Densité + Export DB + Import CSV)
 */
const ListeTools = (() => {

    const state = {};

    function init(tableId, opts = {}) {
        const cfg = {
            storageKey:     opts.storageKey     || tableId,
            exportName:     opts.exportName     || tableId,
            skipExportCols: opts.skipExportCols || [],
            exportUrl:      opts.exportUrl      || null,
            importUrl:      opts.importUrl      || null,
        };

        const table = document.getElementById(tableId);
        if (!table) return;

        const saved = JSON.parse(localStorage.getItem('lt_' + cfg.storageKey) || '{}');
        state[tableId] = { cfg, density: saved.density || 'normal' };

        applyDensity(table, state[tableId].density);

        const bar = buildToolbar(tableId);
        const container = table.closest('.card') || table.parentElement;
        container.insertBefore(bar, container.firstChild);

        updateCount(tableId);
    }

    function buildToolbar(tableId) {
        const s = state[tableId];
        const bar = document.createElement('div');
        bar.className = 'lt-toolbar';

        const left = document.createElement('div');
        left.className = 'lt-left';
        left.innerHTML = `<span class="lt-info" id="lt-info-${tableId}"></span>`;

        const right = document.createElement('div');
        right.className = 'lt-right';

        // Densité
        const grp = document.createElement('div');
        grp.className = 'lt-group';
        grp.title = "Densité d'affichage";
        ['compact','normal','loose'].forEach((d, i) => {
            const btn = document.createElement('button');
            btn.className = 'lt-btn' + (s.density === d ? ' active' : '');
            btn.title = ['Compact','Normal','Confortable'][i];
            btn.textContent = ['▤','▦','▧'][i];
            btn.onclick = () => ListeTools.setDensity(tableId, d);
            grp.appendChild(btn);
        });
        right.appendChild(grp);

        // Export
        const expBtn = document.createElement('button');
        expBtn.className = 'lt-btn';
        expBtn.title = s.cfg.exportUrl
            ? 'Exporter depuis la base de données (toutes les colonnes)'
            : 'Exporter en CSV (vue actuelle)';
        expBtn.innerHTML = '⬇ Exporter';
        if (s.cfg.exportUrl) {
            expBtn.onclick = () => { window.location.href = s.cfg.exportUrl; };
        } else {
            expBtn.onclick = () => ListeTools.exportCSV(tableId);
        }
        right.appendChild(expBtn);

        // Import
        if (s.cfg.importUrl) {
            const importBtn = document.createElement('button');
            importBtn.className = 'lt-btn';
            importBtn.title = "Importer depuis un fichier CSV (même format que l'export)";
            importBtn.innerHTML = '⬆ Importer';

            const fileInput = document.createElement('input');
            fileInput.type = 'file';
            fileInput.accept = '.csv';
            fileInput.style.display = 'none';

            importBtn.onclick = () => fileInput.click();

            fileInput.onchange = function() {
                if (!this.files.length) return;
                if (!confirm(
                    'Importer ce fichier CSV ?\n\n' +
                    'Les lignes existantes seront mises à jour si elles correspondent.\n' +
                    'Les nouvelles lignes seront ajoutées.'
                )) { this.value = ''; return; }
                const fd = new FormData();
                fd.append('fichier', this.files[0]);
                fetch(s.cfg.importUrl, { method: 'POST', body: fd })
                    .then(r => { window.location.href = r.url || window.location.pathname; })
                    .catch(() => window.location.reload());
            };

            right.appendChild(importBtn);
            right.appendChild(fileInput);
        }

        bar.appendChild(left);
        bar.appendChild(right);
        return bar;
    }

    function applyDensity(table, density) {
        table.classList.remove('density-compact', 'density-normal', 'density-loose');
        table.classList.add('density-' + density);
    }

    function updateCount(tableId) {
        const table = document.getElementById(tableId);
        const info = document.getElementById('lt-info-' + tableId);
        if (!table || !info) return;
        const visible = Array.from(table.querySelectorAll('tbody tr'))
            .filter(r => !r.querySelector('td[colspan]') && r.style.display !== 'none').length;
        const total = Array.from(table.querySelectorAll('tbody tr'))
            .filter(r => !r.querySelector('td[colspan]')).length;
        info.textContent = visible < total
            ? `${visible} / ${total} lignes`
            : `${total} ligne${total > 1 ? 's' : ''}`;
    }

    function doExportCSV(tableId) {
        const table = document.getElementById(tableId);
        const s = state[tableId];
        if (!table) return;
        const skip = (s && s.cfg.skipExportCols) || [];
        const rows = [];
        const ths = Array.from(table.querySelectorAll('thead tr:last-child th'));
        const headers = ths
            .map((th, i) => skip.includes(i) ? null : '"' + th.textContent.trim().replace(/"/g, '""') + '"')
            .filter(Boolean);
        rows.push(headers.join(';'));
        table.querySelectorAll('tbody tr').forEach(tr => {
            if (tr.querySelector('td[colspan]')) return;
            if (tr.style.display === 'none') return;
            const cells = Array.from(tr.querySelectorAll('td'))
                .map((td, i) => skip.includes(i) ? null :
                    '"' + td.innerText.trim().replace(/\n/g, ' ').replace(/"/g, '""') + '"')
                .filter(Boolean);
            if (cells.length) rows.push(cells.join(';'));
        });
        const bom = '\uFEFF';
        const blob = new Blob([bom + rows.join('\n')], { type: 'text/csv;charset=utf-8;' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = (s ? s.cfg.exportName : tableId) + '_' + new Date().toISOString().slice(0, 10) + '.csv';
        a.click();
    }

    return {
        init,
        exportCSV: doExportCSV,

        setDensity(tableId, density) {
            const s = state[tableId];
            if (!s) return;
            s.density = density;
            const table = document.getElementById(tableId);
            applyDensity(table, density);
            document.querySelectorAll('.lt-toolbar .lt-btn[title]').forEach(btn => {
                const map = { 'Compact': 'compact', 'Normal': 'normal', 'Confortable': 'loose' };
                if (map[btn.title]) btn.classList.toggle('active', map[btn.title] === density);
            });
            localStorage.setItem('lt_' + s.cfg.storageKey, JSON.stringify({ density }));
        },

        onFilter(tableId) { updateCount(tableId); },
    };
})();
