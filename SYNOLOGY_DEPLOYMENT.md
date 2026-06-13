# 🐳 Guide de Déploiement ParcInfo sur Synology DS1522+

**Version**: 2.6.1+
**Dernière mise à jour**: 2026-06-12

## 📋 Table des matières

1. [Problèmes résolus](#problèmes-résolus)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Troubleshooting](#troubleshooting)

---

## 🔧 Problèmes Résolus

### Version 2.6.1+

**Problème** : Hyper Backup, Centre de paquets, Compte Synology ne se connectent plus après déploiement ParcInfo

**Causes identifiées** :
- ❌ Serveur Werkzeug (développement) utilisé en production
- ❌ Écoute sur `0.0.0.0` sans thread-safety
- ❌ Pas de gestion concurrente des connexions
- ❌ Interferait avec la couche réseau Synology

**Solutions implémentées** :
- ✅ Gunicorn comme serveur WSGI production
- ✅ Workers et threads configurés pour concurrence
- ✅ Entrypoint optimisé pour Synology
- ✅ Health checks robustes
- ✅ Logging amélioré

---

## 🏗️ Architecture

```
Synology DS1522+ avec Container Docker
  → Gunicorn (Production WSGI)
    - 2 workers
    - 2 threads par worker
    - Écoute 0.0.0.0:3456
  → Flask Application
    - Multi-tenant
    - ACL granulaire
  → SQLite Database
    - /data/parc_info.db
    - /data/uploads/

Services Synology (NON affectés) :
  - Hyper Backup ✅
  - Centre de paquets ✅
  - Compte Synology ✅
```

---

## 📦 Installation

### Configuration Gunicorn Recommandée

Pour DS1522+ (4 cores, 8GB RAM) :

```
workers: 2
threads: 2 par worker
total connexions: 4 concurrentes
```

Pour matériel plus faible (2 cores, 4GB RAM) :
```
workers: 1
threads: 2
```

Pour matériel plus puissant (8+ cores, 16GB RAM) :
```
workers: 4
threads: 4
```

---

## 🐛 Problèmes Résolvus

### Hyper Backup, Centre de paquets, Compte Synology ne répondent plus

**Cause** : Ancien serveur Werkzeug interferait avec le réseau

**Solution** : Mettre à jour vers 2.6.1+ qui utilise Gunicorn

```bash
# Vérifier que Gunicorn est actif
docker logs parcinfo | grep Gunicorn
```

---

## 🚀 Accès

```
URL : http://<ip-synology>:3456
```

---

**Compatible avec** : DSM 7.0+, DS1522+, DS1821+, DS923+
