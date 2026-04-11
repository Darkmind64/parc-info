# Problème de Connexion - Analyse et Correction

**Date:** 2026-04-09  
**Problème:** "Je ne peux plus me connecter"  
**Status:** ✅ **RÉPARÉ**

---

## Problème Identifié

L'initialisation des modifications de WAL mode et des fonctions de configuration a révélé un **bug critique d'initialisation de chemin de base de données**.

### Cause Racine

Quand `config_helpers.py` (ou tout autre module) importait directement `database.py` **avant que `app.py` n'initialise** les chemins globaux, la variable `DATABASE` restait vide (`''`).

**Flux d'initialisation INCORRECT:**
```python
# 1. Importer config_helpers
from config_helpers import cfg_get  # ← Importe database.py
    ↓
# 2. database.py s'initialise avec DATABASE = ''
import database  # ← DATABASE = '' (non initialisé)

# 3. Plus tard, app.py tente d'initialiser
import app  # ← app.py définit _db_module.DATABASE = '/path/to/parc_info.db'
    ↓
# 4. Mais config_helpers a déjà importé database.py avec DATABASE = ''!
```

### Conséquences

1. `_local_db()` appelait `sqlite3.connect('')` (chemin vide)
2. Cela créait/ouvrait une base de données vide dans le répertoire courant
3. La vraie base de données (`parc_info.db`) n'était jamais accessible
4. Les requêtes échouaient avec "no such table: auth_users"
5. La connexion était impossible

---

## Correction Appliquée

### Modification: `database.py:_local_db()`

**Code AVANT:**
```python
def _local_db():
    import database as _self
    conn = sqlite3.connect(_self.DATABASE)  # ← BUG: DATABASE peut être ''
    conn.row_factory = sqlite3.Row
    ...
```

**Code APRÈS:**
```python
def _local_db():
    import database as _self
    import os

    # Déterminer le chemin de la base de données
    db_path = _self.DATABASE
    if not db_path:  # ← CORRECTION: Si DATABASE est vide
        # Fallback: chercher parc_info.db dans le répertoire courant du module
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'parc_info.db')

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.create_function('ip_sort_key', 1, _ip_sort_key)
    # ... reste du code
```

### Améliorations Incluses

1. **Fallback robuste** — Si DATABASE n'est pas initialisé, utilise un chemin par défaut
2. **Try-except sur WAL** — En cas d'erreur, continue avec le mode par défaut
3. **Commit après PRAGMA** — Commit explicite pour WAL mode (transaction management)

---

## Résumé des Fichiers Modifiés

| Fichier | Problème | Solution |
|---------|----------|----------|
| `database.py:_local_db()` | DATABASE vide → connection invalide | Ajouterchemin fallback + gestion erreurs |
| `config_helpers.py` | Utilisait get_db() au lieu de get_local_db() | ✅ Déjà corrigé dans session précédente |
| `app.py` | Ajout retry_db_query() pour resilience | ✅ Déjà implémenté |

---

## Vérification de la Correction

Tous les tests diagnostiques passent maintenant:

✅ **[TEST 4] Python Database Module**
- `_local_db()`: Connection
- Auth users: 1
- `get_local_db()`: Connection
- [OK]

✅ **[TEST 5] Config Helpers**
- `cfg_get('db_type')`: local
- `cfg_all()` items: 102
- `cfg_set/cfg_get`: OK
- [OK]

✅ **[TEST 6] Flask App**
- Flask app: app
- App context connection: OK
- Auth users: 1
- [OK]

✅ **[TEST 7] Login Flow**
- POST /login: OK (redirect)
- GET /: OK (page loaded)
- [OK]

---

## Pourquoi Cela s'est Produit

1. **Modification WAL mode** — J'ai ajouté WAL mode à `_local_db()`
2. **Modification config_helpers.py** — J'ai changé `get_db()` → `get_local_db()`
3. **Chaîne d'imports** — Ces changements ont exposé un bug existant d'initialisation

Le bug existait probablement déjà mais n'était pas exposé dans les scénarios normaux parce que `get_db()` était utilisé en première (qui gère le fallback correctement).

---

## Leçons Apprises

1. **Initialisation globale** — Les variables globales doivent avoir des fallbacks robustes
2. **Chemins de fichiers** — Utiliser des chemins absolus ou des fallbacks constructifs
3. **Tests d'importation** — Tester les imports dans différents ordres

---

## Statut: ✅ COMPLÈTEMENT RÉPARÉ

L'application peut maintenant être utilisée normalement:
- ✅ Page de login charge
- ✅ Connexion fonctionnelle
- ✅ Accès à la base de données fonctionnel
- ✅ Configuration fonctionne
- ✅ Synchronisation Turso compatible

Aucune autre modification n'est nécessaire. Le système est prêt pour utilisation.
