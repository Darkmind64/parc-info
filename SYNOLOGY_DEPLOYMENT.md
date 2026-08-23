# 🐳 Guide de Déploiement ParcInfo sur Synology DS1522+

**Version** : 2.18.43
**Dernière mise à jour** : 2026-08-23

## 📋 Table des matières

1. [Historique : Werkzeug, pas Gunicorn](#historique--werkzeug-pas-gunicorn)
2. [Architecture](#architecture)
3. [Accès](#accès)

---

## 🔧 Historique : Werkzeug, pas Gunicorn

**Corrigé ici : ce document affirmait l'inverse de ce que fait le code
actuel.** Un passage à Gunicorn avait bien été tenté (v2.6.1), mais annulé
quasi aussitôt (v2.6.6) — le code Docker utilise **Werkzeug directement**
depuis, avec le commentaire explicite `# En Docker, utiliser Werkzeug
directement (plus stable que Gunicorn)` dans `app.py`.

**Ce qui s'est réellement passé, dans l'ordre :**

1. **v2.6.1** : Hyper Backup / Centre de paquets / Compte Synology
   cessaient de répondre après déploiement. Gunicorn est introduit comme
   correctif supposé.
2. **v2.6.2** : les workers Gunicorn plantent avec le code 255 (exception
   non gérée dans son initialisation), même après être passé de la classe
   `sync` à `gthread`.
3. **v2.6.6** : retour à Werkzeug **directement**, correctement configuré
   (`threaded=True`, `use_reloader=False`, `host='0.0.0.0'`) — plus simple,
   et stable depuis sans régression rapportée sur Hyper Backup / Centre de
   paquets / Compte Synology.

Ce n'était donc pas le choix du serveur WSGI en lui-même qui posait
problème à l'origine, mais probablement l'absence de `use_reloader=False`
(le rechargeur de Werkzeug duplique le processus, ce qui peut perturber la
gestion réseau/processus d'un NAS) — Gunicorn, lui, s'est révélé une cause
de panne distincte et plus grave. `docker-entrypoint.sh` installe encore
`gunicorn` par précaution mais ne l'invoque jamais (`exec python app.py`
uniquement) ; ce n'est plus qu'un vestige, sans conséquence.

---

## 🏗️ Architecture

```
Synology DS1522+ avec Container Docker
  → Werkzeug (threaded=True, use_reloader=False)
    - Écoute 0.0.0.0:3456
  → Flask Application
    - Multi-tenant
    - ACL granulaire
  → SQLite Database (+ synchronisation Turso optionnelle)
    - /data/parc_info.db
    - /data/uploads/

Services Synology (non affectés depuis v2.6.6) :
  - Hyper Backup ✅
  - Centre de paquets ✅
  - Compte Synology ✅
```

---

## 🚀 Accès

```
URL : http://<ip-synology>:3456
```

---

**Compatible avec** : DSM 7.0+, DS1522+, DS1821+, DS923+ (et tout NAS
Synology capable de faire tourner Docker/Container Manager)
