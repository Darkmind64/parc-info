# Collecteur Système — Correspondance des Champs

## 📋 Vue d'ensemble

Ce document détaille ce que collecte le collecteur système ParcInfo et où chaque
donnée atterrit côté serveur.

**Généré par :** `collector_core.py` (logique partagée)
→ `system-info-collector.py` (CLI) et `system-info-collector-gui.py` (GUI)

**Version collecteur :** 3.0 · **Version ParcInfo :** 2.9.7+

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

### 2. Champs déduits (depuis 2.9.7) — `app.py:champs_deduits_du_collecteur()`

Ces colonnes existent sur la fiche appareil depuis plus longtemps qu'elles ne
sont déduites automatiquement ; jusqu'à la 2.9.7 elles se saisissaient à la
main. La déduction ne remplit **jamais** une case déjà renseignée par un
technicien — `poser(colonne, valeur)` vérifie l'existant avant d'écrire.

| Champ collecté | Colonne(s) BD | Notes |
|---|---|---|
| `edr_agents[0]` | `edr_marque` / `edr_nom` | Premier agent EDR détecté (voir plus bas) |
| `remote_support_agents[0]` | `rmm_marque` / `rmm_nom` | Idem pour RMM/télémaintenance |
| `anydesk_id` | `anydesk_id` | Lu directement sur le poste, pas ressaisi |
| `logged_on_user` | `utilisateur` | Domaine retiré (`MONDOMAINE\Éric` → `Éric`) |
| `antivirus_products[0]` / `antivirus` | `av_marque` / `av_nom` | Rapproché des listes curées (`marques_antivirus`, `noms_antivirus`) |
| logiciels du client déjà installés | `logiciels` | Comparé à la liste de logiciels métier du client |

Les fiches déjà collectées avant l'ajout d'une déduction sont rattrapées une
fois au démarrage par `completer_fiches_existantes()`, sans nouvelle collecte
— seulement quand `_CLE_RATTRAPAGE_FICHES` change de version.

### 3. Table `peripheriques` (création automatique)

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

### 4. Page « Fiche système »

`/appareil/<id>/fiche-systeme` restitue l'intégralité de `rapport_systeme_json`,
regroupée par thème depuis la 2.9.6 (le rapport PDF suit le même ordre) :

1. Identification & système → processeur/carte mère → mémoire → stockage →
   affichage/impression → batterie
2. Sécurité & conformité → accès distant & exposition → agents de
   télémaintenance & EDR → journal de sécurité → certificats → licences
3. Périphériques USB → périphériques en erreur
4. Réseau → configuration réseau → partages & lecteurs réseau
5. Environnement & hygiène système → comptes de messagerie → applications
   par défaut
6. Comptes utilisateurs → profils utilisateurs
7. Incidents système → mises à jour disponibles → correctifs installés
8. Démarrage & services → tâches planifiées
9. Logiciels installés (avec filtre de recherche)

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
temperature_c, read_errors, write_errors}` ·
`disk_partition_styles[]{number, style, boot}` (GPT/MBR) · `boot_disk_style` ·
`boot_mode` (« UEFI » / « Legacy (BIOS) »)

> `boot_mode` se lit via `Get-ComputerInfo -Property BiosFirmwareType`, sans
> élévation — `bcdedit /enum`, essayé en premier, s'est révélé exiger les
> droits administrateur même en lecture (constaté, pas supposé).

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

### Périphériques USB
`usb_devices[]{name, categorie, manufacturer, model, vid, pid, serial,
inventoriable}` — les contrôleurs/concentrateurs internes sont listés mais
non repris dans l'inventaire matériel (`inventoriable=false`)

### Réseau
`network_adapters[]` · `network_adapter_details[]{name, description, link_speed,
mac_address, physical, ip_addresses[]}` · `listening_ports[]{port, process}` ·
`default_gateway` · `gateways[]{address, interface}` · `dns_suffixes[]` ·
`dns_servers[]{interface, servers[], dhcp}` · `network_profiles[]{name,
interface, category, connectivity}` · `proxy{server, auto_config_url,
enabled}` · `wifi{ssid, signal, band, channel}` · `latency[]{role, target,
avg_ms, max_ms, loss_pct}` · `bandwidth{mbps, downloaded_mb, seconds}`
(sur demande explicite, `--test-debit`) ·
`smb_shares[]{name, path, administrative}` (partages exposés) ·
`mapped_drives[]{letter, path}` (lecteurs mappés depuis d'autres machines)

### Sécurité & conformité
`antivirus` · `antivirus_products[]{name, enabled, status, up_to_date}` ·
`firewall[]` / `firewall_profiles[]{name, enabled}` ·
`bitlocker[]` / `bitlocker_volumes[]{volume, etat, protection, protege}`
(FileVault sur macOS) · `tpm_present` · `tpm_enabled` · `secure_boot` ·
`local_password_policy{min_length, complexity, history, max_age_days,
lockout_threshold, lockout_duration_min}` · `security_events[]{compte, type,
event_id, count, sources[], last_seen}` (échecs d'ouverture de session,
verrouillages) · `failed_logons` · `account_lockouts` ·
`certificates_expiring[]{sujet, emetteur, expire_le, jours_restants, expire}`

> `local_password_policy` se lit via `secedit /export`, dont les clés
> restent en anglais quelle que soit la langue de Windows — `net accounts`,
> localisé, n'aurait pas été fiable sur un Windows francophone.

### Accès distant & exposition
`remote_access[]{key, label, enabled, secure, detail, level}` (RDP, WinRM,
OpenSSH, Telnet client/serveur, Assistance à distance, Registre distant) ·
`rdp_enabled` · `rdp_nla` · `autologon{enabled, user, password_stored}`
(jamais le mot de passe) · `rdp_allowed_users[]` (membres du groupe Bureau à
distance, résolu par SID `S-1-5-32-555` — indépendant de la langue) ·
`saved_rdp_credentials[]` (cibles `TERMSRV/…` du Gestionnaire d'identifiants,
jamais le secret) · `rdp_logon_history[]{user, ip, when}` (connexions
entrantes réussies récentes)

### Agents de télémaintenance & EDR
`remote_support_agents[]{marque, nom, service, actif}` (AnyDesk, TeamViewer,
ScreenConnect, NinjaRMM, Datto RMM, N-able, Atera, Kaseya, Syncro,
Pulseway…) · `edr_agents[]{marque, nom, service, actif}` (CrowdStrike,
SentinelOne, Cortex XDR, Carbon Black, Cybereason, Sophos Intercept X,
Defender for Endpoint…) · `anydesk_id`

> Ces agents sont recherchés par **sous-chaîne du nom affiché** des
> services Windows (`Get-CimInstance Win32_Service`) — au mieux, jamais une
> preuve. L'ID AnyDesk, lui, vient directement de `anydesk.exe --get-id`,
> un indicateur documenté par l'éditeur.

### Comptes de messagerie
`mail_accounts[]{client, email, display_name, protocol, incoming_server,
incoming_port, outgoing_server, outgoing_port, password_stored, profile}`
(Outlook classique, Thunderbird) · `mail_new_outlook{installed, accounts[],
note}` (comptes non énumérables de façon fiable, présence seule détectée)

> Les mots de passe ne sont **jamais** collectés — ni celui d'Outlook (DPAPI)
> ni celui de Thunderbird (NSS), pourtant techniquement déchiffrables sous le
> compte de l'utilisateur. Seule leur présence (`password_stored`) est notée.

### Applications par défaut
`default_browser` · `default_mail` · `installed_browsers[]{name, version}`

### Maintenance & hygiène
`power_plan` · `fast_startup` · `defender_last_full_scan` /
`defender_last_quick_scan` · `dotnet_versions[]` (Framework 3.5/4.x et
Core/5+, ce dernier via `dotnet --list-runtimes`) · `uac_enabled` ·
`restore_points[]{description, when}` · `temp_files_mb` · `reboot_pending` ·
`reboot_reasons[]` · `domain_joined` · `domain_name` · `domain_controller` ·
`wsus_server` / `wsus_group` · `time_source` / `time_offset`

### Diagnostic
`system_incidents[]{category, count, last_seen, disk, message, level}`
(arrêts inattendus, écrans bleus, erreurs disque) · `unexpected_shutdowns` ·
`problem_devices[]{name, classe, fabricant, code, libelle}` (Gestionnaire de
périphériques) · `stopped_auto_services[]{name, display_name, state}` ·
`startup_programs[]{name, command, location, user}` ·
`scheduled_tasks[]{name, path, state, last_run, failed, action, author}`
(hors dossier `\Microsoft\`) · `boot_times[]{when, secondes, noyau_s,
bureau_s}` · `boot_last_seconds` / `boot_average_seconds` ·
`user_profiles[]{nom, chemin, taille_go, derniere_utilisation,
mesure_complete}`

### Licences & mises à jour
`licenses[]{name, description, partial_key, status, activated, channel}` ·
`windows_activated` · `windows_license_channel` · `oem_product_key` ·
`hotfixes[]{id, description, installed_on}` · `last_windows_update` ·
`pending_updates[]{title, kb, size_mb, security, severity}` ·
`pending_updates_security` · `pending_updates_source`

### Comptes & logiciels
`users[]` (statut + appartenance au groupe Administrateurs) ·
`users_details[]{name, status, enabled, admin, role, account_type,
description, password_never_expires, last_logon}` ·
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
- Politique de mot de passe local (`secedit /export`)
- Journal de sécurité (`security_events`, `rdp_logon_history` — le journal
  « Security » lui-même exige l'élévation, contrairement aux autres journaux)
- Clés de récupération BitLocker

`boot_mode` (UEFI/Legacy) est la seule donnée qui a changé de statut : lue
d'abord via `bcdedit /enum`, qui s'est révélé exiger l'élévation même en
lecture, elle est depuis la 2.9.7 accessible sans droits administrateur via
`Get-ComputerInfo`.

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
| Périphériques USB | ✅ | — | — |
| Accès distant (RDP/WinRM/SSH/Telnet…) | ✅ | — | — |
| Agents de télémaintenance & EDR | ✅ | — | — |
| Comptes de messagerie (Outlook, Thunderbird) | ✅ | — | — |
| Politique de mot de passe local | ✅ (admin) | — | — |
| Plan d'alimentation / démarrage rapide | ✅ | — | — |
| Versions .NET installées | ✅ | — | — |
| Style de partition / mode de démarrage | ✅ | — | — |
| Diagnostic (incidents, tâches, profils…) | ✅ | — | — |

---

## ⚙️ Performance

La collecte Windows tient en **26 étapes groupées** (`_WIN_STEPS` dans
`collector_core.py`, une poignée d'appels PowerShell chacune plutôt qu'un
appel par donnée). Chaque bloc est protégé par son propre `try/catch` côté
PowerShell **et** côté Python : une source indisponible (module absent,
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
| Mot de passe des comptes mail (Outlook/Thunderbird) | **Sécurité — techniquement déchiffrable (DPAPI/NSS), délibérément pas collecté** ; seule la présence (`password_stored`) l'est |
| Valeur des identifiants Bureau à distance enregistrés | **Sécurité** — seules les cibles `TERMSRV/…` sont listées, jamais le secret |
| `rmm_agent_id` | Pas de méthode fiable et générique tous éditeurs confondus ; seuls `rmm_marque`/`rmm_nom` sont déduits |

Note : `anydesk_id` **est** collecté depuis la 2.9.7 (ce n'est pas un secret,
c'est l'identifiant public affiché dans AnyDesk) — seul `anydesk_password`
reste hors de portée.

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

Depuis la 2.9.6, la fiche système et le rapport PDF sont regroupés **par
thème** (voir la structure au §4) plutôt que dans l'ordre d'ajout — un champ
doit rejoindre la rubrique existante qui lui correspond, pas être ajouté en
bas de page. Un `test_parite_rapports.py` fait échouer la CI si un champ
n'est rendu que d'un seul côté (fiche ou PDF).

1. Collecter dans `collector_core.py` (fonction `_win_*` / `get_system_info_mac` /
   `get_system_info_linux`), l'enregistrer dans `_WIN_STEPS` si c'est une
   nouvelle étape Windows — un seul endroit, les deux collecteurs en héritent
2. L'ajouter à la rubrique thématique correspondante dans
   `build_summary_sections()` (console + aperçu GUI ; `build_summary_lines()`
   en dérive automatiquement)
3. L'ajouter au rapport PDF dans `generate_pdf_report()`, dans le bloc de la
   **même rubrique** que dans `fiche_systeme.html`
4. L'ajouter à `templates/fiche_systeme.html`, dans la section thématique
   correspondante (Sécurité & accès / Réseau / Environnement & hygiène /
   Comptes & activité / Matériel)
5. Si le champ mérite une colonne dédiée : l'ajouter à `get_api_payload()`,
   à la migration dans `init_db()` et à `/api/device-info`
   — sinon il part déjà dans `system_report` et s'affiche via `fiche_systeme.html`
6. Si le champ doit **préremplir un champ existant de la fiche appareil**
   (comme `av_nom`/`edr_nom`/`rmm_nom`/`anydesk_id`) : l'ajouter à
   `champs_deduits_du_collecteur()` dans `app.py`, et bumper
   `_CLE_RATTRAPAGE_FICHES` pour que les collectes déjà en base en profitent
   sans nouvelle collecte

---

**Dernière mise à jour** : 2026-08-12 (v2.9.7)
