# Collecteur Système — Correspondance des Champs

## 📋 Vue d'ensemble

Ce document détaille ce que collecte le collecteur système ParcInfo et où chaque
donnée atterrit côté serveur.

**Généré par :** `collector_core.py` (logique partagée)
→ `system-info-collector.py` (CLI) et `system-info-collector-gui.py` (GUI)

**Version collecteur :** 3.0 · **Version ParcInfo :** 2.6.31+

---

## 🏗️ Architecture

Depuis la v3.0, **toute la logique de collecte vit dans `collector_core.py`**.
Les deux scripts d'entrée ne portent plus que leur interface (argparse d'un côté,
tkinter de l'autre). Avant cela, les deux fichiers étaient des copies qui avaient
déjà divergé — la version GUI avait silencieusement perdu la détection logicielle
`pkgutil` (macOS) et `pacman` (Arch).

```
collector_core.py           ← collecte, rapports, payload API, appels réseau
├── system-info-collector.py       (CLI : argparse + affichage console)
└── system-info-collector-gui.py   (GUI : tkinter + découverte réseau)
```

⚠️ **Le collecteur n'est plus un fichier autonome.** `collector_core.py` doit
accompagner le script d'entrée. Les routes `/download/system-info-collector[-gui]`
servent donc une **archive ZIP** contenant les deux fichiers.

---

## 🗄️ Destinations côté serveur

### 1. Colonnes dédiées de `appareils`

| Champ collecté | Colonne BD | Notes |
|---|---|---|
| `hostname` | `nom_machine` | |
| `dns_name` | `nom_dns` | FQDN, vide si la résolution échoue |
| `device_type` | `type_appareil` | Déduit du châssis SMBIOS. N'écrase pas un type saisi à la main |
| `mac_address` | `adresse_mac` | Clé de rapprochement principale |
| `ip_addresses[0]` | `adresse_ip` | |
| `brand` / `model` / `serial_number` | `marque` / `modele` / `numero_serie` | Valeurs de gabarit OEM filtrées |
| `os_name` / `os_version` | `os` / `version_os` | |
| `ram_gb` | `ram` | Envoyé formaté (« 16 Go ») |
| `cpu` | `cpu` | |
| `disk_total_gb` | `stockage` | |
| `gpu` | `carte_graphique` | Avec VRAM quand elle est lisible |
| `antivirus` | `antivirus` | |
| `open_ports` | `ports_ouverts` | Ports TCP en écoute, format identique au scan réseau |
| `installed_software` | `logiciels_installes_json` | Liste complète, plafond 2000 entrées |
| *(tout le reste)* | `rapport_systeme_json` | **Snapshot JSON complet**, plafond 1 Mo |
| *(horodatage)* | `derniere_synchro` | Mis à jour à chaque collecte |

### 2. Table `peripheriques` (création automatique)

| Source | Catégorie | Dédoublonnage |
|---|---|---|
| `monitors[]` | `Ecran` | Numéro de série EDID ; entrées sans modèle ignorées |
| `printers[]` | `Imprimante` | Marque + modèle ; **imprimantes virtuelles exclues** |

Le rattachement se fait via `peripheriques_appareils`. L'opération est
**idempotente** : relancer le collecteur ne crée aucun doublon.

Les imprimantes virtuelles (Print to PDF, XPS Document Writer, fax, OneNote,
AnyDesk, PDFCreator…) restent visibles dans le rapport mais sont exclues de
l'inventaire matériel — sans ce filtre, un poste Windows standard injecte 6 à 8
fausses imprimantes à chaque collecte.

### 3. Page « Fiche système »

`/appareil/<id>/fiche-systeme` restitue l'intégralité de `rapport_systeme_json` :
identification, processeur/carte mère, mémoire par slot, stockage et usure,
affichage/impression, batterie, sécurité, licences, réseau, comptes, correctifs
et logiciels (avec filtre de recherche).

Accessible depuis le bouton **🖥 Fiche système** de la fiche appareil.

---

## 📦 Données collectées

### Identification
`hostname` · `dns_name` · `mac_address` · `ip_addresses` · `brand` · `model` ·
`serial_number` · `asset_tag` · `chassis_type` / `chassis_code` · `device_type`

### Système
`os_name` (avec édition) · `os_version` · `os_build` · `architecture` ·
`os_install_date` · `registered_owner` · `registered_organization` · `timezone` ·
`domain` / `workgroup` · `logged_on_user` · `uptime_hours` · `hypervisor_present` ·
`bios_version` · `bios_manufacturer` · `bios_release_date`

### Carte mère & processeur
`motherboard{manufacturer, model, version, serial_number}` · `cpu` ·
`cpu_sockets` · `cpu_physical_cores` · `cpu_logical_cores` · `cpu_max_clock_mhz` ·
`cpu_l2_cache_kb` · `cpu_l3_cache_kb` · `cpu_socket` · `cpu_virtualization` ·
`cpu_address_width`

### Mémoire
`ram_gb` · `ram_free_gb` · `memory_slots_total` / `_used` / `_free` ·
`memory_max_gb` ·
`memory_modules[]{slot, bank, capacity_gb, type, form_factor, speed_mhz,
rated_speed_mhz, manufacturer, part_number, serial_number}`

> `speed_mhz` est la fréquence **réelle** (`ConfiguredClockSpeed`), pas la
> fréquence nominale de la barrette — une DDR4-3600 bridée à 2400 apparaît à 2400.

### Stockage
`disk_total_gb` / `_used_gb` / `_free_gb` · `disk_drives[]` (volumes logiques) ·
`physical_disks[]` (type SSD/HDD + santé SMART) ·
`disk_reliability[]{name, serial_number, power_on_hours, wear_percent,
temperature_c, read_errors, write_errors}`

### Graphique & affichage
`gpu` · `gpu_details[]{name, vram_gb, driver_version, driver_date, resolution}` ·
`monitors[]{manufacturer, model, serial_number, year}`

> La VRAM est lue dans le registre (`qwMemorySize`) et non via
> `Win32_VideoController.AdapterRAM`, qui est un int32 signé et déborde au-delà de 4 Go.

### Impression
`printers[]{name, driver, port, network, default, shared, virtual}`

### Batterie
`battery` · `battery_charge_percent` · `battery_health_percent` ·
`battery_wear_percent` · `battery_designed_capacity_mwh` ·
`battery_full_capacity_mwh` · `battery_cycles` · `battery_health_status` (macOS)

> L'usure réelle vient des classes `root\wmi` du pilote ACPI :
> `Win32_Battery.DesignCapacity` est presque toujours vide.

### Réseau
`network_adapters[]` · `network_adapter_details[]{name, description, link_speed,
mac_address}` · `listening_ports[]{port, process}`

### Sécurité
`antivirus` · `firewall[]` · `bitlocker[]` (FileVault sur macOS) ·
`tpm_present` · `tpm_enabled` · `secure_boot`

### Licences & mises à jour
`licenses[]{name, description, partial_key, status, activated, channel}` ·
`windows_activated` · `windows_license_channel` · `oem_product_key` ·
`hotfixes[]{id, description, installed_on}` · `last_windows_update`

### Comptes & logiciels
`users[]` (statut + appartenance au groupe Administrateurs) ·
`installed_software[]{name, version, publisher, install_date}`

### Métadonnées
`collector_version` · `timestamp` · `elevated` · `platform`

---

## 🔐 Privilèges

`elevated` indique si le collecteur a tourné en administrateur. **Sans élévation**,
les sources suivantes sont inaccessibles et absentes du rapport :

- SMART détaillé (`disk_reliability` — heures, usure, température)
- TPM (`Get-Tpm`)
- BitLocker (`Get-BitLockerVolume`)
- Clé OEM firmware (`OA3xOriginalProductKey`)
- Barrettes mémoire sur Linux (`dmidecode`)

Le rapport PDF, le résumé console et la fiche système affichent tous un
avertissement explicite dans ce cas : un champ vide ne doit pas être confondu
avec un champ inaccessible.

---

## 🖥️ Couverture par système

| Donnée | Windows | macOS | Linux |
|---|:---:|:---:|:---:|
| Identification, OS, CPU, RAM, disques | ✅ | ✅ | ✅ |
| Carte mère / châssis | ✅ | — | ✅ (`/sys/class/dmi`) |
| Barrettes mémoire par slot | ✅ | ✅ | ⚠️ root requis |
| Écrans | ✅ (EDID) | ✅ | ⚠️ nom du connecteur seul |
| Imprimantes | ✅ | — | ✅ (CUPS) |
| Usure disque / SMART détaillé | ✅ | — | ⚠️ type seul (`lsblk`) |
| Usure batterie | ✅ | ✅ | — |
| Licences / activation | ✅ | — | — |
| Correctifs | ✅ | — | — |
| Chiffrement | ✅ BitLocker | ✅ FileVault | — |
| Pare-feu | ✅ | ✅ | — |
| TPM / Secure Boot | ✅ | — | — |
| Ports en écoute | ✅ | — | ✅ (`ss`) |
| Comptes locaux | ✅ | ✅ | ✅ |
| Logiciels (nom+version+éditeur) | ✅ | ⚠️ nom seul | ✅ dpkg/rpm/pacman |

---

## ⚙️ Performance

La collecte Windows tient en **8 appels PowerShell groupés** (~15-20 s au total)
plutôt qu'un appel par donnée. Chaque bloc est protégé par son propre `try/catch`
côté PowerShell **et** côté Python : une source indisponible (module absent,
privilège manquant, édition Windows différente) ne fait jamais échouer les autres.

Point d'attention : `SoftwareLicensingProduct` doit impérativement être interrogé
avec un filtre WQL (`PartialProductKey IS NOT NULL`). Sans filtre, l'énumération
parcourt plusieurs centaines d'entrées et dépasse 30 s à elle seule.

---

## 🚫 Champs volontairement non collectés

| Champ BD | Raison |
|---|---|
| `utilisateur`, `service`, `localisation` | Données métier, non détectables |
| `date_achat`, `prix_achat`, `fournisseur`, `numero_commande` | Historique d'achat |
| `duree_garantie`, `date_fin_garantie` | Métadonnées contractuelles |
| `user_password`, `admin_password`, `anydesk_password` | **Sécurité — jamais collecté** |

---

## 🚀 Utilisation

### CLI
```bash
# Collecter + envoyer + générer le rapport + joindre le PDF
python system-info-collector.py --server http://parcinfo.local:3456 --client-id 1

# Collecter et générer le rapport sans rien envoyer
python system-info-collector.py --no-send

# Mode silencieux
python system-info-collector.py --quiet --client-id 1
```

### GUI
```bash
python system-info-collector-gui.py --server http://parcinfo.local:3456
```
Sélection du client → aperçu des données → envoi → périphériques et rapport créés.

> Lancer de préférence en administrateur (voir section Privilèges).

---

## ➕ Ajouter un champ

1. Collecter dans `collector_core.py` (fonction `_win_*` / `get_system_info_mac` /
   `get_system_info_linux`) — un seul endroit, les deux collecteurs en héritent
2. L'afficher dans `build_summary_lines()` (console + aperçu GUI)
3. L'ajouter au rapport PDF dans `generate_pdf_report()`
4. Si le champ mérite une colonne dédiée : l'ajouter à `get_api_payload()`,
   à la migration dans `init_db()` et à `/api/device-info`
   — sinon il part déjà dans `system_report` et s'affiche via `fiche_systeme.html`

---

**Dernière mise à jour** : 2026-08-08
