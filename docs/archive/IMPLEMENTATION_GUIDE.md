# Guide d'Implémentation : Auto-Remplissage Système

## 📋 Vue d'ensemble

ParcInfo dispose maintenant d'une **solution complète d'auto-remplissage** qui collecte automatiquement les infos système de chaque machine et les enregistre dans l'inventaire.

### Composants

| Composant | Fichier | Rôle |
|-----------|---------|------|
| **A) Collecteur autonome** | `system-info-collector.py` | Script exécutable sur chaque machine |
| **B) Endpoint API** | `app.py:api_device_info()` | Reçoit les infos du collecteur |
| **C) Enrichissement scan** | `app.py:_enrich_from_wmi()` | Enrichit le scan réseau avec WMI |

---

## 🚀 SOLUTION A : Collecteur Autonome (2 Versions)

### Qu'est-ce que c'est ?

Script Python léger (`system-info-collector.py`) qui :
- S'exécute sur **Windows, macOS, Linux**
- Collecte **toutes les infos système** (marque, modèle, S/N, RAM, CPU, disque, antivirus, logiciels)
- Envoie à ParcInfo via HTTP
- Crée ou met à jour **automatiquement** l'appareil correspondant

### Installation

#### 1. Télécharger le Collecteur

**Deux formats disponibles :**

### 🎉 Exécutables Autonomes (RECOMMANDÉ) - Pas Python requis

| Version | OS | Télécharger | Taille |
|---------|----|-----------:|--------|
| **GUI** ⭐ | Windows | [system-info-collector-gui.exe](https://github.com/Darkmind64/parc-info/releases/latest/download/system-info-collector-gui.exe) | 15.6 MB |
| **CLI** | Windows | [system-info-collector.exe](https://github.com/Darkmind64/parc-info/releases/latest/download/system-info-collector.exe) | 12.5 MB |

**Usage :**
```bash
# GUI - Double-clic pour lancer
system-info-collector-gui.exe

# CLI - Avec options
system-info-collector.exe --client-id 5
system-info-collector.exe --client-name "Mon Entreprise"
```

### Ou Scripts Python (si Python est disponible)

```bash
# Télécharger GUI (archive ZIP : script + collector_core.py, pas un .py seul)
curl -o system-info-collector-gui.zip http://parcinfo.local:3456/download/system-info-collector-gui
unzip system-info-collector-gui.zip && python system-info-collector-gui.py

# Télécharger CLI
curl -o system-info-collector.zip http://parcinfo.local:3456/download/system-info-collector
unzip system-info-collector.zip && python system-info-collector.py --client-id 5
```

### 2a. Version GUI (Recommandée pour les utilisateurs) ⭐

**Avantages :**
- ✅ Pas de ligne de commande à mémoriser
- ✅ Interface intuitive
- ✅ Aperçu des données avant envoi
- ✅ Liste des clients disponibles
- ✅ Confirmation avant transmission

**Utilisation :**

```bash
# Simplement exécuter
python system-info-collector-gui.py
```

**Ce que ça fait :**
1. Lance une fenêtre
2. Affiche la liste des clients disponibles
3. Affiche les informations collectées (marque, OS, RAM, etc.)
4. Demande de sélectionner le client cible
5. Affiche un résumé formaté
6. Demande confirmation avant d'envoyer

**Capture d'écran (exemple) :**
```
┌─────────────────────────────────────────────────────┐
│ ParcInfo - Collecteur d'Informations Système        │
├─────────────────────────────────────────────────────┤
│ 1. Sélectionner le Client Cible                     │
│    [Dropdown: 1 - Mon Entreprise ▼]                 │
│    ⚠️ IMPORTANT : Choisir le bon client             │
├─────────────────────────────────────────────────────┤
│ 2. Informations Collectées                          │
│ ═══════════════════════════════════════════════════ │
│ IDENTIFICATION                                       │
│  Hostname            : DESKTOP-ABC123               │
│  MAC Address         : 00:1A:2B:3C:4D:5E            │
│  IP Address(es)      : 192.168.1.100                │
│  Marque              : Dell                         │
│  Modèle              : Latitude 5420                │
│  Numéro Série        : ABC123XYZ                    │
│                                                     │
│ SYSTÈME D'EXPLOITATION                              │
│  OS                  : Windows                      │
│  Version             : 11 (22H2)                    │
│                                                     │
│ MATÉRIEL                                            │
│  RAM                 : 16 GB                        │
│  CPU                 : Intel Core i7-1185G7         │
│  CPU Cores           : 4                            │
│  Stockage            : 512 GB                       │
│                                                     │
│ SÉCURITÉ                                            │
│  Antivirus           : Windows Defender             │
│ ═══════════════════════════════════════════════════ │
├─────────────────────────────────────────────────────┤
│ [🔄 Rafraîchir] [✓ Envoyer] [✕ Annuler]            │
├─────────────────────────────────────────────────────┤
│ Prêt à envoyer ✓                                    │
└─────────────────────────────────────────────────────┘
```

### 2b. Version CLI (Pour automatisation)

**Avantages :**
- ✅ Intégrable dans scripts
- ✅ Déploiement en masse (Group Policy, MDM)
- ✅ Options en ligne de commande

**Spécifier le client cible :**

```bash
# Par ID du client (recommandé)
python system-info-collector.py --client-id 5

# Par nom du client
python system-info-collector.py --client-name "Mon Entreprise"

# Avec token d'authentification (sécurisé)
python system-info-collector.py --client-id 5 --token ABC123XYZ

# Serveur personnalisé
python system-info-collector.py --server http://192.168.1.100:3456 --client-id 5
```

⚠️ **Important :** Si `--client-id` ou `--client-name` n'est pas spécifié, le collecteur utilise un client "Découverte réseau" par défaut (attention à la confusion entre clients !)

**Windows:**
```powershell
python system-info-collector.py --client-id 5
python system-info-collector.py --client-name "Mon Entreprise"
```

**macOS:**
```bash
python3 system-info-collector.py --client-id 5
```

**Linux:**
```bash
python3 system-info-collector.py --client-id 5
# Ou avec sudo pour infos système complets :
sudo python3 system-info-collector.py --client-id 5
```

#### 3. Exemple de sortie

```
============================================================
ParcInfo System Information Collector v1.0
============================================================

[*] Collecte des informations système...
    ✓ MAC: 00:1A:2B:3C:4D:5E
    ✓ Hostname: DESKTOP-ABC123
    ✓ IP(s): 192.168.1.100
    ✓ OS: Windows 11 (Enterprise)
    ✓ RAM: 16 GB
    ✓ CPU: Intel Core i7-1185G7 (4 cores)
    ✓ Disque: 512 GB
    ✓ Logiciels détectés: 47

[*] Envoi à http://parcinfo.local:3456...
    ✓ Succès!

[+] Résultat :
{
  "status": "success",
  "action": "created",
  "device_id": 42,
  "message": "Nouvel appareil créé (ID: 42)",
  "mac_address": "00:1A:2B:3C:4D:5E",
  "ip_address": "192.168.1.100",
  "hostname": "DESKTOP-ABC123"
}

Le système a été enregistré dans ParcInfo.
```

### Comparaison GUI vs CLI

| Critère | GUI (Recommandé) | CLI |
|---------|------------------|-----|
| **Facilité** | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| **Conforme non-technicien** | ✅ | ❌ |
| **Aperçu avant envoi** | ✅ | ❌ |
| **Sélection client facile** | ✅ (dropdown) | ⚠️ (paramètre) |
| **Déploiement auto** | ❌ | ✅ |
| **Script/MDM** | ❌ | ✅ |
| **Format autonome** | ✅ .exe (15 MB) | ✅ .exe (12 MB) |
| **Python requis** | ❌ | ❌ |
| **Support OS** | Windows/macOS/Linux | Windows/macOS/Linux |

**Recommandation :**
- **Utilisateurs Windows individuels** → [system-info-collector-gui.exe](https://github.com/Darkmind64/parc-info/releases/latest/download/system-info-collector-gui.exe) (double-clic)
- **Déploiement masse Windows** → [system-info-collector.exe](https://github.com/Darkmind64/parc-info/releases/latest/download/system-info-collector.exe) via GPO/MDM
- **macOS/Linux** → Scripts Python (Python 3.8+ requis)

---

### Découvrir les IDs des Clients

1. **Via l'interface ParcInfo :**
   - Aller à : Inventaire → Parc Général
   - Noter l'ID du client dans l'URL ou les détails

2. **Via SQLite (direct) :**
   ```bash
   sqlite3 parc_info.db "SELECT id, nom FROM clients;"
   # Résultat :
   # 1|Mon Entreprise
   # 2|Client A
   # 3|Client B
   ```

3. **Via API :**
   ```bash
   curl http://parcinfo.local:3456/api/clients
   # (nécessite authentification)
   ```

### Options Additionnelles

```bash
# Mode silencieux (pas d'affichage)
python system-info-collector.py --client-id 5 --quiet

# Serveur personnalisé + port
python system-info-collector.py --server http://192.168.1.100:5000 --client-id 5

# Debug verbose
python system-info-collector.py --client-id 5  # (affichage complet par défaut)
```

---

## 📄 Rapport HTML Généré

Chaque collecte génère **deux artefacts** :

### 1️⃣ Rapport HTML Local

**Fichier :** `system-info-report_{hostname}_{mac}_{timestamp}.html`

**Contenu :**
- ✅ **Toutes les infos** collectées (même non-stockées en BD)
- ✅ **Formatage professionnel** : sections, badges, CSS
- ✅ **Badges** : indique "API ✓" vs "Non stocké"
- ✅ **Détail complet** : jusqu'à 200 logiciels, tous les disques

**Sections :**
1. **🔍 Identification** : hostname, MAC, IP, brand, model, serial
2. **🖥️ Système d'Exploitation** : OS, version, platform
3. **⚙️ Matériel** : RAM, CPU, cores
4. **💾 Stockage** : disques individuels (C:, D:, etc.) + total
5. **🛡️ Sécurité** : antivirus détecté
6. **📦 Logiciels** : liste complète (200+)
7. **📋 Métadonnées** : timestamp, client, champs supportés

**Exemple :**
```html
HTML formaté professionnel avec :
- Identité visuelle (header, footer)
- Tableaux avec code couleur
- Badges "API ✓" en vert / "Non stocké" en jaune
- Sections collapsibles (JavaScript)
```

### 2️⃣ Document ParcInfo (Stocké en BD)

**Endpoint :** POST `/api/device-info/upload-report`

**Où :** Détail Appareil → Onglet "Documents"

**Propriétés :**
- **Type** : Rapport HTML système
- **Description** : "Rapport HTML complet collecté par système-info-collector"
- **Format** : HTML text (table `documents_appareils.contenu_blob`)
- **Lié à** : Appareil créé/mis à jour
- **Accessible** : En lecture depuis l'UI ParcInfo

**Avantages :**
- 📚 Historique complet persisté
- 🔗 Tracabilité (qui a collecté, quand)
- 💾 Sauvegarde centralisée
- 🔍 Recherche/audit facile

---

## 🗺️ Correspondance des Champs

**Voir le document complet :** [COLLECTOR_FIELD_MAPPING.md](COLLECTOR_FIELD_MAPPING.md)

**Données sensibles JAMAIS collectées :**
- ❌ Mots de passe (utilisateur, admin, comptes mail, identifiants réseau)
- ❌ Clés administrateur
- ❌ Toute valeur d'un identifiant enregistré — seule sa présence l'est

Ce document ne redonne pas le détail champ par champ (Windows/macOS/Linux,
admin requis ou non) : il a fini par diverger de la réalité au fil des
ajouts. **[COLLECTOR_FIELD_MAPPING.md](COLLECTOR_FIELD_MAPPING.md)** est
l'unique source à jour — plus d'une centaine de champs, classés par thème
(identification, sécurité, accès distant, agents détectés, messagerie,
réseau, diagnostic…), avec la couverture par système d'exploitation.

### Déploiement à Échelle

#### Option 1 : Group Policy (Windows AD)

```powershell
# Créer un GPO qui lance le collecteur en tâche planifiée
# Tous les jours à 9h du matin
```

#### Option 2 : Script de déploiement

```bash
#!/bin/bash
# deploy-collector.sh
SERVER="http://parcinfo.local:3456"
MACHINES="pc-001 pc-002 pc-003"

for machine in $MACHINES; do
    ssh "$machine" "python system-info-collector.py --server $SERVER" &
done
wait
echo "Déploiement terminé"
```

#### Option 3 : MDM / Mobile Device Management

Distribuer via votre MDM (Intune, JumpCloud, etc.)

---

## 📡 SOLUTION B : Endpoint API (`/api/device-info`)

L'endpoint reçoit les données du collecteur et les enregistre dans ParcInfo.

### Spécification

```
POST /api/device-info
Content-Type: application/json

{
  "mac_address": "00:1A:2B:3C:4D:5E",
  "hostname": "DESKTOP-ABC123",
  "ip_addresses": ["192.168.1.100"],
  "os_name": "Windows",
  "os_version": "11",
  "brand": "Dell",
  "model": "Latitude 5420",
  "serial_number": "ABC123XYZ",
  "ram_gb": 16,
  "cpu": "Intel Core i7-1185G7",
  "cpu_cores": 4,
  "disk_total_gb": 512,
  "antivirus": "Windows Defender",
  "installed_software": ["Python", "VS Code", ...],
  "timestamp": "2026-08-03T12:34:56.789012"
}
```

### Réponse Succès

```json
{
  "status": "success",
  "action": "created|updated",
  "device_id": 42,
  "message": "Nouvel appareil créé (ID: 42)",
  "mac_address": "00:1A:2B:3C:4D:5E",
  "ip_address": "192.168.1.100",
  "hostname": "DESKTOP-ABC123"
}
```

### Matching Strategy

L'endpoint cherche une machine existante dans cet ordre :

1. **Par MAC address** (plus fiable)
2. **Par IP address** (si MAC non disponible)
3. **Par hostname** (fallback)
4. **Crée un nouvel appareil** (si aucun match)

### Mise à Jour Intelligente

L'endpoint ne met à jour **que les champs vides** pour ne pas surcharger les données manuelles :

```
Si marque déjà renseignée → ne pas écraser
Si modèle vide → remplir automatiquement
Si serial connu → ne pas changer
```

### Authentification

Optionnelle mais implémentée (`app.py::jeton_collecteur_valide()`) : tant
qu'aucun jeton n'est configuré (Réglages → Collecteur & sauvegardes), tout
collecteur atteignant le serveur peut écrire — comportement historique,
conservé pour ne pas casser les collecteurs déjà déployés sans prévenir.
Dès qu'un jeton est renseigné, il devient obligatoire pour `/api/*` et pour
la liste des clients, envoyé par le collecteur via l'en-tête
`X-Collector-Token` (ou `Authorization: Bearer <jeton>`).

---

## 🔍 SOLUTION C : Enrichissement du Scan Réseau

Enrichit le scan réseau (Inventaire → Scan Réseau) avec WMI pour les machines Windows.

### Utilisation

1. Aller à **Inventaire → Scan Réseau**
2. Entrer la plage IP : `192.168.1.0/24`
3. **Optionnel :** Cocher "Enrichir via WMI"
4. Cliquer "Lancer le scan"

### Via API

```bash
curl -X POST http://parcinfo.local:3456/api/scan/lancer \
  -H "Content-Type: application/json" \
  -d '{
    "plage_ip": "192.168.1.0/24",
    "threads": 30,
    "enrich_wmi": true
  }'
```

### Ce que ça collecte

- Marque (Manufacturer)
- Modèle (Model)
- Numéro de série
- RAM totale
- CPU (modèle + cores)
- Disque principal (C:)

### Limitations

⚠️ **Nécessite :**
- Machine est Windows
- Port RPC (135) accessible
- Peut nécessiter credentials admin
- Ajoute ~2-5 secondes par machine

### Recommandation

👉 **Préférer la Solution A** (collecteur autonome) pour :
- Précision supérieure
- Moins de latence
- Fonctionne même si RPC bloqué
- Collecte antivirus et logiciels

---

## 🎯 Comparaison des Solutions

| Critère | A: Collecteur | B: API | C: WMI Scan |
|---------|---------------|--------|-----------|
| **Couverture données** | 100% | Excellent | Bon |
| **Précision** | Très haute | Très haute | Haute |
| **Latence** | Immédiate | Immédiate | Lente (RPC) |
| **Setup** | Facile | Automatique | Très facile |
| **Antivirus** | ✅ | ✅ | ❌ |
| **Logiciels** | ✅ | ✅ | ❌ |
| **Marque/Modèle** | ✅ | ✅ | ✅ |
| **Nécessite RPC** | ❌ | ❌ | ✅ |
| **Windows seulement** | ❌ | ❌ | ✅ (enrichi) |

---

## 📋 Checklist de Déploiement

### Phase 1 : Test Local

- [ ] Tester `system-info-collector.py` sur Windows
- [ ] Tester sur macOS
- [ ] Tester sur Linux
- [ ] Vérifier que le collecteur se connecte à ParcInfo
- [ ] Vérifier que l'appareil est créé/mis à jour correctement

### Phase 2 : Déploiement Initial

- [ ] Distribuer le script aux administrateurs
- [ ] Former à l'usage
- [ ] Exécuter sur 5-10 machines de test
- [ ] Valider les données remontées

### Phase 3 : Déploiement Complète

- [ ] Optionnel : Configurer un GPO / MDM
- [ ] Ou : Script de déploiement automatique
- [ ] Ou : Distribuer manuellement par site

### Phase 4 : Scan Réseau Enrichi

- [ ] Tester scan avec `enrich_wmi: false` (normal)
- [ ] Tester scan avec `enrich_wmi: true` (enrichi)
- [ ] Valider que ça n'ajoute pas trop de latence

---

## ⚠️ CLIENT CIBLE : CRITIQUE !!!

**TOUJOURS spécifier `--client-id` ou `--client-name`** :

```bash
# ✅ CORRECT - machine créée dans le bon client
python system-info-collector.py --client-id 5

# ✅ AUSSI BON - résolut le nom du client
python system-info-collector.py --client-name "Mon Entreprise"

# ❌ DANGEREUX - machine créée dans "Découverte réseau" (confusion !)
python system-info-collector.py  # Sans --client-id
```

### Pourquoi c'est important ?

- ParcInfo supporte **plusieurs clients** (multi-tenant)
- Chaque client a son propre inventaire
- Si vous oubliez `--client-id`, la machine va **par défaut** au client "Découverte réseau"
- Résultats : **mélange de données, audit trail confus, accès perdus**

### Comment trouver votre Client ID ?

**Avant de lancer le collecteur :**

```bash
# 1. Direct dans SQLite (si accès)
sqlite3 parc_info.db "SELECT id, nom FROM clients;"

# Résultat exemple :
# 1|Mon Entreprise
# 2|Client A
# 3|Client B

# 2. Ou dans ParcInfo UI : Inventaire → Parc Général
# Regarder l'URL ou demander à l'admin
```

**Puis lancez le collecteur avec le bon ID :**

```bash
python system-info-collector.py --client-id 1
```

---

## 🐛 Dépannage

### Le collecteur ne peut pas se connecter

```bash
# Vérifier que ParcInfo est accessible
ping parcinfo.local
curl http://parcinfo.local:3456/

# Ou utiliser l'IP directement
python system-info-collector.py --server http://192.168.1.100:3456
```

### Les données ne sont pas à jour

```bash
# Relancer le collecteur avec le bon client
python system-info-collector.py --client-id 1

# Ou via API avec client_id :
curl -X POST http://parcinfo.local:3456/api/device-info \
  -H "Content-Type: application/json" \
  -d '{
    "mac_address": "00:1A:2B:3C:4D:5E",
    "client_id": 1,
    ...
  }'
```

### La machine s'est créée dans le mauvais client

**Symptôme :** Machine visible dans "Découverte réseau" au lieu de votre client

**Solution :**
1. Vérifier le log du collecteur
2. Relancer avec `--client-id` correct
3. Optionnel : Supprimer la machine mal créée (Inventaire → Appareils → Supprimer)
4. Relancer le collecteur

```bash
# BON DÉPLOIEMENT
python system-info-collector.py --client-id 5 --server http://parcinfo.local:3456
```

### WMI enrichment est lent

- Normal : RPC sur le réseau prend du temps
- Limiter les threads : `--threads 10`
- Ou lancer sans `enrich_wmi`

### Les infos ne remontent pas (Linux)

```bash
# Sur Linux, certaines infos nécessitent sudo
sudo python3 system-info-collector.py
```

---

## 📊 Monitoring

### Voir les collectes reçues

```sql
-- Dans SQLite :
SELECT id, nom_machine, adresse_ip, adresse_mac, derniere_synchro, date_maj
FROM appareils
WHERE derniere_synchro IS NOT NULL
ORDER BY derniere_synchro DESC
LIMIT 20;
```

### Logs du serveur

```bash
# Voir les collectes dans les logs
tail -f parc_info.log | grep "Device info received"
```

---

## 🚀 Améliorations Futures

- [x] Ajouter download automatique du collecteur depuis l'UI — `/download/system-info-collector[-gui]`
- [ ] Ajouter scheduling (tâche quotidienne automatique)
- [ ] Ajouter détection antivirus sur Linux/macOS
- [x] Ajouter détection EDR/RMM — 2.9.7, voir [COLLECTOR_FIELD_MAPPING.md](COLLECTOR_FIELD_MAPPING.md#agents-de-télémaintenance--edr)
- [x] Ajouter détection des mises à jour Windows — `pending_updates[]`, `hotfixes[]`
- [x] Ajouter détection libre disque (pas juste taille totale) — `disk_free_gb`
- [x] Intégration RMM (AnyDesk, TeamViewer, ConnectWise, NinjaOne, Datto, N-able…) — 2.9.7

---

**Version:** 2.18.15
**Dernière mise à jour:** 2026-08-19
