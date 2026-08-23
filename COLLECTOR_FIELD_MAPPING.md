# Collecteur Système — Correspondance des Champs

## 📋 Vue d'ensemble

Ce document détaille ce que collecte le collecteur système ParcInfo et où chaque
donnée atterrit côté serveur.

**Généré par :** `collector_core.py` (logique partagée)
→ `system-info-collector.py` (CLI) et `system-info-collector-gui.py` (GUI)

**Version collecteur :** 3.15 · **À jour avec ParcInfo :** 2.18.37

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
| `installed_software` | `logiciels_installes_json` | Liste complète, plafond 2000 entrées. Chaque entrée porte aussi `update_status` (`obsolete`/`a_jour`/`inconnu`) et `latest_version` — voir § Mises à jour logicielles |
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

> `ip_addresses` (`get_ip_addresses()`, corrigé en 3.9) : `socket.gethostbyname_ex(hostname)`
> est le moyen habituel, mais peu fiable sur macOS — le hostname y est
> souvent un nom mDNS en `.local` (Bonjour) que cette résolution classique
> ne fait pas toujours aboutir, la liste ressortant alors vide **sans lever
> d'exception** (silencieux : ce n'est pas un cas que `try/except` peut
> détecter). Repli sur un socket UDP « connecté » à une adresse externe
> (aucun paquet réellement envoyé — `connect()` sur UDP ne fait
> qu'interroger la table de routage) : donne l'IP de l'interface que le
> système utiliserait réellement pour sortir, fiable sur les trois OS.

> `dns_name` (`get_fqdn()`, corrigé en 3.15) : `socket.getfqdn()` est
> notoirement peu fiable sur macOS — il déclenche
> `gethostbyaddr(gethostname())`, et quand cette résolution retombe sur le
> bouclage IPv6 (`::1`) sans enregistrement PTR configuré, certains
> résolveurs renvoient tel quel le nom de la requête plutôt qu'une erreur :
> constaté en usage réel, `dns_name` valait
> `1.0.0.0.[…]0.0.ip6.arpa` (la représentation PTR standard de `::1`) au
> lieu du vrai nom de la machine. Remplacé sur macOS par `scutil --get
> LocalHostName` (+ suffixe `.local`) : lit directement le nom Bonjour
> configuré localement, sans passer par une résolution réseau — rien à
> confondre avec une adresse de bouclage. Repli sur `hostname` (déjà
> qualifié `.local` sur macOS) si `scutil` échoue.

### Système
`os_name` (avec édition) · `os_version` · `os_build` · `architecture` ·
`os_install_date` · `registered_owner` · `registered_organization` · `timezone` ·
`domain` / `workgroup` · `logged_on_user` · `uptime_hours` · `hypervisor_present` ·
`bios_version` · `bios_manufacturer` · `bios_release_date`

> macOS (depuis 3.8) : `os_version`/`os_build` viennent de `sw_vers`, pas de
> `platform.mac_ver()` — ce dernier a régulièrement traîné derrière une
> nouvelle version majeure macOS (Big Sur longtemps rapporté « 10.16 » par
> certaines versions de Python), alors que `sw_vers` vient directement du
> système. `os_name` porte en plus le nom marketing (« macOS Sequoia ») via
> une table de correspondance tenue à la main (`_MACOS_CODENAMES`,
> `collector_core.py`) — une version plus récente que la dernière mise à
> jour de cette table retombe simplement sur « macOS » sans nom, jamais une
> erreur. `bios_version`/`bios_manufacturer` (depuis 3.8) viennent du
> « Boot ROM Version »/« System Firmware Version » de `SPHardwareDataType`
> (le libellé a changé selon les générations d'Intel Mac).

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
`disk_layout[]{number, model, bus, media_type, health, op_status, size_gb,
partitions[]{letter, type, total, used, free, pct}}` (depuis 3.1, un disque
physique avec TOUTES ses partitions imbriquées, y compris celles sans lettre
de lecteur — voir note ci-dessous) ·
`disk_reliability[]{name, serial_number, power_on_hours, wear_percent,
temperature_c, read_errors, write_errors}` ·
`disk_partition_styles[]{number, style, boot}` (GPT/MBR) · `boot_disk_style` ·
`boot_mode` (« UEFI » / « Legacy (BIOS) »)

> `boot_mode` se lit via `Get-ComputerInfo -Property BiosFirmwareType`, sans
> élévation — `bcdedit /enum`, essayé en premier, s'est révélé exiger les
> droits administrateur même en lecture (constaté, pas supposé).

> **Bug critique corrigé en 3.12 : `_unix_disks()` (macOS/Linux) ne
> remontait AUCUN disque sur macOS.** Le parseur de taille (`to_gb()`) ne
> reconnaissait que le format GNU (Linux, « 494G ») — `df -h` BSD (macOS)
> suffixe les unités binaires d'un « i » (« 494Gi », « 11Mi » — même base
> 1024, juste un suffixe différent), qu'aucune des trois vérifications
> `endswith('T'/'G'/'M')` ne matchait jamais. Résultat : `size_gb` valait
> systématiquement `None` sur macOS, chaque ligne était silencieusement
> filtrée (`continue`), et la section stockage entière ressortait vide —
> sans qu'aucune exception ne le signale. Remplacé par une expression
> régulière tolérant les deux formes (`Gi`/`G`, `Mi`/`M`, `Ti`/`T`, `Ki`/`K`,
> avec ou sans `B` final).
>
> Effet de bord corrigé dans la foulée : `disk_total_gb`/`disk_free_gb`
> sommaient bêtement TOUTES les lignes `df`. Sur macOS/APFS, plusieurs
> volumes d'un même conteneur (Système, Données, VM, Preboot, Update,
> xarts, iSCPreboot, Hardware…) rapportent TOUS la même capacité totale et
> le même espace libre partagé — un Mac avec ~500 Go de disque physique et
> 8 volumes dans son conteneur affichait ~2,5 To de « stockage total ».
> Corrigé : `size_gb`/`free_gb` sont désormais dédupliqués par couple de
> valeurs identiques au sein d'un même disque physique (`disk_layout`)
> avant sommation — `used_gb`, propre à chaque volume, reste sommé sans
> déduplication. Sans effet sur Linux, où chaque partition a déjà sa
> propre taille distincte.
>
> Signalé après coup (toujours en usage réel) : même dédupliqués, les
> volumes de service macOS/APFS (Preboot, VM, Update, xarts, iSCPreboot,
> Hardware, Recovery — présents sur tout Mac moderne) restaient tous
> listés dans `disk_drives`/`disk_layout`, donnant l'illusion visuelle de
> 6 à 8 « disques » quasi identiques là où il n'y en a physiquement qu'un.
> Corrigé en 3.15 : ces points de montage (`_MACOS_VOLUMES_INTERNES`) sont
> désormais exclus de l'affichage ET du total — seuls `/` (Système) et
> `/System/Volumes/Data` restent, les deux volumes où vit réellement le
> contenu de la machine.

> `disk_layout` alimente la vue « un disque, ses partitions dedans » de la
> fiche système (remplace l'ancienne carte à plat qui mélangeait les volumes
> de tous les disques). Sur Windows, `Get-Disk`/`Get-Partition`/`Get-Volume`
> donnent la relation disque ↔ lettre de lecteur ; `physical_disks[]` est
> dérivé de la même requête, pas d'un second appel. Sur macOS/Linux, il n'y a
> pas d'équivalent WMI : le regroupement se fait par nom d'appareil
> (`/dev/sda1` → `sda`, `/dev/nvme0n1p1` → `nvme0n1`, `/dev/disk3s1` → `disk3`
> côté macOS) — `model`/`media_type` restent vides sauf sur Linux, où `lsblk`
> les fournit. Une fiche collectée avant la 3.1 n'a pas ce champ : la fiche
> système retombe alors sur l'ancienne vue à plat (`_fiche_systeme_disques`
> dans `app.py`).
>
> `partitions[].type` vient de `Get-Partition.Type` (Windows uniquement —
> `df` sur macOS/Linux ne voit que les systèmes de fichiers montés, donc pas
> les partitions système sans lettre) : `Basic`/`IFS` (données), `System`
> (EFI), `Reserved` (MSR), `Recovery`. Traduit à l'affichage par
> `_TYPE_PARTITION` (collector_core.py) puis colorié par
> `_COULEUR_PARTITION` (app.py) — un code couleur fixe par type, identique
> sur tous les disques de la fiche, distinct du rouge/orange/vert de
> remplissage (qui reste porté par le pourcentage affiché sur le segment
> « Données », pas par sa couleur).

### Graphique & affichage
`gpu` · `gpu_details[]{name, vram_gb, driver_version, driver_date, resolution}` ·
`monitors[]{manufacturer, model, serial_number, year}`

> La VRAM est lue dans le registre (`qwMemorySize`) et non via
> `Win32_VideoController.AdapterRAM`, qui est un int32 signé et déborde au-delà de 4 Go.

> `gpu`/`gpu_details` (macOS, depuis 3.12) : jusqu'ici totalement absents
> côté macOS — aucune collecte GPU n'avait jamais été implémentée. Viennent
> du même appel `system_profiler SPDisplaysDataType` que les écrans
> ci-dessous (un seul appel pour les deux, pas de requête dupliquée).
> `driver_version`/`driver_date`/`resolution` restent vides : pas
> d'équivalent macOS à ces informations pilote Windows. `vram_gb` : le nom
> de la clé varie selon le type de GPU (intégré/dédié) et la version macOS
> (`spdisplays_vram`, `spdisplays_vram_shared`, `sppci_vram` observés) — les
> trois sont tentées, la première trouvée gagne ; absent si aucune ne l'est,
> plutôt qu'une valeur inventée.

> `monitors[].manufacturer` (macOS, depuis 3.10) : deviné depuis le premier
> mot du nom EDID (`_name` de `SPDisplaysDataType`, souvent « DELL
> U2720Q ») contre une petite liste de marques connues
> (`_deviner_marque_ecran()`) — **jamais** depuis l'identifiant vendeur
> EDID brut, dont l'encodage exact (chaîne déjà résolue ou identifiant à
> décoder, selon la version macOS) était trop incertain à deviner sans
> matériel réel pour vérifier ; une marque erronée aurait été pire qu'un
> champ vide. Écran interne : toujours « Apple ». `year` vient de
> `_spdisplays_display-year` quand l'EDID de l'écran externe l'expose (pas
> tous ne le font). Pas de `diagonal_inch` côté macOS : contrairement à
> Windows (`WmiMonitorBasicDisplayParams`, dimensions physiques en cm),
> `system_profiler` n'expose pas la taille physique de l'écran de façon
> fiable dans son JSON.

### Impression
`printers[]{name, driver, port, network, default, shared, virtual}`

> macOS (depuis 3.5) : `lpstat -p`/`lpstat -v` (CUPS) donnent nom, URI du
> périphérique et imprimante par défaut ; `driver`/`shared` restent vides —
> CUPS ne les expose pas de façon aussi directe que `Win32_Printer`.
> `network`/`virtual`/`connection` réutilisent les mêmes helpers que
> Windows (`printer_connection()`, `is_virtual_printer()`), le préfixe
> `usb://` d'une URI CUPS matchant déjà leurs règles existantes.

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
`default_gateway` · `public_ip` · `public_ip_isp` · `gateways[]{address, interface}` · `dns_suffixes[]` ·
`dns_servers[]{interface, servers[], dhcp}` · `network_profiles[]{name,
interface, category, connectivity}` · `proxy{server, auto_config_url,
enabled}` · `wifi{ssid, signal, band, channel}` · `latency[]{role, target,
avg_ms, max_ms, loss_pct}` · `bandwidth{mbps, downloaded_mb, seconds}`
(sur demande explicite, `--test-debit`) ·
`smb_shares[]{name, path, administrative}` (partages exposés) ·
`mapped_drives[]{letter, path}` (lecteurs mappés depuis d'autres machines) ·
`hosts_entries[]{ip, hostname, local}` ·
`port_forwards[]{listen_address, listen_port, connect_address, connect_port}` ·
`dns_check_resolveur`, `dns_check_reponse` (sur demande explicite,
`--dns-check`) ·
`router_manufacturer`, `router_model`, `router_name`, `router_wan_ip` (sur
demande explicite, `--router-info`)

> `listening_ports` (macOS, depuis 3.5) vient de `lsof -iTCP -sTCP:LISTEN`,
> pendant du `ss -tlnp` déjà utilisé côté Linux.

> macOS (depuis 3.10, `_mac_network()`) alimente `default_gateway`/
> `gateways` (`route -n get default`), `dns_servers`/`dns_suffixes`
> (`scutil --dns` — tous les blocs `resolver #N` sont repris, pas
> seulement le premier, un VPN/split-DNS pouvant en ajouter dont on ne
> veut pas rater les serveurs) et `proxy` (`scutil --proxy`). Pas
> d'équivalent macOS ajouté pour `network_profiles` (concept propre à
> Windows — catégorie réseau Public/Privé/Domaine) ni `wifi` (réseau Wi-Fi
> actuellement connecté — `system_profiler SPAirPortDataType` existe mais
> son schéma JSON exact n'a pas pu être vérifié sans matériel réel ; risque
> de champ silencieusement vide plutôt qu'une régression, mais mieux valait
> ne rien ajouter que deviner).
>
> `dns_suffixes` : bug de longue date corrigé en 3.10 — côté Windows,
> `_win_network()` stockait déjà une chaîne pré-jointe (`-join ', '`) alors
> que le champ est documenté et rendu partout ailleurs comme une **liste**
> (`build_summary_sections()` faisait `', '.join(dns_suffixes)` dessus, ce
> qui rejoignait la chaîne **caractère par caractère** au lieu de suffixe
> par suffixe — visible uniquement sur un poste ayant réellement une liste
> de suffixes de recherche configurée, donc resté longtemps inaperçu).
> Reconverti en liste réelle côté Windows, symétrique de macOS ; le rendu
> PDF et la fiche système, qui affichaient jusqu'ici la chaîne déjà jointe
> sans y toucher, rejoignent désormais eux aussi une vraie liste.

> `hosts_entries`, `public_ip`, `public_ip_isp` et les champs
> `dns_check_*`/`router_*` sont les seuls champs réseau qui ne sont pas
> spécifiques à Windows — tous appelés depuis `collect_system_info()` plutôt
> que `_WIN_STEPS`/`get_system_info_windows()` (simple lecture de fichier
> pour `hosts_entries` ; simple appel HTTPS, `get_public_ip_info()`, pour
> l'IP publique ; requête DNS brute construite à la main,
> `get_dns_check_info()`, pour la vérification DNS ; découverte UPnP/SSDP,
> `get_router_info()`, pour la box internet — les quatre valables sur les
> trois OS).
>
> `dns_check_reponse` : contenu brut de la réponse TXT à
> `test.dnscheck.tools`, interrogé via le résolveur DNS configuré sur ce
> poste (`dns_check_resolveur`) — pas un résolveur public arbitraire, c'est
> justement ce que verrait un navigateur sur ce même poste. dnscheck.tools
> ne documente pas assez précisément l'interprétation de ses variantes
> (ECS, DNSSEC) pour qu'un verdict OK/KO fabriqué ici soit fiable : la
> réponse brute est affichée telle quelle, à charge du technicien de la lire.
>
> `router_*` : description UPnP (IGD) de la box internet — fabricant,
> modèle, nom, IP WAN via un appel SOAP `GetExternalIPAddress`. Best-effort :
> de nombreuses box grand public désactivent UPnP par défaut, ou ne
> répondent pas à la découverte SSDP ; absence totale de ces champs = box
> injoignable en UPnP, pas une erreur de collecte.
>
> `hosts_entries` filtré : `localhost`, les entrées
> `ip6-*` que Linux inscrit lui-même, et la propre entrée `<ip loopback>
> <hostname de la machine>` que Debian/Ubuntu écrivent automatiquement —
> aucune de ces trois n'est une redirection volontaire. Les doublons exacts
> (même IP, même nom — deux outils qui gèrent la même entrée) sont réduits à
> une seule ligne. `local` distingue une redirection vers une IP
> locale/nulle (blocage publicité/licence/télémétrie, ou serveur de dev)
> d'une simple correspondance nom↔IP réelle sur le réseau local.
>
> `port_forwards` vient de `netsh interface portproxy show all` — un
> troisième mécanisme de redirection silencieuse, distinct des deux
> précédents : le pare-feu décide ce qui peut ENTRER, le fichier hosts
> redirige par NOM, portproxy redirige au niveau du PORT, indépendamment des
> deux autres. Pas de libellés de champs à faire correspondre comme pour
> `netsh advfirewall` (le tableau n'a pas d'export XML mais n'a pas non plus
> de texte à traduire) : toute ligne à exactement 4 jetons dont le 2ᵉ et le
> 4ᵉ sont des nombres est retenue, ce qui élimine l'en-tête et le séparateur
> sans avoir à connaître leur texte exact — fonctionne donc aussi dans une
> langue non prévue. Généralement vide sur un poste ordinaire, aucun filtre
> de volume nécessaire contrairement aux règles de pare-feu.

### Réseaux Wi-Fi enregistrés (hors rapport système)
`get_wifi_profiles()` → `[{ssid, authentification, chiffrement, password?}]` —
tous les profils Wi-Fi enregistrés sur le poste (`netsh wlan export profile`),
pas seulement celui actuellement connecté (`wifi` ci-dessus).

> macOS (depuis 3.8) : `networksetup -listpreferredwirelessnetworks` donne
> le SSID de chaque réseau enregistré, **jamais** `authentification`/
> `chiffrement`/`password` (toujours vides/absents) — ces informations
> vivent dans le Trousseau, dont la lecture (`security
> find-generic-password`) déclenche une invite Touch ID/mot de passe PAR
> RÉSEAU, incompatible avec une collecte automatisée non surveillée.
> `inclure_mdp` n'a donc aucun effet sur macOS.

> **Seul champ collecté qui ne transite jamais par `system_report`.** Tout le
> reste de cette page finit, tel quel, dans `rapport_systeme_json` — visible
> en clair dans la fiche appareil et le PDF. Un mot de passe Wi-Fi ne doit
> avoir qu'une seule destination : la table `identifiants`, chiffrée. C'est
> pourquoi `_win_wifi_profiles()` n'est **pas** dans `_WIN_STEPS` et pourquoi
> `get_wifi_profiles()`/`send_wifi_credentials_to_parcinfo()` sont appelés
> séparément de `collect_system_info()`/`send_to_parcinfo()`, avec leur propre
> endpoint `POST /api/device-info/wifi-credentials`.
>
> Le SSID et le type de sécurité sont **toujours** remontés (comme le reste de
> la collecte) ; le mot de passe en clair ne l'est que sur un geste explicite
> — case décochée par défaut côté GUI, `--wifi-passwords` côté CLI. Sans ce
> geste, `netsh wlan export profile` tourne sans `key=clear` : le mot de passe
> n'est alors même pas présent dans le XML exporté, il n'est jamais lu.
>
> Le dossier d'export (`netsh wlan export profile folder=…`) est temporaire et
> supprimé dès la lecture terminée, y compris en cas d'erreur — il contient
> les mots de passe en clair sur disque tant qu'il existe.
>
> Côté serveur, `app.py:_sync_wifi_credentials_from_collector()` range chaque
> réseau dans `identifiants` (catégorie `Wi-Fi`), chiffré via `crypto_utils`.
> Un SSID déjà connu pour ce client est mis à jour plutôt que dupliqué ; son
> mot de passe existant n'est écrasé que si CE relevé en apporte un nouveau —
> jamais vidé par une collecte où la case n'était pas cochée. `nom` et
> `description` saisis à la main (via le formulaire identifiant) ne sont
> jamais touchés par la synchronisation automatique.

### Sécurité & conformité
`antivirus` · `antivirus_products[]{name, enabled, status, up_to_date}` ·
`firewall[]` / `firewall_profiles[]{name, enabled}` ·
`bitlocker[]` / `bitlocker_volumes[]{volume, etat, protection, protege}`
(FileVault sur macOS) · `tpm_present` · `tpm_enabled` · `secure_boot` ·
`sip_status` · `gatekeeper_status` · `mdm_enrolled` · `mdm_detail` (macOS,
depuis 3.8) ·
`local_password_policy{min_length, complexity, history, max_age_days,
lockout_threshold, lockout_duration_min}` · `security_events[]{compte, type,
event_id, count, sources[], last_seen}` (échecs d'ouverture de session,
verrouillages) · `failed_logons` · `account_lockouts` ·
`certificates_expiring[]{sujet, emetteur, expire_le, jours_restants, expire}` ·
`malware_detections[]{threat, category, level, process, resource, when,
cleaned}` · `malware_detections_total` ·
`unsigned_drivers[]{device, version, provider}` ·
`firewall_rules[]{name, protocol, port, profiles}` · `firewall_rules_total`

> `sip_status`/`gatekeeper_status` (macOS, depuis 3.8) viennent de `csrutil
> status` (Protection de l'intégrité système) et `spctl --status`
> (Gatekeeper) — pas des équivalents stricts de TPM/Secure Boot, mais la
> même famille de question : un technicien qui désactive SIP pour un kext
> tiers, ou Gatekeeper pour exécuter du logiciel non notarié, laisse ici une
> trace plutôt que de devoir le redécouvrir en console. `mdm_enrolled`/
> `mdm_detail` viennent de `profiles status -type enrollment` — à croiser
> avec `remote_support_agents` (§ Agents de télémaintenance & EDR) : un
> agent Jamf/Kandji/Mosyle détecté sans inscription MDM correspondante, ou
> l'inverse, vaut la peine d'être remarqué sur un parc censé être
> entièrement géré.

> `local_password_policy` se lit via `secedit /export`, dont les clés
> restent en anglais quelle que soit la langue de Windows — `net accounts`,
> localisé, n'aurait pas été fiable sur un Windows francophone.

> `malware_detections` vient de `Get-MpThreatDetection` (Windows Defender),
> joint côté client à `Get-MpThreat` sur `ThreatID` pour retrouver le nom de
> la menace. `category`/`level` dérivent du **préfixe** de `ThreatName`
> (`Trojan:…`, `PUA:…`, `Ransom:…`) plutôt que du `CategoryID` numérique —
> seules quelques valeurs de cet enum sont documentées de façon fiable, alors
> que le préfixe du nom est une convention Microsoft stable. Fenêtre de 365
> jours (les détections sont rares, contrairement aux 30 jours des autres
> historiques) ; `resource` est nettoyé du jeton de session
> (`file:_`/`webfile:_…|…`) via `_chemin_ressource_defender()`.
>
> `unsigned_drivers` s'appuie sur `IsSigned` de `Win32_PNPSignedDriver`, pas
> sur `DriverDate` : de nombreux pilotes Windows intégrés portent une date
> ancienne héritée de leur toute première publication sans que ce soit un
> signal de problème — un faux positif systématique sur la moitié du parc.
> L'absence de signature, elle, est un fait vérifiable sans ambiguïté.
>
> `firewall_rules` vient de `netsh advfirewall firewall show rule name=all
> dir=in` (`Get-NetFirewallRule` exige l'élévation, contrairement à
> `netsh`) — décodé via la page de code OEM active (`GetOEMCP()`), jamais en
> UTF-8 : ces outils console héritées de cmd.exe n'écrivent jamais en UTF-8,
> et décoder autrement corrompt silencieusement tout libellé accentué (voir
> `_win_console_output()`). Pas d'export XML disponible pour cette commande
> (contrairement à `netsh wlan` ou `gpresult`), d'où un parsing bilingue
> FR/EN par libellé — certains restent d'ailleurs en anglais même sur un
> Windows français (`LocalPort`, `Profiles`), preuve que ce n'est pas
> uniforme.
>
> Un poste compte facilement plus de 1500 règles : les centaines propres à
> chaque fonctionnalité Windows (Découverte réseau, Partage d'imprimantes…),
> déjà résumées via `firewall_profiles`, et les noms générés en masse par
> Docker/Hyper-V (`HNS Container Networking - <GUID> - 0`, régénérés à
> chaque réseau de conteneur créé) sont délibérément écartés. Ce qui reste —
> actif, autorisé, sans groupe Windows, sans nom généré — ce sont les trous
> ouverts par des logiciels installés, fusionnés par nom (`firewall_rules_total`
> garde le compte avant la limite d'affichage). Entrant seulement.

### Accès distant & exposition
`remote_access[]{key, label, enabled, secure, detail, level}` (RDP, WinRM,
OpenSSH, Telnet client/serveur, Assistance à distance, Registre distant) ·
`rdp_enabled` · `rdp_nla` · `autologon{enabled, user, password_stored}`
(jamais le mot de passe) · `rdp_allowed_users[]` (membres du groupe Bureau à
distance, résolu par SID `S-1-5-32-555` — indépendant de la langue) ·
`saved_rdp_credentials[]` (cibles `TERMSRV/…` du Gestionnaire d'identifiants,
jamais le secret) · `rdp_logon_history[]{user, ip, when}` (connexions
entrantes réussies récentes)

> macOS (depuis 3.5, `_mac_remote_access()`) alimente le même
> `remote_access[]` avec deux entrées : Connexion à distance/SSH
> (`systemsetup -getremotelogin`) et Partage d'écran/VNC-ARD (`launchctl
> print system/com.apple.screensharing`). Les deux commandes exigent les
> privilèges administrateur pour répondre correctement ; sans élévation,
> le champ reste absent plutôt que faussement « désactivé » — même
> logique que TPM/BitLocker côté Windows non élevé.

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

> macOS (depuis 3.5, `_mac_managed_agents()`) réutilise le même
> `chercher_agents()` (fonction pure, partagée avec Windows) contre deux
> sources combinées : les process en cours (`ps -axo comm=`, fiables pour
> `actif` mais dont le nom de binaire ne porte pas toujours la marque —
> SentinelOne tourne sous `SentinelAgent`, catalogue macOS dédié
> `_AGENTS_EDR_MAC`) et les applications installées (`/Applications/*.app`,
> dont le nom porte généralement la marque mais dont la présence ne prouve
> pas que l'agent tourne — ces entrées sont posées `actif=False`).
> S'y ajoute `_AGENTS_RMM_MAC` (Jamf, Kandji, Addigy, Mosyle — MDM/RMM
> courants en parc Mac professionnel, absents du catalogue Windows).
> L'ID AnyDesk est lu dans `~/Library/Application Support/AnyDesk/system.conf`
> (clé `ad.anynet.id`), faute de commande `--get-id` documentée sur macOS.
> `antivirus` reçoit par défaut `XProtect (intégré macOS)` — toujours
> présent sur macOS moderne — remplacé si un antivirus grand public connu
> (Malwarebytes, Sophos, Norton, Avast, Bitdefender) est détecté parmi les
> mêmes sources.

### Comptes de messagerie
`mail_accounts[]{client, email, display_name, protocol, incoming_server,
incoming_port, outgoing_server, outgoing_port, password_stored, profile}`
(Outlook classique, Thunderbird) · `mail_new_outlook{installed, accounts[],
note}` (comptes non énumérables de façon fiable, présence seule détectée)

> Les mots de passe ne sont **jamais** collectés — ni celui d'Outlook (DPAPI)
> ni celui de Thunderbird (NSS), pourtant techniquement déchiffrables sous le
> compte de l'utilisateur. Seule leur présence (`password_stored`) est notée.

### Applications par défaut
`default_browser` / `default_browser_icon` · `default_mail` /
`default_mail_icon` · `installed_browsers[]{name, version, icon}` ·
`file_type_defaults[]{extension, name, icon}` — programme par défaut pour une
poignée d'extensions courantes (`.pdf`, `.txt`, `.log`, `.jpg`, `.png`,
`.docx`, `.xlsx`, `.csv`, voir `_EXTENSIONS_SUIVIES`).

> `icon` (depuis 3.2, Windows uniquement) est la VRAIE icône de l'application,
> extraite de son exécutable via `ExtractIconEx` (Win32) — PNG 32×32 en
> base64, pas un émoji déduit du nom. La clé de registre `DefaultIcon` de
> chaque ProgId donne le chemin et l'index à extraire ; un index négatif y
> désigne un identifiant de ressource (pas une position), `ExtractIconEx` le
> gère nativement — ne surtout pas le rendre positif (constaté sur Outlook :
> index `-9403`, extraction silencieusement vide si converti en `9403`).
> Toutes ces résolutions passent par `Registry::HKEY_CLASSES_ROOT\...`, pas
> par le lecteur `HKCR:` — celui-ci n'existe pas dans le contexte non
> interactif (`powershell -NoProfile -NonInteractive -Command`) où tourne le
> collecteur (constaté : seuls `HKCU:`/`HKLM:` y sont montés par défaut).
> Cette même correction a aussi réparé la résolution du nom lisible d'une
> association de fichier, qui retombait silencieusement sur le ProgId brut
> (`Acrobat.Document.DC` au lieu de « Document Adobe Acrobat »).
>
> Sans icône extraite (app UWP/AppX — mécanisme de packaging différent,
> sans `DefaultIcon` classique — ou extraction échouée), le filtre Jinja
> `app_icon` (app.py) prend le relais avec une icône par mot-clé déduite du
> nom, comme avant la 3.2. `icon` est vide dans les deux cas ; la fiche
> système choisit entre image réelle et émoji via la macro `app_icone`.

### Maintenance & hygiène
`power_plan` · `fast_startup` · `defender_last_full_scan` /
`defender_last_quick_scan` · `dotnet_versions[]` (Framework 3.5/4.x et
Core/5+, ce dernier via `dotnet --list-runtimes`) · `uac_enabled` ·
`restore_points[]{description, when}` · `temp_files_mb` · `reboot_pending` ·
`reboot_reasons[]` · `domain_joined` · `domain_name` · `domain_controller` ·
`wsus_server` / `wsus_group` · `time_source` / `time_offset` ·
`group_policies[]{name, scope, enabled, denied}`

> macOS (depuis 3.8) : `restore_points` reçoit la dernière sauvegarde Time
> Machine (`tmutil latestbackup` pour la date, `tmutil destinationinfo` pour
> le nom du disque de destination) plutôt qu'un point de restauration
> Windows — même champ générique, même question de fond (« ce poste est-il
> sauvegardé, et depuis quand pas »), pertinente pour la gestion de parc au
> même titre que la garantie ou les contrats. Une seule entrée (la plus
> récente), pas l'historique complet des sauvegardes.

> `group_policies` vient de `gpresult /X` (export XML), pas de `gpresult /r`
> (texte) — même raison que pour les profils Wi-Fi (`_win_wifi_profiles`) :
> le texte change de libellés selon la langue de Windows, le schéma XML est
> fixe. Le périmètre `scope='Utilisateur'` ne demande aucun privilège
> particulier et est donc toujours présent ; `scope='Ordinateur'` n'apparaît
> que si la collecte tourne élevée.

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
mesure_complete}` · `system_errors[]{provider, event_id, message, count,
last_seen}` · `application_errors[]{application, type, module, exception,
path, count, last_seen}` · `shutdown_history[]{when, action, reason, planned,
user}` · `top_processes_cpu[]{name, cpu_pct, ram_mb}` ·
`top_processes_ram[]{name, cpu_pct, ram_mb}`

> `top_processes_cpu`/`top_processes_ram` sont un **instantané**, pas une
> moyenne : `Get-Process` expose un temps CPU cumulé depuis le lancement du
> processus, pas une charge instantanée (un navigateur ouvert depuis trois
> jours dominerait sinon le classement même inactif). Deux relevés espacés
> de ~600 ms et leur delta donnent un vrai pourcentage instantané, normalisé
> par le nombre de cœurs. Le processus PowerShell qui exécute la mesure
> apparaît lui-même dans le classement — donnée honnête, pas un artefact à
> filtrer. Dix processus par liste (`_win_top_processes(limite=10)`, cinq
> avant la 3.2).
>
> macOS (depuis 3.8, `_mac_top_processes()`) : `ps -Ao comm,%cpu,rss -r`
> donne `%cpu` déjà instantané côté noyau (pas besoin du double relevé
> Windows) et `rss` en Ko, converti en Mo — **pas** `%mem`, qui est un
> pourcentage et aurait rendu `ram_mb` silencieusement incohérent d'un
> poste à l'autre (4,2 signifiant 4,2 % sur un Mac contre 4,2 Mo sur un PC).
>
> `system_incidents` (macOS, depuis 3.8, `_mac_crash_diagnostics()`) :
> compte les rapports de plantage applicatifs
> (`~/Library/Logs/DiagnosticReports/*.crash` et `*.ips` — le premier avant
> macOS Monterey, le second depuis, fusionnés en une seule catégorie « 
> Plantage application ») et les paniques noyau
> (`/Library/Logs/DiagnosticReports/*.panic`, catégorie « Panique noyau »,
> niveau `danger` — l'équivalent le plus proche d'un écran bleu Windows).
> Fenêtre de 30 jours par défaut. Comptage par lecture de répertoire (nom +
> date de modification) sans jamais ouvrir le contenu des fichiers : robuste
> aux changements de format interne entre versions macOS, contrairement à
> un parsing de leur contenu.
>
> `startup_programs` (macOS, depuis 3.8, `_mac_startup_items()`) : LaunchAgents
> utilisateur (`~/Library/LaunchAgents`) et système
> (`/Library/LaunchAgents`, `/Library/LaunchDaemons`) — équivalent
> approximatif des tâches planifiées/programmes de démarrage Windows. Lus
> directement via `plistlib` (bibliothèque standard), pas en scrapant
> `launchctl list` : un fichier `.plist` a un format stable, alors que le
> texte de `launchctl list` change de colonnes selon la version macOS. Les
> jobs Apple (`/System/Library/Launch*`) sont exclus — même principe que
> l'exclusion du dossier `\Microsoft\` côté tâches planifiées Windows.
> `system_incidents` intègre désormais le code STOP (bugcheck) précis d'un
> écran bleu quand disponible, extrait du `param1` structuré de l'événement
> 1001 (`Microsoft-Windows-WER-SystemErrorReporting`) plutôt que du texte
> localisé du message — voir `_code_arret_depuis_param1()`. Le code entre
> dans la clé de regroupement : deux écrans bleus de causes différentes ne
> sont pas comptés comme un seul incident répété.

> `system_errors` interroge le journal Système (niveaux Erreur/Critique) et
> **exclut explicitement** les couples (fournisseur, ID) déjà couverts par
> `system_incidents` ci-dessus, pour ne rien signaler deux fois — c'est un
> filet plus large et moins curaté, pas un remplacement.
>
> `application_errors` couvre les IDs 1000 (plantage) et 1002 (ne répond
> plus) du journal Application. Ces deux événements n'ont **pas** le même
> schéma de champs positionnels : lire `module`/`exception`/`path` sur un
> 1002 renvoie un horodatage et un GUID travestis en module/exception — ces
> trois champs restent donc `None` hors des 1000 (`_champs_erreur_application()`,
> couvert par un test de non-régression).
>
> `shutdown_history` lit l'ID 1074 (journal Système), seul événement de ce
> lot dont les champs XML sont **nommés** (`param1`…`param7`) plutôt que
> positionnels — pas de piège équivalent à celui des erreurs applicatives.
> `planned` distingue un arrêt/redémarrage à l'initiative d'un utilisateur ou
> planifié d'un arrêt inattendu.

### Licences & mises à jour
`licenses[]{name, description, partial_key, status, activated, channel}` ·
`windows_activated` · `windows_license_channel` · `oem_product_key` ·
`hotfixes[]{id, description, installed_on}` · `last_windows_update` ·
`pending_updates[]{title, kb, size_mb, security, severity}` ·
`pending_updates_security` · `pending_updates_source`

> macOS (depuis 3.5, `_mac_pending_updates()`) alimente le même
> `pending_updates[]`/`pending_updates_source` via `softwareupdate -l`
> (mises à jour système + apps Apple — distinct des mises à jour Homebrew
> par logiciel, voir § Mises à jour logicielles ci-dessous). `kb` reste
> vide (pas d'équivalent macOS) et `security` reste toujours `False` :
> `softwareupdate -l` ne distingue pas fiablement une mise à jour de
> sécurité d'une mise à jour de confort — `pending_updates_security` n'est
> donc jamais posé sur macOS, jamais un verdict inventé sans preuve fiable
> (même principe que `dns_check_reponse`). Licences/activation, correctifs
> déjà installés (`hotfixes`) : pas d'équivalent, macOS n'a ni notion de
> licence Windows ni de liste de correctifs individuels installés.

### Comptes & logiciels
`users[]` (statut + appartenance au groupe Administrateurs) ·
`users_details[]{name, status, enabled, admin, role, account_type,
description, password_never_expires, last_logon}` ·
`installed_software[]{name, version, publisher, install_date, update_status,
latest_version, update_source}`

> macOS (depuis 3.5) : `version`/`publisher`/`install_date` sont désormais
> remplis via `system_profiler SPApplicationsDataType -json`, complété par
> les formules/casks Homebrew (`/usr/local/opt` **et** `/opt/homebrew/opt`
> — Intel et Apple Silicon, les deux chemins sont tentés faute de savoir
> lequel est actif) et les paquets `pkgutil` sans bundle `.app`, ces deux
> derniers restant nom seul comme avant 3.5. `SPApplicationsDataType` est
> réputé lent (vérifie la signature de chaque application) : timeout de
> 60 s, best-effort comme le reste du fichier.

#### Mises à jour logicielles (depuis 3.1, tri-état depuis 3.2)

Chaque entrée de `installed_software` reçoit deux champs supplémentaires,
calculés par `check_software_updates()` juste après l'inventaire :

| Champ | Valeurs | Notes |
|---|---|---|
| `update_status` | `'obsolete'` \| `'a_jour'` \| `'inconnu'` | Voir tableau de décision ci-dessous |
| `latest_version` | version dispo, ou `''` | Rempli seulement si `update_status == 'obsolete'` |
| `update_source` | `'winget'` \| `'brew'` \| `'apt'` \| `'dnf/yum'` \| `'pacman'` | Présent pour `'obsolete'` **et** `'a_jour'` |

`'a_jour'` n'est posé QUE quand le gestionnaire de paquets confirme
explicitement connaître ce logiciel — jamais déduit d'un simple silence,
qui ne distingue pas « connu et à jour » de « pas indexé par cette source » :

| OS | Comment `'a_jour'` est confirmé | Portée |
|---|---|---|
| Windows | `winget list` recoupé : présent avec une colonne Source non vide (une entrée sans source vient d'une lecture brute du Panneau de configuration, sans rien à vérifier) | Paquets connus de winget uniquement |
| Linux | `installed_software` vient déjà du même gestionnaire que celui interrogé ici (dpkg/apt, rpm/dnf, pacman partagent chacun la même base de paquets que leur outil d'inventaire) | Tout `installed_software` quand apt/dnf/yum/pacman est présent |
| macOS | Jamais — `installed_software` mélange /Applications, `pkgutil` et Homebrew sans distinguer leur origine ; rien ne garantit qu'un logiciel donné soit dans le champ de `brew` | — |

Source consultée par OS pour la liste des mises à jour DISPONIBLES — chacune
est une commande locale, best-effort (résultat vide si l'outil est absent,
hors ligne, ou le format de sortie a changé) :

| OS | Commande | Portée |
|---|---|---|
| Windows | `winget upgrade --include-unknown` | Paquets connus de winget uniquement |
| macOS | `brew outdated --json=v2` | Formules + casks Homebrew uniquement |
| Linux | `apt list --upgradable` puis `dnf`/`yum check-update` puis `pacman -Qu` (le premier gestionnaire présent qui répond) | Paquets du gestionnaire natif de la distribution |

Le rapprochement entre le nom du registre/du gestionnaire de paquets se fait
via `_normalize_software_name()` (casse, ponctuation et architecture entre
parenthèses ignorées) — un logiciel installé hors de ces canaux (exe
téléchargé à la main, par exemple) reste donc en `'inconnu'`.

> `winget upgrade`/`winget list` partagent le même parseur de tableau
> texte, `_parse_winget_table()` : positionnel (colonnes nom/ID/version/
> disponible/source, ordre fixe), jamais par intitulé d'en-tête (localisé
> selon la langue de Windows). Gère aussi plusieurs tableaux dans une même
> sortie (`upgrade` en affiche un second pour les mises à jour nécessitant
> un ciblage explicite). Vérifié sur un poste réel : 106 logiciels avec
> mise à jour disponible + 102 confirmés à jour sur 878 au total (le reste,
> inconnu de winget — pilotes, redistribuables, installations manuelles).

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

macOS a ses propres sources gated par l'élévation, sans équivalent du champ
`elevated` détaillé ci-dessus (absence silencieuse, même principe) :
connexion à distance/SSH (`systemsetup -getremotelogin` refuse la lecture
sans root) et Partage d'écran (`launchctl print system/…`, incomplet sans
root) — voir § Accès distant & exposition.

### Proposition d'élévation au lancement (GUI, depuis 3.9)

`system-info-collector-gui.py` détecte l'absence d'élévation dès le
démarrage et propose une boîte de dialogue (« Relancer avec les droits
administrateur ? ») avant d'afficher la fenêtre principale — jamais de
relance automatique sans confirmation explicite du technicien.

- **Windows** : sur confirmation, relance via `ShellExecuteW(..., "runas",
  ...)` (déclenche l'invite UAC standard) et ferme le process courant. Le
  code de retour de `ShellExecuteW` se lit de façon synchrone et fiable
  (`<= 32` = échec/annulation) : pas de zone grise, contrairement à macOS.
- **macOS (depuis 3.13, `_relancer_macos_eleve()`)** : sur confirmation,
  relance via `osascript -e 'do shell script "…" with administrator
  privileges'` — déclenche l'invite d'authentification macOS standard (la
  même que Réglages Système ou l'installation d'un logiciel), sans ligne de
  commande à taper. **Fire-and-forget, volontairement, et la fenêtre
  courante n'est jamais fermée automatiquement** : contrairement à
  `ShellExecuteW`, rien ne permet de savoir si l'authentification a réussi
  sans risquer d'attendre — la commande `osascript` reste bloquée tant que
  LE COLLECTEUR ÉLEVÉ tourne, pas seulement le temps de l'authentification.
  Deviner à partir d'un délai d'attente aurait pu fermer la fenêtre
  courante sur un faux positif (authentification lente puis annulée). Au
  pire, le technicien se retrouve avec deux fenêtres ouvertes une fois
  authentifié — jamais avec aucune.
- **Linux** : pas d'équivalent standard à `osascript` sur l'ensemble des
  environnements de bureau. La boîte de dialogue affiche la commande
  `sudo …` à lancer soi-même dans un terminal, puis la collecte continue
  sans élévation.

> **App Translocation (macOS, corrigé en 3.9, message mis à jour en 3.14) :**
> si l'app tourne encore sous protection Gatekeeper « App Translocation »
> (jamais déplacée depuis le téléchargement), `sys.executable`/`argv[0]`
> reflètent un chemin temporaire en lecture seule
> (`/private/var/folders/…/AppTranslocation/…`), propre à cette session de
> lancement — tout aussi inexploitable pour `_relancer_macos_eleve()` que
> pour une commande `sudo`. `_proposer_elevation()` détecte ce cas
> (`/AppTranslocation/` dans le chemin) et affiche la marche à suivre pour
> lever la translocation **sans Terminal** : déplacer l'app avec le Finder
> (glisser-déposer vers Applications ou le Bureau) suffit — macOS ne la
> relance plus jamais depuis un emplacement temporaire dès qu'elle a
> quitté une fois son dossier de téléchargement d'origine. `xattr -cr`
> reste mentionné en alternative pour qui préfère le Terminal, mais n'est
> plus présenté comme la seule solution (il ne l'a jamais été — le
> message d'origine, en 3.9, l'omettait par erreur).

Le collecteur CLI (`system-info-collector.py`) n'a pas cette boîte de
dialogue — il journalise le même avertissement mais reste non-interactif,
cohérent avec son usage scripté (déploiement en masse, `--quiet`).

---

## 🖥️ Couverture par système

| Donnée | Windows | macOS | Linux |
|---|:---:|:---:|:---:|
| Identification, OS, CPU, RAM, disques | ✅ | ✅ | ✅ |
| Carte mère / châssis | ✅ | — | ✅ (`/sys/class/dmi`) |
| Barrettes mémoire par slot | ✅ | ✅ | ⚠️ root requis |
| Écrans | ✅ (EDID) | ⚠️ marque devinée par heuristique, pas de taille physique (depuis 3.10 — voir note) | ⚠️ nom du connecteur seul |
| Carte graphique (GPU) | ✅ | ✅ (depuis 3.12 — voir note, absent avant) | — |
| Imprimantes | ✅ | ✅ (CUPS, depuis 3.5) | ✅ (CUPS) |
| Usure disque / SMART détaillé | ✅ | — | ⚠️ type seul (`lsblk`) |
| Usure batterie | ✅ | ✅ | — |
| Licences / activation | ✅ | — | — |
| Correctifs | ✅ | ✅ `softwareupdate -l` (système + apps Apple, depuis 3.5) | — |
| Chiffrement | ✅ BitLocker | ✅ FileVault | — |
| Pare-feu | ✅ | ✅ | — |
| TPM / Secure Boot | ✅ | ⚠️ équivalents macOS : SIP + Gatekeeper (depuis 3.8, pas les mêmes mécanismes — voir note) | — |
| Inscription MDM (Jamf, Kandji, Mosyle, ABM…) | — (sans équivalent Windows) | ✅ (depuis 3.8) | — |
| Détections antivirus (Defender) | ✅ | ⚠️ XProtect toujours signalé, tiers détecté au mieux (voir § Agents) | — |
| Pilotes non signés | ✅ | — | — |
| Règles de pare-feu (filtrées, non par défaut) | ✅ | — | — |
| Redirections du fichier hosts (filtrées) | ✅ | ✅ | ✅ |
| Redirections de port (portproxy) | ✅ | — | — |
| Réseaux Wi-Fi enregistrés (SSID + mot de passe optionnel) | ✅ | ⚠️ SSID seul, jamais le mot de passe (depuis 3.8 — voir note) | — |
| Stratégies de groupe appliquées | ✅ | — | — |
| Passerelle par défaut, DNS, proxy | ✅ | ✅ (`route`/`scutil`, depuis 3.10 — voir note) | — |
| Ports en écoute | ✅ | ✅ (`lsof`, depuis 3.5) | ✅ (`ss`) |
| Comptes locaux | ✅ | ✅ | ✅ |
| Logiciels (nom+version+éditeur) | ✅ | ✅ `SPApplicationsDataType` (depuis 3.5, éditeur vide pour les apps non signées — voir note) | ✅ dpkg/rpm/pacman |
| Mise à jour logicielle disponible | ✅ winget | ✅ brew (par logiciel) + `softwareupdate` (système, depuis 3.5) | ✅ apt/dnf/pacman |
| Périphériques USB | ✅ | ✅ (`system_profiler SPUSBDataType`) | — |
| Accès distant (RDP/WinRM/SSH/Telnet…) | ✅ | ⚠️ SSH + Partage d'écran, root requis (depuis 3.5) | — |
| Agents de télémaintenance & EDR | ✅ | ⚠️ best-effort par nom de process/app, root non requis (depuis 3.5) | — |
| Comptes de messagerie (Outlook, Thunderbird) | ✅ | — | — |
| Politique de mot de passe local | ✅ (admin) | — | — |
| Sauvegardes (Time Machine côté macOS) | — (Windows : `restore_points` = Restauration système) | ✅ (depuis 3.8 — voir note, réutilise `restore_points`) | — |
| Plan d'alimentation / démarrage rapide | ✅ | — | — |
| Versions .NET installées | ✅ | — | — |
| Style de partition / mode de démarrage | ✅ | — | — |
| Diagnostic — plantages applicatifs & paniques noyau | ✅ (incidents, erreurs, arrêts…) | ✅ (depuis 3.8, réutilise `system_incidents` — voir note) | — |
| Diagnostic — agents/démons au démarrage | ✅ (`startup_programs`) | ✅ (depuis 3.8, LaunchAgents/LaunchDaemons — voir note) | — |
| Processus les plus gourmands (CPU/RAM, instantané) | ✅ | ✅ (`ps`, depuis 3.8) | — |

---

## ⚙️ Performance

La collecte Windows tient en **35 étapes groupées** (`_WIN_STEPS` dans
`collector_core.py`, une poignée d'appels PowerShell chacune plutôt qu'un
appel par donnée). Chaque bloc est protégé par son propre `try/catch` côté
PowerShell **et** côté Python : une source indisponible (module absent,
privilège manquant, édition Windows différente) ne fait jamais échouer les autres.

Point d'attention : `SoftwareLicensingProduct` doit impérativement être interrogé
avec un filtre WQL (`PartialProductKey IS NOT NULL`). Sans filtre, l'énumération
parcourt plusieurs centaines d'entrées et dépasse 30 s à elle seule.

L'étape « Vérification des mises à jour logicielles » (`check_software_updates()`)
appelle un gestionnaire de paquets local (winget/brew/apt/dnf/pacman), donc sa
durée dépend de l'accès réseau de la machine : jusqu'à 90 s de timeout sur
Windows (`winget upgrade` peut resynchroniser ses sources). Comme les autres
étapes, un échec ou une absence d'outil ne bloque jamais la collecte — le
statut retombe alors à `'inconnu'` pour tous les logiciels.

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

### Exécutables compilés

Windows dispose de longue date de `system-info-collector.exe`/`-gui.exe`
(GitHub Releases). Depuis 3.5, macOS Intel a l'équivalent — construits en
best-effort par le job `build-macos-intel` (`.github/workflows/build-release.yml`,
même croisement Rosetta que `ParcInfo-macOS-Intel.zip`) :
`system-info-collector-macOS-Intel.zip` (binaire brut, en ligne de commande)
et `system-info-collector-gui-macOS-Intel.zip` (`ParcInfo-Collector.app`).
S'ils manquent exceptionnellement à une release (échec du croisement Tk pour
la GUI, notamment), `python system-info-collector[-gui].py` reste toujours
la solution de repli — c'est d'ailleurs elle qu'utilise directement macOS
Apple Silicon, jamais compilé pour l'instant.

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

**Dernière mise à jour** : 2026-08-21 (v2.18.37 — collecteur 3.15, correctif nom DNS macOS + doublons stockage APFS)
