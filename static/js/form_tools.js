/**
 * ParcInfo — Form Tools
 * Validation temps réel, brouillon auto, confirmation quitter
 */
const FormTools = (() => {

const VALIDATORS = {
    ip:    { re: /^(\d{1,3}\.){3}\d{1,3}(\/\d{1,2})?$/, msg: 'Format IP invalide (ex: 192.168.1.1)' },
    mac:   { re: /^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$/, msg: 'Format MAC invalide (ex: AA:BB:CC:DD:EE:FF)' },
    email: { re: /^[^\s@]+@[^\s@]+\.[^\s@]+$/, msg: 'Adresse email invalide' },
    url:   { re: /^https?:\/\/.+/, msg: 'URL invalide (doit commencer par http:// ou https://)' },
};

const FIELD_TYPES = {
    adresse_ip: 'ip', adresse_mac: 'mac',
    email: 'email', email_fournisseur: 'email',
    url: 'url',
};

function init(formId, opts) {
    opts = opts || {};
    const cfg = {
        draftKey: opts.draftKey || ('draft_' + window.location.pathname),
    };
    const form = document.getElementById(formId);
    if (!form) return;

    initValidation(form);
    initDraft(form, cfg.draftKey);
    initConfirm(form);
}

// ── VALIDATION ────────────────────────────────────────────────────────────────
function initValidation(form) {
    // Marquer champs requis avec *
    form.querySelectorAll('[required]').forEach(function(field) {
        var label = field.id ? form.querySelector('label[for="' + field.id + '"]') : null;
        if (!label) label = field.closest('.form-group') && field.closest('.form-group').querySelector('label');
        if (label && !label.querySelector('.req-star')) {
            var star = document.createElement('span');
            star.className = 'req-star';
            star.textContent = ' *';
            label.appendChild(star);
        }
    });

    // Validation au blur et à la saisie si déjà en erreur
    form.querySelectorAll('input, select, textarea').forEach(function(field) {
        field.addEventListener('blur', function() { validateField(field); });
        field.addEventListener('input', function() {
            if (field.classList.contains('field-error')) validateField(field);
        });
    });

    // Validation à la soumission
    form.addEventListener('submit', function(e) {
        var valid = true;
        form.querySelectorAll('[required]').forEach(function(f) {
            if (!validateField(f)) valid = false;
        });
        if (!valid) {
            e.preventDefault();
            var first = form.querySelector('.field-error');
            if (first) { first.scrollIntoView({ behavior:'smooth', block:'center' }); first.focus(); }
        }
    });
}

function validateField(field) {
    var group = field.closest('.form-group') || field.parentElement;
    var msg = '';

    if (field.required && !field.value.trim()) {
        msg = 'Ce champ est requis';
    } else if (field.value.trim()) {
        var type = FIELD_TYPES[field.name] || field.dataset.validate;
        if (type && VALIDATORS[type] && !VALIDATORS[type].re.test(field.value.trim())) {
            msg = VALIDATORS[type].msg;
        }
    }

    var errEl = group.querySelector('.field-err-msg');
    if (msg) {
        field.classList.add('field-error');
        field.classList.remove('field-ok');
        if (!errEl) {
            errEl = document.createElement('div');
            errEl.className = 'field-err-msg';
            field.insertAdjacentElement('afterend', errEl);
        }
        errEl.textContent = msg;
        return false;
    } else {
        field.classList.remove('field-error');
        if (field.value.trim() && field.required) field.classList.add('field-ok');
        else field.classList.remove('field-ok');
        if (errEl) errEl.remove();
        return true;
    }
}

// ── BROUILLON ─────────────────────────────────────────────────────────────────
function initDraft(form, draftKey) {
    var isNew = window.location.pathname.indexOf('/nouveau') >= 0;
    if (!isNew) return;

    // Restaurer
    try {
        var saved = localStorage.getItem(draftKey);
        if (saved) {
            var data = JSON.parse(saved);
            var restored = 0;
            form.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=color]), textarea').forEach(function(f) {
                if (f.name && data[f.name] !== undefined && !f.value) {
                    f.value = data[f.name]; restored++;
                }
            });
            form.querySelectorAll('select').forEach(function(f) {
                if (f.name && data[f.name] !== undefined) f.value = data[f.name];
            });
            if (restored > 0) showDraftNotice(form, draftKey);
        }
    } catch(e) {}

    // Sauvegarder toutes les 2s après saisie
    var timer;
    form.addEventListener('input', function() {
        clearTimeout(timer);
        timer = setTimeout(function() { saveDraft(form, draftKey); }, 2000);
    });
    form.addEventListener('submit', function() { localStorage.removeItem(draftKey); });
}

function saveDraft(form, key) {
    var data = {};
    form.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=color]), select, textarea').forEach(function(f) {
        if (f.name) data[f.name] = f.type === 'checkbox' ? f.checked : f.value;
    });
    try { localStorage.setItem(key, JSON.stringify(data)); } catch(e) {}
}

function showDraftNotice(form, key) {
    var n = document.createElement('div');
    n.className = 'draft-notice';
    n.innerHTML = '<span>📝 Brouillon restauré depuis votre dernière saisie</span>' +
        '<button type="button" onclick="localStorage.removeItem(\'' + key + '\');this.closest(\'.draft-notice\').remove();">✕ Ignorer</button>';
    form.insertAdjacentElement('beforebegin', n);
    setTimeout(function() { if (n.parentElement) n.style.opacity = '0'; }, 4000);
    setTimeout(function() { if (n.parentElement) n.remove(); }, 4500);
}

// ── CONFIRMATION QUITTER ──────────────────────────────────────────────────────
function initConfirm(form) {
    var modified = false;
    form.addEventListener('input',  function() { modified = true; });
    form.addEventListener('change', function() { modified = true; });
    form.addEventListener('submit', function() { modified = false; });

    window.addEventListener('beforeunload', function(e) {
        if (modified) { e.preventDefault(); e.returnValue = ''; }
    });

    document.querySelectorAll('a.btn-primary[href], a.btn[href]').forEach(function(link) {
        link.addEventListener('click', function(e) {
            if (modified && !confirm('Des modifications non sauvegardées seront perdues. Quitter quand même ?')) {
                e.preventDefault();
            }
        });
    });
}

return { init: init };
})();
