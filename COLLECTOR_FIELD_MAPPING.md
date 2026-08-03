# Collecteur Système - Correspondance des Champs

## 📋 Vue d'ensemble

Ce document détaille la correspondance entre les champs collectés par le collecteur système et les champs disponibles dans la table `appareils` de ParcInfo.

**Généré par :** système-info-collector.py (CLI) / system-info-collector-gui.py (GUI)
**Date :** 2026-08-03

---

## ✅ Champs Collectés et Stockés

| Champ Collecté | Champ BD | Table | Statut | Notes |
|---|---|---|---|---|
| `hostname` | `nom_machine` | appareils | ✅ Stocké | Nom de la machine |
| `mac_address` | `adresse_mac` | appareils | ✅ Stocké | Adresse MAC (identifiant) |
| `ip_addresses[0]` | `adresse_ip` | appareils | ✅ Stocké | Première adresse IP |
| `os_name + os_version` | `os` | appareils | ✅ Stocké | Système d'exploitation complet |
| `os_version` | `version_os` | appareils | ⚠️ Partiellement | Version OS seulement |
| `brand` | `marque` | appareils | ✅ Stocké | Marque du constructeur |
| `model` | `modele` | appareils | ✅ Stocké | Modèle exact |
| `serial_number` | `numero_serie` | appareils | ✅ Stocké | Numéro de série |
| `ram_gb` | `ram` | appareils | ✅ Stocké | RAM totale en GB |
| `cpu` | `cpu` | appareils | ✅ Stocké | Modèle du processeur |
| `disk_total_gb` | `stockage` | appareils | ✅ Stocké | Stockage total en GB |
| `antivirus` | `antivirus` | appareils | ✅ Stocké | Antivirus détecté |
| `installed_software` | `logiciels_installes_json` | appareils | ✅ Stocké | JSON array (max 50) |
| `cpu_cores` | N/A | - | ❌ Non stocké | Pas de colonne dédiée |
| `platform` | N/A | - | ❌ Non stocké | Info système brute |
| `disk_drives` | N/A | - | ❌ Non stocké | Disques individuels (pas de colonne) |

---

## 📊 Rapport HTML Détails

Le rapport HTML généré localement et stocké en tant que document inclut **tous** les champs collectés :

### Sections du Rapport

1. **🔍 Identification** (✅ tous stockés)
   - Hostname → `nom_machine`
   - MAC Address → `adresse_mac`
   - IP Addresses → `adresse_ip` (première)
   - Marque → `marque`
   - Modèle → `modele`
   - Numéro Série → `numero_serie`

2. **🖥️ Système d'Exploitation** (✅ tous stockés)
   - OS → `os`
   - Version → `version_os` + `os`
   - Platform → **Non stocké** (info système brute)

3. **⚙️ Matériel** (⚠️ partiellement)
   - RAM → `ram` ✅
   - CPU → `cpu` ✅
   - CPU Cores → **Non stocké** ❌

4. **💾 Stockage** (⚠️ partiellement)
   - Total Stockage → `stockage` ✅
   - Disques Détaillés → **Non stocké individuellement** ❌

5. **🛡️ Sécurité** (✅ tous stockés)
   - Antivirus → `antivirus` ✅

6. **📦 Logiciels** (✅ stocké avec limite)
   - Installed Software → `logiciels_installes_json` ✅
   - Limite API : 50 premiers
   - Rapport : jusqu'à 200

---

## 🔄 Flux de Données

```
┌─────────────────────────────────────┐
│  Collecte Système (CLI/GUI)         │
│  • WMI (Windows)                    │
│  • system_profiler (macOS)          │
│  • /proc + dmidecode (Linux)        │
└────────────┬────────────────────────┘
             │
             ├──→ [1] Générer Rapport HTML Complet
             │        (tous les champs)
             │        └──→ Sauvegarder localement
             │
             ├──→ [2] Filtrer Payload API
             │        (champs supportés seulement)
             │
             └──→ [3] POST /api/device-info
                      ├─ Créer/Mettre à jour appareil
                      │  avec champs supportés
                      │
                      └─→ POST /api/device-info/upload-report
                         └─ Stocker rapport HTML
                            en tant que document_appareils
```

---

## ⚠️ Champs NON Collectés (But Disponibles dans la BD)

Les champs suivants **existent** dans la table `appareils` mais ne sont **pas remplis** par le collecteur :

| Champ BD | Type | Raison |
|---|---|---|
| `utilisateur` | TEXT | Non disponible en tant qu'utilisateur system |
| `service` | TEXT | Infos métier, non détectables |
| `localisation` | TEXT | Non détectable automatiquement |
| `date_achat` | TEXT | Donnée historique (achat) |
| `duree_garantie` | INTEGER | Métadonnées de garantie |
| `date_fin_garantie` | TEXT | Calculable si date_achat + durée |
| `fournisseur` | TEXT | Donnée métier |
| `prix_achat` | REAL | Donnée historique |
| `numero_commande` | TEXT | Donnée de suivi achat |
| `user_login` | TEXT | Credentials utilisateur |
| `user_password` | TEXT | **Sécurité** - jamais collecter |
| `admin_login` | TEXT | Credentials admin |
| `admin_password` | TEXT | **Sécurité** - jamais collecter |
| `anydesk_id` | TEXT | Tool externe, hors scope |
| `anydesk_password` | TEXT | **Sécurité** - jamais collecter |
| `nom_dns` | TEXT | Détectable via reverse DNS (futur) |
| `ports_ouverts` | TEXT | Scan réseau séparé (futur) |
| `type_appareil` | TEXT | Détecté mais pas spécialisé |

---

## 🎯 Optimisations Futures

### Court terme
- [ ] Collecter `nom_dns` via reverse DNS lookup
- [ ] Détecter type_appareil plus précis (laptop vs desktop vs VM)

### Moyen terme
- [ ] Support scan réseau → `ports_ouverts`
- [ ] Enrichissement WMI → infos constructeur détaillées
- [ ] macOS system_profiler → modèle exact vs identifiant

### Long terme
- [ ] Intégration LDAP/AD → `utilisateur`, `service`
- [ ] Gestion configurations → `localisation` from mobile app
- [ ] Suivi historique → `date_achat`, `prix_achat` (CMDB import)

---

## 🔍 Checklist de Complétude

### Windows (WMI)
- [x] Marque + Modèle (Win32_ComputerSystem)
- [x] Numéro série (Win32_SystemEnclosure)
- [x] RAM totale (Win32_PhysicalMemory)
- [x] CPU + Cores (Win32_Processor)
- [x] Tous les disques (Win32_LogicalDisk)
- [x] Antivirus (Win32_SecurityCenter1)
- [x] Logiciels installés (Registre Uninstall)

### macOS (system_profiler + df)
- [x] Marque (Apple constant)
- [x] Modèle + Identifiant (SPHardwareDataType)
- [x] Numéro série (SPHardwareDataType)
- [x] RAM totale (SPHardwareDataType)
- [x] CPU + Cores (SPHardwareDataType)
- [x] Tous les disques (df -h)
- [ ] Antivirus (requires scanning /Library/Security)
- [x] Applications (ls /Applications + /usr/local/opt + pkgutil)

### Linux (dmidecode + /proc + df)
- [x] Marque + Modèle (dmidecode system)
- [x] Numéro série (dmidecode system)
- [x] RAM totale (/proc/meminfo)
- [x] CPU + Cores (/proc/cpuinfo)
- [x] Tous les disques (df -h)
- [ ] Antivirus (distribution-specific)
- [x] Logiciels installés (dpkg, rpm, pacman)

---

## 📄 Documents Générés

### Rapport HTML Local
- **Nom** : `system-info-report_{hostname}_{mac}_{timestamp}.html`
- **Contenu** : Tous les champs collectés (200+ logiciels max)
- **Stockage** : Répertoire courant (ou variable d'env REPORT_DIR)
- **Badges** : Indique champs "API ✓" vs "Non stocké"

### Document ParcInfo
- **Table** : `documents_appareils`
- **Type** : `rapport_html`
- **Nom** : `Rapport Système - {timestamp}`
- **Description** : "Rapport HTML complet collecté par système-info-collector"
- **Contenu** : `contenu_blob` (HTML text)
- **Lié à** : Appareil créé/mis à jour (appareil_id)

---

## 🚀 Utilisation

### CLI
```bash
# Collecter + envoyer + générer rapport + uploader
python system-info-collector.py --server http://parcinfo.local:3456 --client-id 1

# Mode silencieux (rapports locaux seulement)
python system-info-collector.py --quiet
```

### GUI
```bash
# Interface graphique interactive
python system-info-collector-gui.py --server http://parcinfo.local:3456

# Sélectionner client → affiche rapport preview → envoyer → documents créés
```

---

## 📞 Support

Pour ajouter des champs :
1. Vérifier disponibilité dans la table `appareils`
2. Implémenter collection dans les 3 fonctions OS (Windows/macOS/Linux)
3. Ajouter au `get_api_payload()` pour l'API
4. Inclure dans rapport HTML
5. Tester sur les 3 OS

---

**Dernière mise à jour** : 2026-08-03
**Version Collecteur** : v2.1
**Version ParcInfo** : v2.6.24+
