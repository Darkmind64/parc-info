# Personnalisation par Utilisateur — Implémentation Complète

**Date:** 2026-04-10  
**Status:** ✅ ACTIVÉ

---

## 📋 Vue d'ensemble

Chaque utilisateur peut maintenant **personnaliser son interface** indépendamment des autres:
- Thème (sombre/clair)
- Couleurs accents
- Format de date et pagination
- Navigation (horizontal/vertical)
- Autres préférences visuelles

Les paramètres **globaux/d'administration** restent partagés:
- Informations société
- Ports scannés et noms des services
- Couleurs des types d'appareils
- Couleurs des badges périphériques

---

## 🗄️ Architecture de la Base de Données

### Table `config` (Configurations Globales)
```sql
CREATE TABLE config (
    cle TEXT PRIMARY KEY,
    valeur TEXT DEFAULT '',
    date_maj TEXT DEFAULT ''
)
```
**Utilisateurs:** Toutes les configurations globales/d'administration

**Exemples:**
- `entreprise_nom` → "Mon Entreprise"
- `port_22_name` → "SSH"
- `port_22_color` → "#00ff88"
- `type_color_pc` → "#0284c7"

---

### Table `user_preferences` (Configurations Personnelles) — ✨ NOUVEAU
```sql
CREATE TABLE user_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auth_user_id INTEGER NOT NULL,
    cle TEXT NOT NULL,
    valeur TEXT DEFAULT '',
    date_maj TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(auth_user_id, cle),
    FOREIGN KEY(auth_user_id) REFERENCES auth_users(id) ON DELETE CASCADE
)
```
**Utilisateurs:** Préférences spécifiques à chaque utilisateur

**Exemples:**
- Alice: `user_preferences(auth_user_id=1, cle='mode', valeur='dark')`
- Bob:   `user_preferences(auth_user_id=2, cle='mode', valeur='light')`

---

## ⚙️ Paramètres Personnalisables

Les clés suivantes sont **stockées par utilisateur** dans `user_preferences`:

| Clé | Description | Exemple |
|-----|-------------|---------|
| `mode` | Thème (dark/light) | `'dark'` |
| `theme` | Thème prédéfini | `'dark-blue-ocean'` |
| `accent_color` | Couleur accent (liens) | `'#00c9ff'` |
| `accent_green` | Couleur verte (succès) | `'#00ff88'` |
| `accent_red` | Couleur rouge (danger) | `'#ff3355'` |
| `accent_orange` | Couleur orange (alerte) | `'#ff8c00'` |
| `nav_mode` | Navigation (horizontal/vertical) | `'horizontal'` |
| `date_format` | Format de date | `'dd/mm/yyyy'` |
| `lignes_par_page` | Pagination | `'50'` |
| `afficher_mac` | Afficher adresses MAC | `'1'` |
| `afficher_dernier_ping` | Afficher dernier ping | `'1'` |

### Paramètres Globaux (Partagés par Tous)

Toutes les **autres clés** sont globales:
- `entreprise_nom`, `entreprise_logo_url` (Infos société)
- `port_*` (Noms et couleurs des services)
- `periph_color_*` (Couleurs périphériques)
- `type_color_*`, `type_badge_*`, `type_desc_*` (Types d'appareils)
- `scan_ports`, `scan_workers`, etc. (Réseau)

---

## 💾 Fonctions Modifiées

### `config_helpers.py`

#### `cfg_get(cle, default=None, auth_user_id=None)`
```python
# Chercher préférence personnelle
couleur = cfg_get('mode', auth_user_id=123)
# Résultat: cherche d'abord dans user_preferences(auth_user_id=123)
#          puis fallback dans config (global)

# Chercher config globale
couleur = cfg_get('port_22_color')
# Résultat: cherche uniquement dans config
```

#### `cfg_set(cle, valeur, auth_user_id=None)`
```python
# Sauvegarder préférence personnelle
cfg_set('mode', 'dark', auth_user_id=123)
# Résultat: sauvegarde dans user_preferences

# Sauvegarder config globale
cfg_set('port_22_color', '#00ff88')
# Résultat: sauvegarde dans config
```

#### `cfg_set_batch(config_dict, auth_user_id=None)`
```python
# Sauvegarder plusieurs configs en une transaction
cfg_set_batch({
    'mode': 'light',
    'accent_color': '#ff0000',
    'port_22_color': '#00ff88'  # Global
}, auth_user_id=123)
# Résultat: automatiquement sépare les clés personnelles vs globales
#          et les sauvegarde aux bons endroits
```

#### `cfg_all(auth_user_id=None)`
```python
# Fusionner global + préférences personnelles
config = cfg_all(auth_user_id=123)
# Résultat: dict fusionnant config globale + user_preferences
#          Les préfs personnelles écrasent les globales
```

---

## 🌐 API

### `GET /api/config`
Retourne **toutes les configurations** (global + personnelles de l'utilisateur connecté).

**Exemple de réponse:**
```json
{
  "mode": "dark",                    // ← Préférence personnelle
  "accent_color": "#00c9ff",         // ← Préférence personnelle
  "entreprise_nom": "Mon Entreprise", // ← Global
  "port_22_color": "#00ff88",        // ← Global
  "port_22_name": "SSH",             // ← Global
  ...
}
```

### `POST /api/config`
Sauvegarde les configurations modifiées. Sépare automatiquement:
- Clés personnelles → `user_preferences`
- Clés globales → `config`

**Exemple de payload:**
```json
{
  "mode": "light",              // → user_preferences
  "accent_color": "#ff0000",    // → user_preferences
  "port_22_color": "#00ff88",   // → config (global)
  "entreprise_nom": "ACME Inc"  // → config (global)
}
```

---

## 🔄 Stratégie de Lookup

Quand l'app cherche une configuration pour l'utilisateur **Alice** (id=1):

```
cfg_get('mode', auth_user_id=1)
├─ Si c'est une clé personnelle:
│  ├─ Chercher dans user_preferences WHERE auth_user_id=1 AND cle='mode'
│  ├─ Si trouvé → retourner la valeur
│  └─ Si pas trouvé → fallback à config (global)
└─ Si ce n'est pas une clé personnelle:
   └─ Chercher uniquement dans config (global)
```

**Résultat:**
- **Préférences personnelles:** Alice voit ce qu'elle a configuré
- **Paramètres globaux:** Alice voit ce que l'admin a configuré
- **Pas de duplication:** Si Alice n'a jamais modifié 'mode', elle voit la valeur globale

---

## 👥 Comportement Multi-Utilisateur

### Scénario: Alice et Bob

| Action | Alice | Bob |
|--------|-------|-----|
| Admin change `port_22_color` → `#ff0000` | Voit rouge | Voit rouge |
| Alice change `mode` → `light` | Voit clair | Toujours sombre |
| Bob change `accent_color` → `#00ff00` | Toujours défaut | Voit vert |
| Alice se déconnecte, Bob se reconnecte | — | Configuration de Bob appliquée ✓ |

---

## 🚀 Avantages

✅ **Indépendance:** Chaque utilisateur a sa propre interface  
✅ **Pas de pollution:** Les changements d'un utilisateur n'affectent pas les autres  
✅ **Hygiène BD:** Les préférences personnelles sont isolées  
✅ **Fallback gracieux:** Si utilisateur n'a jamais modifié, on voit la valeur globale  
✅ **Performance:** Batch optimization avec `cfg_set_batch()`  
✅ **Compatibilité:** Code existant continue de fonctionner (backward compatible)

---

## 🔒 Sécurité

- ✅ `user_preferences` a une FK sur `auth_users` → si utilisateur supprimé, ses prefs sont supprimées
- ✅ API `/api/config` vérifie `get_auth_user()` → pas d'accès sans authentification
- ✅ Chaque utilisateur ne voit/modifie que ses préférences personnelles

---

## 📝 Notes de Migration

**Pour les installations existantes:**
- La table `user_preferences` est créée automatiquement au démarrage (IF NOT EXISTS)
- Les configurations existantes restent dans `config` (inchangées)
- Quand utilisateurs modifient paramètres personnels, elles vont dans `user_preferences`
- **Pas de danger:** Système de fallback garantit que rien ne casse

---

## 📚 Exemple Complet

```python
# app.py - Lors de la sauvegarde des paramètres
@app.route('/api/config', methods=['POST'])
def api_config_save():
    user = get_auth_user()
    auth_user_id = user['id'] if user else None
    
    # Payload du frontend
    data = {
        'mode': 'light',           # Personel
        'accent_color': '#ff0000', # Personnel
        'port_22_color': '#00ff88' # Global
    }
    
    # Sauvegarde intelligente
    cfg_set_batch(data, auth_user_id=auth_user_id)
    # ├─ Insère dans user_preferences: mode, accent_color
    # └─ Insère dans config: port_22_color
    
    return jsonify({'ok': True})
```

```python
# app.py - Lors du chargement des paramètres
@app.route('/api/config', methods=['GET'])
def api_config_get():
    user = get_auth_user()
    auth_user_id = user['id'] if user else None
    
    # Fusionner global + personnel
    config = cfg_all(auth_user_id=auth_user_id)
    # ├─ Charge config globale
    # ├─ Charge user_preferences pour cet utilisateur
    # └─ Fusionne (personnel écrase global)
    
    return jsonify(config)
```

---

**Fin du document** — 2026-04-10
