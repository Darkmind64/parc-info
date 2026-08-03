# Guide d'Implémentation : Auto-Remplissage Système (v2.6.24)

## 📋 Vue d'ensemble

ParcInfo dispose maintenant d'une **solution complète d'auto-remplissage** qui collecte automatiquement les infos système de chaque machine et les enregistre dans l'inventaire.

### Composants

| Composant | Fichier | Rôle |
|-----------|---------|------|
| **A) Collecteur autonome** | `system-info-collector.py` | Script exécutable sur chaque machine |
| **B) Endpoint API** | `app.py:api_device_info()` | Reçoit les infos du collecteur |
| **C) Enrichissement scan** | `app.py:_enrich_from_wmi()` | Enrichit le scan réseau avec WMI |

---

## 🚀 SOLUTION A : Collecteur Autonome (Recommandé)

### Qu'est-ce que c'est ?

Script Python léger (`system-info-collector.py`) qui :
- S'exécute sur **Windows, macOS, Linux**
- Collecte **toutes les infos système** (marque, modèle, S/N, RAM, CPU, disque, antivirus, logiciels)
- Envoie à ParcInfo via HTTP
- Crée ou met à jour **automatiquement** l'appareil correspondant

### Installation

#### 1. Télécharger le script

Le script se trouve dans le répertoire racine de ParcInfo :

```bash
# Windows PowerShell
curl -o system-info-collector.py http://parcinfo.local:3456/static/system-info-collector.py

# Linux/macOS
curl -o system-info-collector.py http://parcinfo.local:3456/static/system-info-collector.py
```

Ou télécharger directement depuis : `/static/system-info-collector.py`

#### 2. Exécuter sur chaque machine

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

### Données Collectées

#### ✅ Toujours Collectées

| Champ | Source | Exemple |
|-------|--------|---------|
| MAC Address | UUID Python | `00:1A:2B:3C:4D:5E` |
| Hostname | `socket.gethostname()` | `DESKTOP-ABC123` |
| IP Addresses | Socket DNS | `192.168.1.100` |
| OS | `platform.system()` | `Windows` |
| OS Version | Registry/Release | `11 (22H2)` |
| CPU | WMI / sysctl | `Intel Core i7-1185G7` |
| CPU Cores | WMI / cpuinfo | `4` |
| RAM | WMI / /proc/meminfo | `16` GB |
| Disque | WMI / df | `512` GB |

#### ⚠️ Selon la Plateforme

| Champ | Windows | macOS | Linux | Source |
|-------|---------|-------|-------|--------|
| Marque | ✅ | ✅ | ✅ | WMI / system_profiler / dmidecode |
| Modèle | ✅ | ✅ | ✅ | WMI / system_profiler / dmidecode |
| Numéro S/N | ✅ | ✅ | ⚠️ | WMI / system_profiler / dmidecode (sudo) |
| Antivirus | ✅ | ❌ | ❌ | WMI Win32_SecurityCenter1 |
| Logiciels | ✅ (limité 50) | ✅ | ✅ | Registry / Applications / dpkg/rpm |

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

Actuellement **pas d'authentification requise** (collecteur s'exécute localement sur la machine).

Pour ajouter une sécurité :
```python
# À implémenter si nécessaire
if data.get('token') != cfg_get('collector_token'):
    return {"error": "Unauthorized"}, 401
```

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

- [ ] Ajouter download automatique du collecteur depuis l'UI
- [ ] Ajouter scheduling (tâche quotidienne automatique)
- [ ] Ajouter détection antivirus sur Linux/macOS
- [ ] Ajouter détection EDR/RMM
- [ ] Ajouter détection des mises à jour Windows
- [ ] Ajouter détection libre disque (pas juste taille totale)
- [ ] Intégration RMM (si agent AnyDesk/TeamViewer/ConnectWise)

---

**Version:** 2.6.24  
**Dernière mise à jour:** 2026-08-03
