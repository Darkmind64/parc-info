/**
 * Annonce des mises à jour de ParcInfo.
 *
 * Interroge /api/updates/status, affiche une bannière quand une version plus
 * récente existe, et suit l'installation jusqu'au redémarrage.
 *
 * Trois présentations selon le mode d'exécution renvoyé par le serveur :
 *   - windows / macos : bouton « Installer »
 *   - docker          : commande à lancer sur l'hôte, un conteneur ne pouvant
 *                       pas se remplacer lui-même
 *   - source          : information seule
 */

(function () {
    'use strict';

    var INTERVALLE_NORMAL = 15 * 60 * 1000;   // 15 min au repos
    var INTERVALLE_TRAVAIL = 2 * 1000;        // 2 s pendant une installation

    var conteneur = null;
    var minuteur = null;
    var etatCourant = null;
    var pastilleVersion = null;   // bouton du numéro de version dans la barre
    var versionInitiale = '';

    function elt(id) { return document.getElementById(id); }

    function echapper(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }

    // ── Rendu ───────────────────────────────────────────────────────────────

    function styleBandeau(couleur) {
        return 'display:flex;align-items:center;gap:1rem;flex-wrap:wrap;'
             + 'background:var(--bg-card);border:1px solid ' + couleur + ';'
             + 'border-left:4px solid ' + couleur + ';border-radius:6px;'
             + 'padding:.75rem 1rem;margin:.75rem 1rem 0;'
             + 'font-size:.9rem;color:var(--text-primary);';
    }

    function bouton(action, libelle, principal) {
        var base = 'padding:.45rem .9rem;border-radius:4px;cursor:pointer;'
                 + 'font-family:inherit;font-size:.85rem;white-space:nowrap;';
        if (principal) {
            base += 'background:var(--accent);color:#001018;border:none;font-weight:700;';
        } else {
            base += 'background:transparent;color:var(--text-secondary);'
                  + 'border:1px solid var(--border);';
        }
        return '<button data-action="' + action + '" style="' + base + '">'
             + echapper(libelle) + '</button>';
    }

    function barreProgression(pct) {
        return '<div style="flex:1 1 180px;height:6px;background:var(--bg-secondary);'
             + 'border-radius:3px;overflow:hidden;min-width:120px;">'
             + '<div style="height:100%;width:' + Math.max(0, Math.min(100, pct)) + '%;'
             + 'background:var(--accent);transition:width .4s ease;"></div></div>';
    }

    function rendre(etat) {
        if (!conteneur) { return; }

        // Rien à annoncer : pas de bannière du tout.
        var enCours = etat.phase === 'telechargement' || etat.phase === 'installation'
                   || etat.phase === 'pret';
        if (!enCours && !etat.version_installee
            && (!etat.mise_a_jour_disponible || etat.masquee)) {
            conteneur.innerHTML = '';
            return;
        }

        var html;

        if (etat.version_installee && !enCours) {
            // Premier démarrage après une mise à jour : dire ce qui a changé.
            html = '<div style="' + styleBandeau('var(--accent-green)') + '">'
                 + '<div style="flex:1 1 320px;">'
                 + '<strong>✓ ParcInfo a été mis à jour en version '
                 + echapper(etat.version_installee) + '</strong></div>'
                 + (etat.url_notes
                    ? '<a href="' + echapper(etat.url_notes) + '" target="_blank" rel="noopener" '
                      + 'style="color:var(--accent);font-size:.85rem;">Nouveautés</a>'
                    : '')
                 + bouton('dismiss', 'Fermer', false)
                 + '</div>';
        } else if (etat.phase === 'telechargement') {
            html = '<div style="' + styleBandeau('var(--accent)') + '">'
                 + '<strong>⬇ Téléchargement de la version ' + echapper(etat.version_disponible) + '</strong>'
                 + barreProgression(etat.progression || 0)
                 + '<span style="color:var(--text-secondary);">' + (etat.progression || 0) + '&nbsp;%</span>'
                 + '</div>';
        } else if (etat.phase === 'installation') {
            html = '<div style="' + styleBandeau('var(--accent)') + '">'
                 + '<strong>⚙ Installation de la version ' + echapper(etat.version_disponible) + '…</strong>'
                 + '<span style="color:var(--text-secondary);">L\'application va redémarrer.</span>'
                 + '</div>';
        } else if (etat.phase === 'pret') {
            html = '<div style="' + styleBandeau('var(--accent-green)') + '">'
                 + '<strong>✓ Version ' + echapper(etat.version_disponible) + ' installée</strong>'
                 + '<span style="color:var(--text-secondary);">'
                 + 'ParcInfo redémarre — rechargez la page dans quelques secondes.</span>'
                 + '</div>';
        } else if (etat.phase === 'erreur' && etat.erreur) {
            html = '<div style="' + styleBandeau('var(--accent-red)') + '">'
                 + '<div style="flex:1 1 320px;"><strong>Mise à jour interrompue</strong><br>'
                 + '<span style="color:var(--text-secondary);">' + echapper(etat.erreur) + '</span></div>'
                 + (etat.url_notes
                    ? '<a href="' + echapper(etat.url_notes) + '" target="_blank" rel="noopener" '
                      + 'style="color:var(--accent);">Télécharger manuellement</a>'
                    : '')
                 + bouton('dismiss', 'Fermer', false)
                 + '</div>';
        } else if (etat.mode === 'docker') {
            var cmd = (etat.commandes && etat.commandes.compose) || '';
            html = '<div style="' + styleBandeau('var(--accent)') + '">'
                 + '<div style="flex:1 1 260px;">'
                 + '<strong>📦 ParcInfo ' + echapper(etat.version_disponible) + ' est disponible</strong>'
                 + '<div style="color:var(--text-secondary);font-size:.82rem;margin-top:.15rem;">'
                 + 'Version installée ' + echapper(etat.version_actuelle)
                 + ' — à mettre à jour depuis l\'hôte Docker</div></div>'
                 + '<code style="flex:1 1 300px;background:var(--bg-secondary);padding:.4rem .6rem;'
                 + 'border-radius:4px;font-size:.8rem;color:var(--accent);user-select:all;">'
                 + echapper(cmd) + '</code>'
                 + bouton('copy', 'Copier', true)
                 + (etat.url_notes
                    ? '<a href="' + echapper(etat.url_notes) + '" target="_blank" rel="noopener" '
                      + 'style="color:var(--accent);font-size:.85rem;">Notes</a>'
                    : '')
                 + bouton('dismiss', 'Plus tard', false)
                 + '</div>';
        } else {
            html = '<div style="' + styleBandeau('var(--accent)') + '">'
                 + '<div style="flex:1 1 300px;">'
                 + '<strong>📦 ParcInfo ' + echapper(etat.version_disponible) + ' est disponible</strong>'
                 + '<div style="color:var(--text-secondary);font-size:.82rem;margin-top:.15rem;">'
                 + 'Version installée ' + echapper(etat.version_actuelle)
                 + (etat.installable ? '' : ' — installation manuelle') + '</div></div>'
                 + (etat.url_notes
                    ? '<a href="' + echapper(etat.url_notes) + '" target="_blank" rel="noopener" '
                      + 'style="color:var(--accent);font-size:.85rem;">Notes de version</a>'
                    : '')
                 + (etat.installable ? bouton('install', 'Installer maintenant', true) : '')
                 + bouton('dismiss', 'Plus tard', false)
                 + '</div>';
        }

        conteneur.innerHTML = html;
        brancherActions(etat);
    }

    function brancherActions(etat) {
        var installer = conteneur.querySelector('[data-action="install"]');
        var ecarter = conteneur.querySelector('[data-action="dismiss"]');
        var copier = conteneur.querySelector('[data-action="copy"]');

        if (installer) {
            installer.addEventListener('click', function () {
                installer.disabled = true;
                installer.textContent = 'Démarrage…';
                envoyer('/api/updates/install');
            });
        }
        if (ecarter) {
            ecarter.addEventListener('click', function () {
                conteneur.innerHTML = '';
                envoyer('/api/updates/dismiss');
            });
        }
        if (copier) {
            copier.addEventListener('click', function () {
                var cmd = (etat.commandes && etat.commandes.compose) || '';
                var fini = function () {
                    copier.textContent = 'Copié ✓';
                    setTimeout(function () { copier.textContent = 'Copier'; }, 2000);
                };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(cmd).then(fini, function () {});
                } else {
                    var champ = document.createElement('textarea');
                    champ.value = cmd;
                    document.body.appendChild(champ);
                    champ.select();
                    try { document.execCommand('copy'); fini(); } catch (e) { /* ignoré */ }
                    document.body.removeChild(champ);
                }
            });
        }
    }

    // ── Réseau ──────────────────────────────────────────────────────────────

    // ── Numéro de version cliquable ─────────────────────────────────────────

    function majPastille(etat) {
        if (!pastilleVersion) { return; }
        var pastille = pastilleVersion.querySelector('.nav-version-pastille');
        var dispo = !!etat.mise_a_jour_disponible;
        // Le point reste affiché même quand la bannière a été écartée : sinon
        // « Plus tard » reviendrait à effacer toute trace de la version en attente.
        if (pastille) { pastille.hidden = !dispo; }
        pastilleVersion.classList.toggle('maj-dispo', dispo);
        pastilleVersion.title = dispo
            ? 'Version ' + etat.version_disponible + ' disponible — cliquer pour l\'installer'
            : 'Version ' + etat.version_actuelle + ' — cliquer pour rechercher une mise à jour';
    }

    function texteVersion(valeur) {
        var num = pastilleVersion && pastilleVersion.querySelector('.nav-version-num');
        if (num) { num.textContent = valeur; }
    }

    function verifierDepuisVersion() {
        if (!pastilleVersion) { return; }
        // Une mise à jour déjà détectée : inutile de réinterroger le dépôt,
        // on remonte à la bannière qui porte le bouton d'installation.
        if (etatCourant && etatCourant.mise_a_jour_disponible) {
            if (etatCourant.masquee) { envoyer('/api/updates/undismiss'); }
            else { rendre(etatCourant); }
            window.scrollTo({ top: 0, behavior: 'smooth' });
            return;
        }

        pastilleVersion.disabled = true;
        texteVersion('recherche…');
        fetch('/api/updates/check', { method: 'POST', credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (etat) {
                pastilleVersion.disabled = false;
                appliquer(etat);
                if (etat.mise_a_jour_disponible) {
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                } else {
                    texteVersion('à jour ✓');
                    setTimeout(function () { texteVersion(versionInitiale); }, 2500);
                }
            })
            .catch(function () {
                pastilleVersion.disabled = false;
                texteVersion('hors ligne');
                setTimeout(function () { texteVersion(versionInitiale); }, 2500);
            });
    }

    function appliquer(etat) {
        etatCourant = etat;
        majPastille(etat);
        rendre(etat);
        // Pendant une installation, on suit de près ; sinon on se fait oublier.
        var actif = etat.phase === 'telechargement' || etat.phase === 'installation';
        planifier(actif ? INTERVALLE_TRAVAIL : INTERVALLE_NORMAL);
    }

    function interroger() {
        fetch('/api/updates/status', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (etat) {
                if (etat) { appliquer(etat); } else { planifier(INTERVALLE_NORMAL); }
            })
            .catch(function () { planifier(INTERVALLE_NORMAL); });
    }

    function afficherRefus(message) {
        // Un refus du serveur — droits insuffisants, mise à jour indisponible —
        // restait invisible : le bouton gardait « Démarrage… » indéfiniment et
        // l'utilisateur n'avait aucun moyen de savoir ce qui bloquait.
        var etat = {}, source = etatCourant || {};
        for (var cle in source) {
            if (Object.prototype.hasOwnProperty.call(source, cle)) { etat[cle] = source[cle]; }
        }
        etat.phase = 'erreur';
        etat.erreur = message || "Le serveur a refusé l'opération";
        etat.progression = 0;
        appliquer(etat);
    }

    function envoyer(url) {
        var statut = 0;
        fetch(url, { method: 'POST', credentials: 'same-origin' })
            .then(function (r) {
                statut = r.status;
                return r.json().catch(function () { return {}; });
            })
            .then(function (corps) {
                if (statut >= 400) {
                    afficherRefus(corps.erreur || corps.message
                        || ('Erreur ' + statut));
                    return;
                }
                if (corps && corps.phase) { appliquer(corps); }
            })
            .catch(function () {
                afficherRefus("Serveur injoignable");
            });
    }

    function planifier(delai) {
        if (minuteur) { clearTimeout(minuteur); }
        minuteur = setTimeout(interroger, delai);
    }

    // ── Démarrage ───────────────────────────────────────────────────────────

    function demarrer() {
        conteneur = elt('update-notification-container');
        pastilleVersion = elt('maj-version');
        if (pastilleVersion) {
            var num = pastilleVersion.querySelector('.nav-version-num');
            versionInitiale = num ? num.textContent : '';
            pastilleVersion.addEventListener('click', verifierDepuisVersion);
        }
        if (!conteneur) { return; }   // page sans bannière (connexion, erreurs)
        interroger();
    }

    // Ce script est chargé en fin de <body> : à cet instant le document est
    // encore en cours d'analyse (readyState « loading »). La version précédente
    // ne s'initialisait QUE si readyState valait autre chose — la bannière ne
    // pouvait donc jamais apparaître.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', demarrer);
    } else {
        demarrer();
    }

    window.ParcInfoMaj = {
        verifier: function () { envoyer('/api/updates/check'); },
        etat: function () { return etatCourant; }
    };
})();
