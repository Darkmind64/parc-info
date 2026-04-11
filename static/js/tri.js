/**
 * ParcInfo — Module de tri de tableaux
 * Tri multi-colonnes côté client, persisté en sessionStorage par table.
 */
(function () {
    'use strict';

    /* ── CONFIG ─────────────────────────────────────────────────────── */
    const ICONS = { asc: ' ↑', desc: ' ↓', none: ' ↕' };

    /* ── ÉTAT ───────────────────────────────────────────────────────── */
    // { tableId: { col: Number, dir: 'asc'|'desc' } }
    const state = {};

    /* ── INIT ───────────────────────────────────────────────────────── */
    function init(tableId, opts = {}) {
        const table = document.getElementById(tableId);
        if (!table) return;

        const skipped = opts.skipCols || [];   // indices de colonnes non triables
        const dataKey = 'sort_' + tableId;

        // Restaurer état précédent
        try {
            const saved = JSON.parse(localStorage.getItem(dataKey));
            if (saved) state[tableId] = saved;
        } catch (_) {}

        // Décorer les <th>
        const ths = table.querySelectorAll('thead th');
        ths.forEach((th, i) => {
            if (skipped.includes(i)) return;
            th.style.cursor = 'pointer';
            th.style.userSelect = 'none';
            th.style.whiteSpace = 'nowrap';
            th.dataset.col = i;
            const ind = document.createElement('span');
            ind.className = 'sort-ind';
            ind.textContent = ICONS.none;
            ind.style.cssText = 'font-size:.7rem;opacity:.4;margin-left:2px;';
            th.appendChild(ind);
            th.addEventListener('click', () => triColonne(tableId, i, skipped));
        });

        // Appliquer tri sauvegardé
        if (state[tableId]) {
            appliquerTri(table, state[tableId].col, state[tableId].dir, skipped);
            updateHeaders(table, state[tableId].col, state[tableId].dir, skipped);
        }
    }

    /* ── TRI ────────────────────────────────────────────────────────── */
    function triColonne(tableId, col, skipped) {
        const table = document.getElementById(tableId);
        if (!table) return;
        const cur = state[tableId];
        let dir = 'asc';
        if (cur && cur.col === col) dir = cur.dir === 'asc' ? 'desc' : 'asc';
        state[tableId] = { col, dir };
        try { localStorage.setItem('sort_' + tableId, JSON.stringify({ col, dir })); } catch (_) {}
        appliquerTri(table, col, dir, skipped);
        updateHeaders(table, col, dir, skipped);
    }

    function appliquerTri(table, col, dir, skipped) {
        const tbody = table.querySelector('tbody');
        if (!tbody) return;

        // Séparer les lignes normales des lignes "séparateur" (colspan entier)
        const allRows = Array.from(tbody.querySelectorAll('tr'));
        const colCount = table.querySelectorAll('thead th').length;

        // Lignes triables = celles qui ont le bon nb de cellules
        const dataRows = allRows.filter(r => r.cells.length >= colCount - 1 && !r.querySelector('td[colspan]'));
        const sepRows  = allRows.filter(r => r.querySelector('td[colspan]') || r.cells.length < colCount - 1);

        dataRows.sort((a, b) => {
            const va = cellValue(a, col);
            const vb = cellValue(b, col);
            const cmp = smartCompare(va, vb);
            return dir === 'asc' ? cmp : -cmp;
        });

        // Vider le tbody et remettre les lignes dans l'ordre trié (sans les séparateurs)
        // Si le tri est actif on supprime les séparateurs de catégorie (ils seront ré-insérés si besoin)
        tbody.innerHTML = '';
        dataRows.forEach(r => tbody.appendChild(r));
    }

    function cellValue(row, col) {
        const cell = row.cells[col];
        if (!cell) return '';
        // Priorité : data-sort > texte
        return (cell.dataset.sort || cell.innerText || '').trim().toLowerCase();
    }

    function smartCompare(a, b) {
        // IP addresses
        const ipA = parseIP(a), ipB = parseIP(b);
        if (ipA !== null && ipB !== null) return ipA - ipB;
        // Dates dd/mm/yyyy
        const dA = parseDate(a), dB = parseDate(b);
        if (dA && dB) return dA - dB;
        // Nombres
        const nA = parseFloat(a), nB = parseFloat(b);
        if (!isNaN(nA) && !isNaN(nB)) return nA - nB;
        // Chaînes
        return a.localeCompare(b, 'fr', { sensitivity: 'base' });
    }

    function parseIP(s) {
        const m = s.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
        if (!m) return null;
        return (+m[1] << 24) + (+m[2] << 16) + (+m[3] << 8) + +m[4];
    }

    function parseDate(s) {
        // dd/mm/yyyy
        const m = s.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
        if (!m) return null;
        return new Date(+m[3], +m[2] - 1, +m[1]);
    }

    /* ── HEADERS ────────────────────────────────────────────────────── */
    function updateHeaders(table, activeCol, dir, skipped) {
        const ths = table.querySelectorAll('thead th');
        ths.forEach((th, i) => {
            const ind = th.querySelector('.sort-ind');
            if (!ind) return;
            if (i === activeCol) {
                ind.textContent = ICONS[dir];
                ind.style.opacity = '1';
                ind.style.color = 'var(--accent)';
                th.style.color = 'var(--accent)';
            } else {
                ind.textContent = ICONS.none;
                ind.style.opacity = '.3';
                ind.style.color = '';
                th.style.color = '';
            }
        });
    }

    /* ── API PUBLIQUE ───────────────────────────────────────────────── */
    window.TriTable = { init };
})();
