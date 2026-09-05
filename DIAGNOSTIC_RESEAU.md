# Diagnostic réseau — référence

Module `network_diag.py` + routes `/api/diag-reseau/*` dans `app.py`. Page
**Inventaire → Diagnostic réseau** (`/diag-reseau`). Analyse la santé du réseau
du **client actif** ; les évènements sont rattachés à ce client.

> Ce document décrit le comportement au 2026-09-05 (v2.19.37). En cas de doute
> sur une valeur par défaut, vérifier `config_helpers.py:CFG_DEFAULTS` et
> `network_diag.py`.

---

## Les six paliers

| # | Nom | Ce qu'il observe | Prérequis | Limites |
|---|-----|------------------|-----------|---------|
| 1 | **Diagnostic actif** | Tables ARP de l'OS, ping en rafale (perte/latence/gigue), DHCPDISCOVER, requêtes NBNS/DNS | Aucun | Le DHCP pirate est best-effort (port 68 souvent occupé) |
| 2 | **Capture passive** | Trames vues sur l'interface (ARP, DHCP, STP, RA IPv6, en-têtes TCP) | `scapy` + privilèges + Npcap (Windows) ; en conteneur `network_mode: host` + `cap_add: [NET_RAW, NET_ADMIN]` | Ne voit que le trafic qui atteint la sonde (pas au-delà du switch) |
| 3 | **Interrogation SNMP** | Compteurs par port des switchs/routeurs/NAS (erreurs, duplex, débit, état) | SNMP v1/v2c **ou v3 authNoPriv** en lecture seule ; `diag_snmp_actif` | Deux relevés successifs nécessaires pour un delta |
| 4 | **Topologie L2** | Tables MAC (bridge-MIB FDB) + LLDP/CDP + STP → quel appareil sur quel port, quel switch derrière quel switch. Découverte **récursive** par `lldpRemManAddr` | Palier 3 actif + `diag_topologie_active` | FDB volatile (une machine éteinte disparaît en ~300 s) ; un switch non manageable ne dit rien |
| 5 | **Tendances & baseline** | Historique des métriques (liaison, ports) → dégradation *relative* | `diag_baseline_active` (défaut on) ; ~8 points d'historique | Ne remplace pas les seuils absolus, les complète |
| 6 | **Rapport & remédiation** | — (synthèse) | reportlab pour le PDF (repli HTML sinon) | — |
| 7a | **Wi-Fi (poste)** | État Wi-Fi du poste ParcInfo + AP visibles (`netsh wlan` / `iw` / `system_profiler`) | `diag_wifi_active` (défaut on) ; un adaptateur Wi-Fi | Vision depuis un seul point ; scan macOS best-effort |
| 7b | **Onduleurs SNMP** | UPS-MIB (source secteur/batterie, charge, autonomie, batterie, alarmes) + repli APC | Palier 3 actif + `diag_ups_active` ; appareil de type `Onduleur / UPS` avec IP | Dépend de ce que la carte réseau de l'onduleur expose |

**Deux modes** : *snapshot* à la demande (bouton « Lancer un diagnostic », mode
rapide possible) et *surveillance continue* (`diag_surveillance_active`, thread
démon `_moniteur_loop` calqué sur le watchdog ping, période `diag_intervalle_s`).

---

## Palier 4 — topologie L2, en détail (revu v2.19.32)

Onglet **Topologie** de `/diag-reseau`. Trois façons de le relever : le bouton
**« Cartographier maintenant »** (job dédié en tâche de fond,
`lancer_cartographie` / `statut_cartographie`, routes
`POST /api/diag-reseau/topologie/cartographier` et `GET …/statut`), la phase
`topologie` d'un snapshot complet, ou le cycle de surveillance continue.
Le bouton dédié existe parce que la phase de snapshot arrive en avant-dernière
position, derrière un garde `_budget_ok` : une liaison lente suffisait à la
sauter snapshot après snapshot.

### Ce qui est relevé, par équipement

| Source | Fonction | Ce qu'elle apporte |
|--------|----------|--------------------|
| Table MAC | `_releve_mac_switch` | quelle MAC sur quel ifIndex, **et dans quel VLAN** (l'index `dot1qTpFdbPort` porte le fdbId ; repli `dot1qPvid` par port) |
| LLDP / CDP | `_voisins_lldp_cdp` | nom, port, capacités, MAC de châssis et **IP de gestion** (`lldpRemManAddr`) du voisin |
| STP | `_stp_switch` | port vers la racine, pont amont de chaque port, état (`passant`, `bloquant`…) |
| ENTITY-MIB | `_entite_physique` | modèle exact, numéro de série, composition d'un stack |
| IP-MIB | `_sous_reseaux_equipement` | sous-réseaux portés / routés, proposés comme plages de scan |
| Noms d'interface | `_noms_interfaces` | **cache partagé** avec la vue d'activité (TTL 90 s) — la topologie ne refait plus un `interroger_equipement` complet |

### Découverte récursive

`decouvrir_topologie` part des appareils inventoriés de type réseau avec une IP,
puis suit les `lldpRemManAddr` des voisins jusqu'à `_TOPO_PROFONDEUR_MAX` (3)
sauts, plafonné à `_TOPO_EQUIP_MAX` (40) équipements et à un budget en secondes.
Les équipements atteints qui ne sont **pas** dans l'inventaire sont renvoyés dans
`decouverts` et affichés sous la carte : ils ne sont **jamais créés d'office**.
Les relevés sont menés `_TOPO_WORKERS` (6) de front — les switchs sont
indépendants et l'attente est du temps réseau.

### Port d'accès contre uplink

Chaque ligne de `diag_topologie` porte `nb_macs_port` et `est_uplink`. Un port
est un **uplink** dès qu'il apprend plus d'une MAC, qu'il a un voisin
pont / routeur / borne, ou qu'il est le port racine STP. Cette distinction est la
condition de sûreté de tout ce qui écrit dans la baie : un uplink apprend toutes
les MAC d'en aval, et sans elle le premier appareil arbitraire vu derrière un
uplink se retrouvait affecté au port d'uplink.

### Reporter dans la baie

`proposer_topologie_baie` (lecture seule, `GET /api/diag-reseau/topologie/proposer`)
rend `{propositions, ignores}` ; l'interface en fait une modale à cases à cocher,
et `appliquer_topologie_baie(client_id, selection)` n'écrit que la sélection.
Deux garde-fous :

- le numéro de port de la baie vient de **`_mapping_baie_ifindex`** (manuel >
  topologie > nom d'interface), jamais de l'ifIndex SNMP. Le numéro de façade
  n'est dans aucune MIB : le déduire est la seule voie, et un ifIndex n'est
  presque jamais un numéro de façade — un switch qui numérote en 10101 faisait
  créer un port 10101 dans le rack ;
- un port de façade **absent du châssis n'est plus créé**, il part dans
  `ignores` avec son motif.

### Historique des mouvements

`diag_topologie` est écrasée à chaque passage. `_journaliser_mouvements` compare
l'ancien relevé au nouveau **avant** l'écrasement et écrit les transitions dans
`diag_topologie_mouvements` : `apparu`, `disparu`, `deplace` (avec port avant /
après). Uniquement sur les ports d'accès — un uplink bougerait sans arrêt.
Rétention `diag_reseau_max_jours`.

### Quand un équipement ne répond pas

`_topologie_equipement` commence par une sonde `_snmp_presence` (un `GET
sysDescr`, ~1 s). Si l'agent n'est pas lisible, elle rend la main aussitôt et
l'IP part dans `muets` — avec le motif exact (`_snmp_presence` le calcule déjà :
« aucune réponse SNMP » si l'agent est silencieux, « agent présent mais
communauté/utilisateur v3 refusés » s'il répond à la découverte v3 sans
accepter la lecture), affiché tel quel sous la carte. Sans cette sonde, les
~20 parcours SNMP qui suivent expiraient un par un — près d'une minute par
équipement, pendant laquelle la cartographie paraissait figée. Une échéance
(`deadline`) est ensuite propagée : LLDP/CDP, STP, ENTITY-MIB et sous-réseaux
sont sautés quand le budget se tend, dans cet ordre de priorité décroissante.

Côté orchestration, `as_completed` reçoit le délai restant et l'exécuteur est
fermé avec `wait=False` : un relevé qui traîne n'immobilise plus le job, son
résultat est simplement ignoré. La progression est rapportée à **chaque**
équipement terminé, pas une fois par lot.

### Ce que ce palier ne pourra pas faire

- Un **switch non manageable** ne dit rien : ni MIB, ni LLDP. On ne peut que
  l'inférer (plusieurs MAC sur un port, `_classer_cascade`).
- La **FDB est volatile** : une machine éteinte disparaît en ~300 s. La carte
  reflète ce qui a parlé récemment, jamais l'inventaire complet — d'où
  l'affichage systématique de sa fraîcheur.
- Le **numéro sérigraphié sur la façade n'existe dans aucune MIB standard**. Le
  mapping restera une déduction (nom d'interface) ou une calibration humaine.
- Pas de SNMP en écriture : ParcInfo constate et propose, il ne corrige pas.

### Sous-réseaux supplémentaires d'un site (page Scan réseau)

Un site à plusieurs sous-réseaux routés (le même routeur dessert par exemple
`192.168.1.0/24` **et** `192.168.0.0/24`) laissait jusqu'ici les appareils du
second réseau invisibles partout — scan, baie, diagnostic — faute que
quelqu'un pense à taper cette plage à la main. Le routeur, lui, connaît ses
propres sous-réseaux.

`network_diag.sous_reseaux_detectes(client_id)` interroge chaque routeur/switch
SNMP de l'inventaire : sonde `_snmp_presence` d'abord (échec en ~1 s sur un
équipement pas encore configuré pour le SNMP), puis lecture de sa table
IP/routes via `_sous_reseaux_equipement` — le même relevé que la cartographie
utilise depuis 2.19.32. Résultat agrégé par CIDR, avec la liste des
équipements qui l'ont vu, en excluant ce qui est déjà déclaré dans
`parc_general.plage_ip_locale`. Route `GET /api/scan/sous-reseaux`, lecture
seule (aucun scan n'est déclenché).

La page **Scan réseau** affiche un encart « Sous-réseaux détectés via SNMP »
sous les plages déjà saisies, avec un bouton « + Ajouter » par plage proposée
— il réutilise `ajouterPlage()`, déjà capable de lancer un scan sur plusieurs
plages à la fois (`POST /api/scan/lancer` accepte `plage_ip` en liste). Rien
n'est scanné sans un clic explicite.

`parc_general.plage_ip_locale` accepte désormais **plusieurs plages séparées
par une virgule** (même convention que `diag_snmp_communautes`). Dans
`_appareil_sur_reseau_courant` (surveillance ping en tâche de fond), le site
est considéré confirmé dès qu'**une seule** des plages déclarées chevauche le
réseau local du poste ParcInfo — les *autres* plages du même site sont alors
elles aussi jugées joignables, même si elles ne chevauchent pas directement
son interface (elles sont routées via le même équipement). Sans ce
correctif, un appareil importé depuis le second sous-réseau restait invisible
pour le watchdog même après avoir complété `plage_ip_locale`.

**Limite assumée, pas un défaut à corriger** : un appareil scanné sur un
sous-réseau **routé** (pas directement rattaché à l'interface du poste
ParcInfo) n'aura pas de MAC résolue — l'ARP ne traverse pas un routeur. Ni
fabricant OUI, ni corrélation avec une topologie/FDB de switch. Pour
apparaître correctement placé dans la baie de brassage, c'est le switch qui
dessert *physiquement* cet appareil qui doit être ajouté à l'inventaire et
interrogé en SNMP — détecter le sous-réseau ne suffit pas à ça.

### Croiser SNMP et capture passive pour la MAC d'un hôte hors segment local

La limite ci-dessus (« pas de MAC résolue sur un sous-réseau routé ») n'est
pas contournable par l'ARP local — mais elle l'est en partie en interrogeant
une source qui, elle, a une vue L2 réelle sur ce segment-là : le routeur lui-
même. Deux sources complètent `_mac_from_arp()` dans `_scan_host()`,
**uniquement quand celui-ci ne renvoie rien** (jamais un écrasement d'une
résolution locale déjà obtenue) :

- **`network_diag.hotes_vus_snmp(client_id)`** — même sonde d'existence
  `_snmp_presence` que `sous_reseaux_detectes` ci-dessus (coupe court en ~1 s
  sur un équipement muet), puis lecture de la table ARP du routeur/switch
  (`ipNetToMediaPhysAddress`, repli `ipNetToPhysicalPhysAddress`). Cette table
  est une résolution L2 **réelle**, faite par l'équipement sur sa propre
  interface : la MAC qui en ressort est celle de l'appareil visé, jamais celle
  du routeur — elle traverse donc le VLAN là où l'ARP de ce poste ne le peut
  pas. `_ip_depuis_suffixe_arp()` extrait l'IP portée par l'index SNMP (les 4
  derniers sous-identifiants, identiques dans leur structure entre les deux
  formats de table). Plusieurs équipements voyant la même IP sont agrégés en
  une seule entrée, avec la liste de leurs noms.
- **`network_diag.capture_arp_sightings(duree)`** — écoute ARP passive courte
  (scapy, le même mécanisme que la capture du palier 2), pour un appareil qui
  ne répond à *aucune* sonde active mais parle ARP normalement (beaucoup
  d'objets connectés, imprimantes, caméras qui filtrent tout le reste).
  Complémentaire de la source SNMP : elle ne voit jamais au-delà du segment où
  tourne ParcInfo, contrairement à SNMP qui voit les sous-réseaux distants du
  routeur.

`_run_scan()` lance les deux relevés en threads parallèles (budgets 10 s /
9 s), aux côtés des découvertes UPnP/mDNS/ONVIF déjà existantes. La provenance
de la MAC est tracée explicitement (`result['mac_source']` = `'snmp'` ou
`'capture'`, `result['mac_sources']` pour SNMP) — jamais fondue silencieusement
avec une résolution locale. Dans la liste des résultats, un badge 🌐 (SNMP,
infobulle nommant l'équipement source) ou 📡 (capture) apparaît à côté de la
MAC ; le badge « muet » 🔇 précise « sur un sous-réseau routé (VLAN) » plutôt
que le message générique « pare-feu / ICMP bloqué » quand la source est SNMP.

Cela ne lève **pas** la limite de corrélation topologie/FDB mentionnée
ci-dessus : un appareil ainsi résolu reste « vu » sans être *placé* dans la
baie, faute que son propre switch d'accès soit, lui, inventorié et interrogé.

---

## Vue d'activité de la baie (LEDs live) — v2.19.6, revues v2.19.8 / v2.19.9 / v2.19.10

Pas un palier : une **vue temps réel**, pas une détection. Tant que la page
`/baie` (sélecteur « ⚡ activité ») **ou** le widget « Activité réseau » du
tableau de bord est ouvert, le navigateur envoie un battement à
`GET /api/baie/activite` toutes les 3 s. Un thread démon `_activite_loop`
(`network_diag.py`, calqué sur `_moniteur_loop`, démarré à la première requête,
se rendort dès qu'aucun battement < 20 s) interroge alors les compteurs SNMP
des switchs de la baie et calcule l'état à peindre par port.

- **Quels équipements** : `_switchs_baie` — un slot de baie lié à un appareil
  doté d'une **adresse IP**, dont le `type_appareil` est réseau (`_TYPES_EQUIP_SNMP`)
  **ou** dont l'étiquette de slot `type_equipement` vaut Switch/Switch·AP/Routeur.
  Le résultat porte `nb_switchs` / `nb_muets` / `motif` (`aucun_switch` |
  `sans_reponse`) pour que le bandeau explique l'absence de données.
- **Relevé SNMP** (v2.19.9) : `_noms_interfaces` récupère nom/alias/type/vitesse
  de toutes les interfaces (caché ~90 s, **rafraîchi en tâche de fond** pour ne
  pas bloquer le relevé sur un switch lent) ; `_poll_switch_ports` relève chaque
  cycle `ifOperStatus` + octets + paquets (in/out, 64 bits si l'`ifXTable`
  existe, sinon 32 bits) via **`_snmp_bulk_cols`** (`app.py`) — un parcours
  **GETBULK v2c multi-colonnes auto-descriptif** : chaque varbind porte son OID,
  l'association est faite par préfixe (jamais par position, contrairement à
  `_snmp_get_typed` qui se trompait sur les agents ne respectant pas l'ordre
  RFC 1157 → valeurs identiques par lot). Repli GETNEXT par colonne.
  `_snmp_walk` **et** `_snmp_bulk_cols` vérifient le request-id de la réponse
  (un switch lent renvoyait parfois une réponse tardive à la requête
  précédente → relevé décalé). Erreurs relevées 1 cycle sur 8.
- **Réponse partielle** (v2.19.15) : si la colonne `ifOperStatus` manque pour un
  port (réponse GETBULK tronquée, ou démarrage où 10 colonnes sont demandées le
  temps que `_activite_hc` se fixe) alors que les octets répondent,
  `_poll_switch_ports` renvoie `oper=None` + `oper_ok=False` — **jamais un
  « up » fabriqué**. `_cycle_activite` traite ce port comme un relevé manqué
  (dernier état conservé). Avant : tous les ports, débranchés compris,
  s'allumaient une ou deux secondes.
- **Δt via l'horloge de l'agent** (v2.19.14) : `sysUpTime` (`1.3.6.1.2.1.1.3.0`)
  est lu dans le même GETBULK ; le débit se calcule sur
  `(sysUpTime_now - sysUpTime_prev) / 100`, exact quelle que soit la durée du
  poll (5 à 25 s sur un switch lent, variable d'un cycle à l'autre, faussait le
  débit de ~20 %). `_activite_sut`. Repli sur l'horloge du poste si `sysUpTime`
  manque. Un `sysUpTime` qui **recule** = redémarrage du switch → un message
  journal, deltas ignorés ce cycle.
- **Compteurs d'OCTETS bloqués / bouclage 32 bits** : certains agents bas de
  gamme figent leurs compteurs d'octets 32 bits à `0x7FFFFFFF` (2 Go) — détecté
  (`_CPT_SENTINELLE_32`), remonté (`cpt_pegge`, journal « compteurs bloqués »
  une fois), pas de débit inventé. Depuis v2.19.16, `cpt_pegge` ne touche plus
  au **pps** : les compteurs de paquets restent souvent bons sur ces switchs, la
  LED clignote donc quand même. Garde-fous (v2.19.17) : les compteurs de paquets
  eux aussi menteurs sont écartés par **cohérence octets/paquets** (un paquet
  fait ≥ 64 octets → `pps ≤ bps / 512`) quand le débit est fiable, sinon plafond
  absolu `_ACTIVITE_PPS_PEGGE_MAX` (50 000) ; un cycle délirant → `pps = 0` sans
  traîner de pic dans l'EMA ; la sortie de `traffic` attend `_ACTIVITE_DEBOUNCE`
  × 2 relevés calmes (ces compteurs bougent par à-coups). Un compteur 32 bits
  qui **boucle** (v2.19.14 : `prev` proche de 2³²) donne le vrai delta
  `(2³² - prev) + cur` au lieu d'être pris pour un redémarrage — sur un lien
  Gigabit chargé, `ifInOctets` boucle toutes les 34 s.
- **Plafond de plausibilité** (v2.19.10) : un compteur qui « dépègue »
  (`0x7FFFFFFF` → valeur réelle), boucle, ou bascule 64↔32 bits d'un cycle à
  l'autre injecte un delta gigantesque. `_etat_led` borne débit et paquets/s à
  la capacité physique du lien (plafond absolu `_ACTIVITE_PPS_MAX_PORT` si la
  vitesse est inconnue) — au-delà, c'est un artefact, on garde la dernière valeur
  lissée. Corrige le moniteur qui affichait des millions de pkt/s sur des ports
  inactifs.
- **PoE par port** (v2.19.10, `_poll_poe`) : si le switch expose la
  POWER-ETHERNET-MIB (RFC 3621), `pethPsePortTable` est relevée à chaque cycle —
  statut (`deliveringPower` / `fault`), classe 0-4 — plus les scalaires
  `pethMainPse` (budget / consommation, **eux réels**). La table est indexée
  `groupe.port` : le composant `port` **est** le numéro de port physique, donc
  recoupé directement au numéro de baie (pas de mapping ifIndex). La puissance
  par port affichée est le **plafond de sa classe** (`≤ X W`), pas une mesure —
  la RFC ne l'expose pas. Colonne PoE dans le moniteur, total dans l'en-tête,
  évènement journal sur défaut PoE **et sur budget presque atteint** (v2.19.14,
  > 90 % du budget).
- **Contexte du switch** (v2.19.14, `_lire_sysinfo`, cache 10 min) : `sysName`,
  modèle (`sysDescr` tronqué) et uptime (« up 48 j » / « redémarré il y a
  12 min ») dans l'en-tête du moniteur — un redémarrage explique les resets de
  compteurs.
- **Mini-courbe par port** (v2.19.14) : `_activite_hist`, une deque de
  `_ACTIVITE_HIST_MAX` (60) échantillons `[ts, bps, pps]` par port mappé, en
  mémoire, affichée en sparkline dans le moniteur (colonne « tendance »). La
  sparkline trace le **débit**, ou les **paquets/s** pour un port dont les
  compteurs d'octets sont bloqués (v2.19.17 — sinon elle restait plate).
- **Prises murales d'un bandeau RJ** (v2.19.18, `_prises_murales_activite`) : une
  prise murale est passive (pas de SNMP), mais elle est reliée par le cordon de
  brassage (`baie_slot_ports.lie_slot_id`/`lie_port_numero`, mode « 🔗 Lier des
  ports ») à un port de switch. On suit ce lien jusqu'au port de switch, on
  résout son ifIndex via le `_mapping_baie_ifindex` du slot switch, et on
  **réutilise l'état SNMP déjà relevé** (`etats_par_ip`) → la LED de la prise est
  animée comme celles des ports de switch, sans relevé supplémentaire.
  **Contrôle de câblage** (revu v2.19.19) : `_fdb_switch` relève à chaque cycle la
  **table d'apprentissage MAC « live » du switch** (bridge-MIB `dot1q`/`dot1d`,
  `{ifIndex: set(mac)}`, cache `_ACTIVITE_FDB_TTL` 150 s). Pour une prise, la MAC
  de l'appareil déclaré (`baie_prises_murales.appareil_id` → `appareils.adresse_mac`)
  doit figurer sur le port de switch au bout du cordon ; si le port apprend
  d'autres MAC mais pas celle-là → `incoherent` (badge rouge ⚠, évènement journal
  une fois nommant l'appareil réellement vu). Repli sur la topologie L2
  (`diag_topologie`, palier 4) si le switch ne répond pas à la bridge-MIB ; `ok`
  quand ça correspond, `inconnu` sans donnée exploitable. Le walk FDB est résilient
  face à un agent SNMP lent : relevé **en tête de cycle** (avant les GETBULK qui le
  saturent), mémorisation du dialecte qui répond (`_activite_fdb_dialecte`),
  backoff `_ACTIVITE_FDB_BACKOFF` 45 s après échec, service de la dernière valeur
  connue jusqu'à `_ACTIVITE_FDB_PERIME` 15 min.
- **Appareils vus par port** (v2.19.19, `_voisins_port`) : la même FDB « live »
  alimente, dans l'infobulle de chaque port (switch **et** prise murale) et dans
  le détail du moniteur, la liste des appareils dont une MAC transite réellement
  par le port (« trafic : PC-A, PC-B +3 » — nom d'inventaire, sinon fabricant OUI).
  v2.19.20 : ces infos vont dans l'**infobulle riche existante** (lignes
  « Activité » / « Trafic vu » / « Câblage » ajoutées par `rowsActivitePort`,
  alimentées par `_activiteInfos`), plus de `title` natif en double.
- **Relevé de table MAC unifié** (v2.19.25, `_releve_mac_switch`) : point d'entrée
  unique partagé par le **cycle d'activité**, **`analyser_brassage_baie`** et la
  **découverte de topologie** (palier 4, `_topologie_equipement`). Enchaîne
  `_fdb_switch` (bridge dot1q/dot1d, **contexte de communauté `public@<vlan>`**
  via `_fdb_par_vlan` pour les switchs Cisco/HP qui l'exigent, **+ table ARP**
  `ipNetToMediaPhysAddress` lue en GETBULK plafonné par `_snmp_walk_octets`)
  puis `_fdb_corriger` (hypothèses de forme + réglage `diag_fdb_mode:<ip>`).
  `_macs_infra_switch` apprend toutes les MAC propres du switch
  (`dot1dBaseBridgeAddress` + `ifPhysAddress`).
- **Plusieurs MAC par appareil** (v2.19.27, table `appareil_macs`,
  `network_diag._macs_secondaires`) : `appareils.adresse_mac` reste la MAC
  principale ; les autres cartes (serveur bi-NIC, routeur WAN+LAN, borne
  2 radios, NVR, carte d'administration iDRAC/iLO) sont enregistrées à part et
  fusionnées dans l'inventaire MAC de **toutes** les corrélations
  (`analyser_brassage_baie`, `_elements_baie` → `mac_infra`, cycle d'activité,
  `_topologie_equipement`, `capturer_trafic`). Alimentée par le collecteur
  système (cartes `physical` seulement — Hyper-V/WSL/Docker/VPN exclus), par le
  scan (MAC vue à une IP déjà rattachée, ≠ principale) et à la main dans la
  fiche appareil (« Adresses MAC supplémentaires »).
- **Voisins LLDP + CDP** (v2.19.25, `_voisins_lldp_cdp`) : `lldpRemSysCapEnabled`
  (bits pont / routeur / borne Wi-Fi / téléphone / modem câble / répéteur),
  `lldpRemChassisId` (MAC du voisin → recoupement inventaire même sans FDB),
  `lldpRemPortIdSubtype` (3=MAC, 5=ifName, 7=local) pour interpréter le port du
  voisin ; complété par CDP (`cdpCacheDeviceId`/`Platform`/`Capabilities`…).
  Persisté dans `diag_topologie` (`voisin_caps`, `voisin_mac`,
  `voisin_port_subtype`, `voisin_source`). `_classer_cascade` s'en sert :
  cap `wlan` → borne Wi-Fi certaine, `bridge` seul → switch.
- **Deviner le brassage — « carte réseau proposée »** (v2.19.22,
  `analyser_brassage_baie`, bouton « 🧠 Deviner le brassage » sur `/baie`, route
  `GET /api/baie/brassage/proposer`, **aperçu seul**). Part de **toutes** les MAC
  apprises sur les switchs de la baie (`_fdb_switch` par IP), les corrèle à
  l'inventaire (`appareils.adresse_mac` → nom d'inventaire), et rend 4 groupes de
  propositions cochables + 2 listes d'information. `_elements_baie` fournit
  `{appareil_id → slot/ports}` et `{mac → appareil_id d'infra}`.
  - **Relevé en tâche de fond** (v2.19.28) : le balayage SNMP tourne dans un
    thread (`lancer_analyse_brassage` / `statut_analyse_brassage`, thread
    `DiagBrassage`), jamais dans le fil de la requête HTTP. La route
    `GET /api/baie/brassage/proposer` démarre le job et renvoie
    `{en_cours, progress, message}` ; la modale poll la même route jusqu'au
    résultat (`?forcer=1` = balayage neuf, après un changement de réglage FDB).
    Un résultat de moins de `_BRASSAGE_CACHE_S` (90 s) est resservi tel quel.
    Budget de sécurité 150 s → résultat partiel + `switchs_non_releves`
    (bannière). Motif `switchs_muets` = des switchs sont configurés mais aucun
    ne répond en SNMP.
  - **`prises_appareils`** : port de switch **brassé** (`lie_*`) vers un bandeau,
    1 MAC inventoriée dessus → assigne la machine à la **prise murale**
    (`PUT /api/baie/prise-murale/<slot>/<num>` `{appareil_id}`).
  - **`ports_appareils`** : port de switch **non brassé**, 1 MAC inventoriée →
    assigne la machine **au port de switch** (`PUT /api/baie/slot/<id>/port/<num>`).
    Ignoré si la machine est déjà déclarée sur une prise (elle relève alors du
    cordon).
  - **`cordons`** : une machine déclarée sur une prise, pas encore de cordon, sa
    MAC vue sur un port de switch (le **moins chargé** ; `> _BRASSAGE_UPLINK_MAX`
    co-MAC ou port déjà brassé ailleurs → confiance « faible ») → propose le
    cordon bandeau ⇄ switch (`POST /api/baie/lien-port`).
  - **Élément de baie vu sur un port de switch** (v2.19.30-31) : un appareil
    positionné dans le rack (NAS, serveur, imprimante, caméra, 2ᵉ switch…)
    reconnu par sa MAC sur un port de switch → proposition `liens_baie`. Port
    du voisin : résolu par LLDP/CDP/FDB réciproque si possible, sinon **un seul
    port** → ce port (`via='port_unique'`), **plusieurs ports** → menu de choix
    dans la modale (`b_port_options`, défaut = le plus petit, `via='port_a_
    choisir'`, `confiance='faible'`, décoché par défaut). **Plusieurs MAC** →
    `cascades`.
  - **`liens_baie`** : un port de switch A apprend la MAC d'infra d'un autre
    élément de baie B → lien A ⇄ B. Port de B : par **LLDP** (`diag_topologie`,
    `voisin_port`) sinon **FDB réciproque** (la mgmt MAC de A vue sur un port de
    B). `POST /api/baie/lien-port`.
  - **`cascades`** (info, `_classer_cascade`) : ≥ 2 MAC sur un port → équipement
    intermédiaire (switch non géré / borne Wi-Fi). Classification : appareil
    inventorié `Borne WiFi`/`Switch` vu = certain ; MAC **localement administrées**
    (bit U/L, `_mac_locale`) = clients Wi-Fi ; OUI AP / OUI « mobile » pondèrent
    Wi-Fi ; peu de MAC, aucune aléatoire = switch non géré ; sinon `indetermine`.
    Aussi en direct dans l'infobulle de la prise (ligne « En aval »).
  - **`hors_inventaire`** (info) : MAC seule sur un port, absente de l'inventaire,
    avec son fabricant (OUI) — à ajouter à l'inventaire. v2.19.28 : enrichi
    des **talkers de la dernière capture passive** (palier 2) absents de
    l'inventaire, d'aucune FDB relevée et non localement administrés
    (`via='capture'`, affiché « capture passive ») — utile là où aucun switch
    ne répond en SNMP, sur un VLAN sans SNMP ou derrière un switch non géré.
  - **Confiance faible** (v2.19.28, #19) : sur un switch à table MAC déformée,
    une MAC réparée qui matche 2 appareils de l'inventaire (mêmes 4 premiers
    octets) était jetée. `_fdb_corriger` la remonte maintenant dans
    `meta['ambigus']` ; si **un seul** de ses ports candidats est cartographié,
    `analyser_brassage_baie` en fait une proposition `prises_appareils` /
    `ports_appareils` marquée `confiance='faible'` (décochée par défaut, badge
    « ⚠ à vérifier »).
  - **`retypage`** (info, v2.19.26) : un voisin LLDP (`diag_topologie.voisin_caps`)
    se déclare `wlan` / `router` / `bridge` / `phone`, mais l'appareil
    correspondant en inventaire porte un `type_appareil` incompatible → on
    propose le type probable (`Borne Wi-Fi`, `Routeur/Pare-feu`, `Switch`,
    `Telephone IP`). Purement indicatif — aucune modification, lien vers la
    fiche appareil dans la modale.
- **Fiabilité de la table MAC d'un switch** (v2.19.23-24). Constaté sur un HP
  ProCurve J9450A (1810G-24) : l'agent renvoie dans `dot1dTpFdbAddress` la valeur
  `00:01` + les **4 premiers octets** de la vraie MAC (les 2 derniers perdus) —
  `_vendor()` sortait des fabricants au hasard.
  - **Sources** (`_fdb_switch`) : table bridge (`dot1q`/`dot1d`) **+ table ARP**
    (`ipNetToMediaPhysAddress`, `_OID_ARP_PHYS`, lue via `_snmp_walk_octets` qui
    préserve les octets bruts d'un OCTET STRING — `app._snmp_walk` les décode en
    UTF-8 et détruit une MAC). L'ARP est unie à la FDB → indispensable pour un
    routeur / switch L3 sans FDB.
  - **Hypothèses de forme** (`_fdb_corriger`, `_FDB_HYPOTHESES`) : `exact` /
    `tronque4` / `tronque5` / `prefixe2` (ProCurve) / `prefixe1`. Chacune est
    scorée par le nombre d'appareils de l'inventaire qu'elle fait reconnaître ;
    on garde la meilleure si ≥ 3 reconnus **et** nettement mieux que `exact`.
    Sous une hypothèse déformée, on ne garde que ce qu'on sait rattacher (le
    reste est perdu) ; une MAC réparée sur plusieurs ports (collision de préfixe)
    est écartée.
  - **Réglage manuel par switch** : config `diag_fdb_mode:<ip>` ∈ `auto` /
    `standard` (jamais transformer) / `prefixe` (forcer l'hypothèse ProCurve) /
    `ignorer` (FDB vide). Route `POST /api/baie/brassage/fdb-mode` (`can_write`).
  - **Indicateur** : `analyser_brassage_baie` renvoie `fdb: [{nom, ip, mode,
    transform, reconnues, total, fiable}]` → la modale affiche pour chaque switch
    l'hypothèse retenue, « N/M appareils reconnus » et le menu déroulant.
  - Appliqué dans `analyser_brassage_baie` **et** le cycle d'activité live
    (journal). Une table MAC normale n'est jamais modifiée.
- **Capacités SNMP (compteurs 64 bits, PoE) non condamnées sur un seul échec**
  (v2.19.13) : `_activite_hc[ip]` / `_activite_poe[ip]` ne passent à `False`
  qu'après `_ACTIVITE_NEG_CONFIRME` (2) relevés négatifs consécutifs — un paquet
  UDP perdu au premier passage (fréquent sur les agents bas de gamme) ne fige
  plus le mode dégradé pour la vie du process. Re-test périodique tous les
  `_ACTIVITE_REPROBE_CYCLES` (50) cycles même une fois « absente ».
- **Cadence adaptative** : `_activite_loop` espace ses cycles en proportion du
  relevé le plus lent (plafond 30 s) — inutile de re-solliciter un agent SNMP
  qui met 10 s à répondre toutes les 3 s. **Exceptions, cadence forcée courte** :
  les `_ACTIVITE_RECHAUFFE_CYCLES` (3) premiers cycles (v2.19.11 — il faut deux
  relevés pour un premier débit), **et tant qu'un assistant de calibration
  attend** (v2.19.13 — il compare `oper` entre deux relevés, 30 s d'écart le
  rendraient inutilisable). `_activite_rechauffe` repart de zéro dès qu'aucun
  navigateur ne bat. Le bandeau affiche « démarrage du relevé SNMP… » tant que
  `actif` vaut `null`.
- **Association port baie ↔ ifIndex** (v2.19.9) : `_mapping_baie_ifindex` —
  priorité **manuel** (`baie_slot_ports.if_index`, source `manuel`) > **topologie**
  (`diag_topologie`, source `topologie`) > **nom d'interface**
  (`_port_physique_depuis_nom` : `GigabitEthernet1/0/12` → 12, `Gi1/0/12` → 12,
  `Port: 12 …` → 12, `swp12` → 12 ; source `nom_port` ; **plage RJ 1-48
  uniquement** depuis v2.19.14 — un port SFP de baie 1001+ collisionnait avec le
  RJ du même rang) > **repli naïf** `numero == ifIndex` (**désactivé par
  défaut** — souvent faux ; `diag_baie_activite_repli_naif='1'` pour l'activer ;
  source `repli`). Le numéro de port de la baie n'est presque jamais l'ifIndex
  du switch : le repli naïf de v2.19.6-.8 allumait des LEDs sur les mauvais
  ports. Quand **topologie et nom d'interface s'accordent tous deux mais
  divergent** sur un port (v2.19.14), c'est signalé (`divergences`, marqueur ⚠
  dans le moniteur + journal). Bouton **« décaler tous les ports RJ de N »**
  (`calibrer_decalage_baie`, route `POST /api/baie/activite/calibrer/decalage`,
  `can_write`) pour le cas fréquent ifIndex = n° de port + constante.
- **Sémantique LED** (`_etat_led`, priorité décroissante) : `down` (éteinte) ·
  `stale` si ≥ 3 relevés manqués consécutifs (grise, v2.19.8 — un port ne
  s'éteint plus jamais brutalement à cause d'un seul paquet SNMP perdu) · `err`
  si Δ(in+out errors) > `diag_baie_activite_seuil_err` (rouge) · `sature` si
  débit ≥ `diag_snmp_seuil_saturation_pct` % de la vitesse du lien (orange) ·
  `traffic` si **paquets/s ≥ `diag_baie_activite_pps_mini`** (défaut **15**
  depuis v2.19.12) **OU** débit > `diag_baie_activite_bps_mini` (défaut **500**
  bit/s depuis v2.19.16, configurable) · `idle` (vert fixe, aucune activité).
  Les **paquets** comptés = unicast **+ broadcast/multicast** (v2.19.16,
  `ifInNUcastPkts`/`ifOutNUcastPkts`) — une vraie LED de switch clignote aussi
  sur les ARP/STP/mDNS, et un port de VLAN calme n'a souvent que ça. Le
  clignotement a une période ∝ paquets/s.
- **Anti-rebond ASYMÉTRIQUE** (v2.19.12, revu v2.19.16 :
  `_ACTIVITE_DEBOUNCE_ON = 1`, `_ACTIVITE_DEBOUNCE = 2`) : un port **passe en
  `traffic` dès le premier relevé** au-dessus du seuil (comme une vraie LED) et
  n'en **sort qu'après deux relevés calmes** (persistance visuelle sans
  scintillement). La décision se prend sur les valeurs **instantanées** ;
  les chiffres affichés restent lissés par EMA (α≈0,5). `down`/`stale`/`err`/
  `sature` basculent immédiatement (pas d'anti-rebond).
- **Aucune persistance** : `_activite_prev` / `_activite_resultat` /
  `_activite_detail` / `_activite_journal` vivent en mémoire, purgés 120 s après
  le dernier battement. Les tendances (`diag_metriques`) restent alimentées
  uniquement par le cycle de surveillance normal. Prérequis : `diag_snmp_actif`.

### Moniteur réseau de la baie (modale) — v2.19.8

Bouton **📊 Moniteur** dans la barre d'outils de `/baie` → modale à 3 onglets,
poll `GET /api/baie/activite/moniteur` toutes les 2 s (entretient aussi le
battement du collecteur d'activité) :

- **Ports** : par switch (IP, durée du dernier poll, communauté, compteurs
  64/32 bits, nb d'interfaces, `nb_ports_calibres/nb_ports_mappes`, budget PoE,
  avertissement « compteurs bloqués » le cas échéant) puis un **tableau de
  calibration** : pour chaque port de la baie (numéro + appareil branché), un
  menu déroulant listant toutes les interfaces du switch avec leur débit en
  direct → choix enregistré via `POST /api/baie/activite/calibrer` (`can_write`)
  dans `baie_slot_ports.if_index`. Colonne PoE (statut/classe/W) si le switch en
  fait. **Assistant « calibrer par débranchement »** (v2.19.10, bouton 🔌) : on
  débranche puis rebranche le câble du port, l'assistant surveille les
  transitions `oper` de toutes les interfaces sur une fenêtre de 90 s
  (`_CALIB_FENETRE`) et associe automatiquement celle qui a bougé —
  `POST /api/baie/activite/calibrer/assistant` (`can_write`), action
  `start`/`stop`. **Décision** (v2.19.13) : prise quand le réseau s'est calmé
  (un cycle sans nouvelle transition) sur l'interface débranchée **puis
  rebranchée en dernier** (≥ 2 transitions, état final up) → un voisin qui
  flappe pendant le geste ne gagne plus ; à défaut, à l'expiration de la
  fenêtre, la seule interface qui a bougé. L'écriture (`calibrer_port_baie`)
  est faite par le thread `_activite_loop`, jamais par le GET moniteur.
  Section repliable « toutes les interfaces » avec leur état/débit/pps
  (v2.19.30 : garde son état ouvert d'un rafraîchissement à l'autre). Rend
  transparent ce qui alimente les LEDs. v2.19.30 : la colonne **« Appareil
  branché »** affiche, à défaut d'affectation déclarée, l'appareil vu sur le
  port par la table MAC (`p.voisins`, suffixé « (vu) ») ; la colonne
  **« source »** est en clair (`nom d'interface` / `topologie L2` /
  `manuel (calibré)` / `repli`).
- **Journal** : flux d'évènements horodatés et dédoublonnés (`_activite_journal`,
  `collections.deque(maxlen=250)`, alimenté par `_journal()`) — switch injoignable
  ou rétabli, port devenu actif/calme, port passé « obsolète », lien coupé,
  compteur SNMP réinitialisé, vitesse de lien inconnue.
- **Trafic capturé** : capture à la demande (`capturer_trafic`, palier 2,
  thread démon `DiagCaptureBaie` via `lancer_capture_baie`/`statut_capture_baie`,
  durée `diag_baie_capture_duree_s`) — répartition broadcast/multicast/unicast,
  top 15 des MAC qui parlent le plus (fabricant + appareil résolus), anomalies
  rapides (tempête de broadcast, ARP gratuits, DHCP multiples). Affiche le
  `motif` d'indisponibilité (`scapy_absent`/`docker_bridge`/
  `privileges_insuffisants`) si la capture n'est pas utilisable — voir
  § Déploiement.

`GET /api/baie/activite/moniteur` est en lecture seule (accès simple suffit).
`POST /api/baie/activite/capture` exige `can_write` — comme
`/api/diag-reseau/snapshot` : lancer une capture consomme le réseau/système de
la machine hôte, ce n'est pas une simple consultation.

---

## Catégories d'évènements

| Catégorie | Libellé | Gravité | Palier | Méthode de détection | Remédiation (résumé) |
|-----------|---------|---------|--------|----------------------|----------------------|
| `conflit_ip` | Conflit d'adresse IP | critique | 1 | une IP portant ≥ 2 MAC dans la table ARP sur plusieurs relevés | repasser un poste en DHCP / IP hors plage dynamique |
| `arp_spoofing` | ARP spoofing / usurpation | critique | 1/2 | une MAC (hors passerelle) répondant pour ≥ 6 IP ; ou liaison IP↔MAC changeant en capture | isoler la machine ; DAI / DHCP snooping |
| `dhcp_pirate` | Serveur DHCP non autorisé | critique | 1/2 | ≥ 2 `server id` distincts, ou serveur hors liste blanche | débrancher/reconfigurer en AP ; DHCP snooping |
| `ra_pirate` | Router Advertisement IPv6 non autorisé | critique | 2 | ≥ 2 routeurs distincts émettant des RA | désactiver le RA parasite ; RA Guard |
| `mac_flapping` | MAC instable (flapping) | avertissement | 2 | même MAC vue avec ≥ 6 IP en capture | rétablir STP ; agréger (LACP) les liens redondants |
| `tempete_broadcast` | Tempête de broadcast | avertissement | 2 | pps broadcast > `diag_seuil_broadcast_pps` | chercher une boucle ; rétablir STP |
| `stp_instable` | Topologie STP instable | avertissement | 2 | ≥ 5 BPDU TCN captés | PortFast sur les accès ; câble/SFP douteux |
| `qualite_liaison` | Qualité de liaison dégradée | avertissement | 1 | perte ≥ `diag_seuil_perte_pct` ou gigue ≥ `diag_seuil_jitter_ms` | tester en filaire ; compteurs de port ; décharger le lien |
| `passerelle_injoignable` | Passerelle injoignable | critique | 1 | 0 réponse sur N pings vers la passerelle | vérifier alim/câble ; redémarrer la passerelle |
| `dns_degrade` | Résolution DNS dégradée | avertissement | 1 | échecs de résolution ou latence > 300 ms | DNS secondaire ; redémarrer le service |
| `conflit_nom` | Conflit de nom réseau | avertissement | 1 | ≥ 2 IP annonçant le même nom NetBIOS | renommer une machine |
| `tcp_retransmissions` | Retransmissions TCP élevées | info | 2 | ≥ 15 retransmissions sur un flux en capture | traiter la perte sous-jacente ; MTU |
| `duplex_mismatch` | Duplex mismatch (port) | critique | 3 | Δ `dot3StatsLateCollisions` > 0, ou `dot3StatsDuplexStatus == halfDuplex` sur port actif ≥ 100 Mb/s | autonégociation des DEUX côtés ; câble |
| `port_crc` | Erreurs CRC/FCS en hausse (port) | avertissement | 3 | Δ (`dot3StatsFCSErrors` + `dot3StatsAlignmentErrors`) ≥ `diag_snmp_seuil_erreurs` | remplacer câble/cordon/SFP ; changer de port |
| `port_erreurs` | Erreurs / rejets de paquets (port) | avertissement | 3 | Δ (`ifInErrors`+`ifOutErrors`) ou Δ discards ≥ seuil | augmenter la capacité ; QoS |
| `port_sature` | Lien saturé (port) | avertissement | 3 | débit moyen ≥ `diag_snmp_seuil_saturation_pct` % de la vitesse | planifier les gros transferts ; 10G / LACP |
| `port_flapping` | Port instable (flapping) | avertissement | 3 | `ifOperStatus` changé ≥ 3 fois sur les 4 derniers relevés | remplacer câble/SFP ; fiabiliser l'alim |
| `vitesse_reduite` | Vitesse de lien réduite (port) | info | 3 | port actif à 10/100 Mb/s sur un équipement gigabit | câble Cat5e+ 4 paires ; autonégociation |
| `cablage_incoherent` | Câblage baie incohérent avec la topologie | avertissement | 4 | le switch (FDB) voit un appareil connu, seul, ≠ de celui déclaré dans `baie_slot_ports` pour ce port | corriger la baie ; « Reporter dans la baie » |
| `degradation_relative` | Dégradation par rapport à la référence | avertissement | 5 | valeur courante ≥ p90 × `diag_baseline_facteur` **et** au-dessus d'un plancher absolu | traiter la cause vue sur la courbe |
| `wifi_signal_faible` | Signal Wi-Fi faible (poste) | avertissement | 7a | RSSI du poste ≤ `diag_wifi_seuil_rssi` (−72) | rapprocher/ajouter une borne ; filaire pour les fixes |
| `wifi_canal_sature` | Canal Wi-Fi encombré | avertissement | 7a | ≥ `diag_wifi_seuil_aps_canal` BSSID sur des canaux chevauchants | canaux 1/6/11 en 2.4 GHz ; privilégier le 5 GHz |
| `wifi_ap_suspect` | Point d'accès Wi-Fi suspect (evil twin) | critique | 7a | le SSID du parc (`parc_general.wifi_ssid`) diffusé par des fabricants (`_vendor`) différents | localiser/débrancher la borne pirate ; WPA2/3-Entreprise |
| `wifi_bande_2ghz` | Wi-Fi en 2.4 GHz alors que 5 GHz dispo | info | 7a | poste en 2.4 GHz + BSSID du même SSID en 5 GHz | band steering ; forcer le 5 GHz sur les fixes |
| `wifi_debit_faible` | Débit Wi-Fi négocié faible | info | 7a | débit < 25 % de la capacité radio | traiter signal/canal ; MàJ pilote ; borne/carte récente |
| `ups_sur_batterie` | Onduleur sur batterie (coupure secteur) | critique | 7b | `upsOutputSource == battery` ou `upsSecondsOnBattery > 0` | arrêter proprement avant l'épuisement ; vérifier le secteur |
| `ups_batterie_faible` | Onduleur — batterie faible / autonomie critique | critique | 7b | statut low/depleted, ou autonomie < `diag_ups_seuil_autonomie_min`, ou batterie < 30 % | remplacer le bloc batterie ; réduire la charge |
| `ups_surcharge` | Onduleur en surcharge | avertissement | 7b | charge de sortie > `diag_ups_seuil_charge_pct` (80 %) | débrancher le non-critique ; onduleur plus puissant |
| `ups_batterie_usee` | Onduleur — batterie à remplacer | avertissement | 7b | APC replace indicator, ou température > 40 °C | poser un bloc batterie neuf ; tester l'autonomie |
| `ups_alarme` | Onduleur — alarme active | avertissement | 7b | `upsAlarmsPresent > 0` | lire le code d'alarme sur l'onduleur ; ticket constructeur |
| `ups_secteur_instable` | Onduleur — tension d'entrée hors plage | info | 7b | tension d'entrée hors [195, 255] V (ou [95, 130]) | faire contrôler l'installation électrique ; onduleur online |

Le texte complet (cause / à vérifier / à corriger) est dans
`network_diag.py:_REMEDIATION`, affiché sous chaque évènement (« 💡 Comment
corriger ? »), dans le rapport et dans l'e-mail d'alerte.

---

## Clés de configuration (`config_helpers.py`, section diag)

| clé | défaut | effet |
|-----|--------|-------|
| `diag_surveillance_active` | `0` | active le thread de surveillance continue |
| `diag_intervalle_s` | `300` | période du moniteur continu |
| `diag_snapshot_duree_s` | `20` | fenêtre d'un snapshot / d'une capture |
| `diag_snapshot_rapide` | `0` | mode court : ping n=8, 1 relevé ARP, pas de capture |
| `diag_snapshot_budget_s` | `120` | plafond souple ; au-delà, SNMP/topologie/capture sont sautées |
| `diag_capture_active` | `0` | palier 2 (capture passive scapy) |
| `diag_seuil_broadcast_pps` | `150` | seuil tempête de broadcast |
| `diag_seuil_perte_pct` | `5` | seuil de perte de paquets (%) |
| `diag_seuil_jitter_ms` | `30` | seuil de gigue (ms) |
| `diag_dhcp_serveurs_attendus` | `` | liste blanche IP serveurs DHCP (CSV) |
| `diag_cibles_ping` | `` | cibles de test de liaison ; vide = passerelle + DNS du parc |
| `diag_reseau_max_jours` | `30` | rétention des évènements résolus, runs, relevés SNMP, métriques (0 = off) |
| `diag_alerte_email` | `0` | e-mail sur nouvel évènement critique |
| `diag_alerte_destinataire` | `` | destinataire des alertes et du rapport périodique |
| `diag_snmp_actif` | `0` | palier 3 (interrogation SNMP) |
| `diag_snmp_communautes` | `public` | communautés v1/v2c, CSV, essayées dans l'ordre |
| `diag_snmp_v3_user` | `` | utilisateur SNMPv3 (USM) — vide = v3 désactivé ; essayé avant les communautés |
| `diag_snmp_v3_auth_proto` | `SHA` | protocole d'auth v3 : `MD5` / `SHA` (SHA-1) / `SHA224` / `SHA256` / `SHA384` / `SHA512` |
| `diag_snmp_v3_auth_pass` | `` | mot de passe d'authentification v3 (authNoPriv — pas de chiffrement) |
| `diag_snmp_seuil_erreurs` | `50` | Δ erreurs/discards/CRC par relevé avant alerte |
| `diag_snmp_seuil_saturation_pct` | `90` | seuil de saturation de lien (%) |
| `diag_topologie_active` | `0` | palier 4 (topologie L2, nécessite le SNMP) |
| `diag_baseline_active` | `1` | palier 5 (alerte sur dégradation relative) |
| `diag_baseline_jours` | `7` | fenêtre d'historique pour la référence |
| `diag_baseline_facteur` | `2.5` | multiple de la p90 déclenchant l'alerte |
| `diag_rapport_cron` | `` | envoi périodique du rapport (`HH:MM` quotidien ou `lun HH:MM` hebdo) ; redémarrage requis |
| `diag_wifi_active` | `1` | palier 7a (diagnostic Wi-Fi côté poste) — sans effet sans adaptateur |
| `diag_wifi_seuil_rssi` | `-72` | RSSI (dBm) sous lequel on alerte |
| `diag_wifi_seuil_aps_canal` | `4` | nb de BSSID sur des canaux chevauchants avant « canal encombré » |
| `diag_ups_active` | `1` | palier 7b (supervision SNMP des onduleurs) — nécessite `diag_snmp_actif` |
| `diag_ups_seuil_charge_pct` | `80` | charge de sortie au-delà de laquelle on alerte |
| `diag_ups_seuil_autonomie_min` | `10` | autonomie estimée mini (min) avant alerte |
| `diag_baie_activite_seuil_err` | `20` | vue d'activité baie : Δ erreurs (in+out) par fenêtre avant LED rouge (nécessite `diag_snmp_actif`) |
| `diag_baie_activite_pps_mini` | `15` | vue d'activité baie : paquets/s (unicast + broadcast/multicast) sous lesquels un port up reste « calme » |
| `diag_baie_activite_bps_mini` | `500` | vue d'activité baie : débit (bit/s) sous lequel un port up reste « calme » (l'autre voie). Baisser pour clignoter comme un vrai switch |
| `diag_baie_capture_duree_s` | `20` | moniteur baie : durée de la capture de trafic à la demande |
| `diag_baie_activite_repli_naif` | `0` | vue d'activité baie : dernier recours `numero de port == ifIndex` (souvent faux — désactivé) |

SMTP : réutilise `smtp_server` / `smtp_port` / `smtp_login` / `smtp_password` /
`from_email` (Réglages → e-mail).

---

## OIDs SNMP utilisés (tous en lecture seule)

`network_diag.py` — `_OID_*` :

```
# ifTable (1.3.6.1.2.1.2.2.1.x)
ifDescr .2   ifType .3   ifSpeed .5   ifAdminStatus .7   ifOperStatus .8
ifInOctets .10   ifInDiscards .13   ifInErrors .14
ifOutOctets .16  ifOutDiscards .19   ifOutErrors .20
# ifXTable (1.3.6.1.2.1.31.1.1.1.x)
ifName .1   ifHCInOctets .6   ifHCOutOctets .10   ifHighSpeed .15   ifAlias .18
# EtherLike-MIB dot3StatsTable (1.3.6.1.2.1.10.7.2.1.x)
dot3StatsAlignmentErrors .2   dot3StatsFCSErrors .3
dot3StatsLateCollisions .11   dot3StatsExcessiveCollisions .12
dot3StatsDuplexStatus .19   (1 unknown | 2 halfDuplex | 3 fullDuplex)
# BRIDGE-MIB / Q-BRIDGE-MIB
dot1dTpFdbPort        1.3.6.1.2.1.17.4.3.1.2
dot1dBasePortIfIndex  1.3.6.1.2.1.17.1.4.1.2
dot1qTpFdbPort        1.3.6.1.2.1.17.7.1.2.2.1.2   (index = <fdbId>.<6 octets de MAC> : le VLAN est gratuit)
dot1qPvid             1.3.6.1.2.1.17.7.1.4.5.1.1   (VLAN d'accès par dot1dBasePort)
# STP (BRIDGE-MIB) — donne le SENS des liens, même sans LLDP
dot1dStpRootPort             1.3.6.1.2.1.17.2.7.0
dot1dStpDesignatedRoot       1.3.6.1.2.1.17.2.5.0
dot1dStpPortState            1.3.6.1.2.1.17.2.15.1.3
dot1dStpPortDesignatedBridge 1.3.6.1.2.1.17.2.15.1.8
# ENTITY-MIB — modèle / n° de série / composition d'un stack (base 1.3.6.1.2.1.47.1.1.1.1)
entPhysicalDescr .2   entPhysicalClass .5   entPhysicalName .7
entPhysicalSerialNum .11   entPhysicalModelName .13
# IP-MIB — sous-réseaux portés / routés (proposés comme plages de scan)
ipAdEntIfIndex 1.3.6.1.2.1.4.20.1.2   ipAdEntNetMask 1.3.6.1.2.1.4.20.1.3
ipCidrRouteDest 1.3.6.1.2.1.4.24.4.1.1
# LLDP-MIB
lldpRemSysName  1.0.8802.1.1.2.1.4.1.1.9
lldpRemPortId   1.0.8802.1.1.2.1.4.1.1.7
lldpRemManAddrIfId 1.0.8802.1.1.2.1.4.2.1.2   (l'INDEX porte l'IP de gestion du voisin)
# UPS-MIB (RFC 1628, 1.3.6.1.2.1.33.1.x) — onduleurs
upsIdentModel .1.1.2   upsBatteryStatus .1.2.1   upsSecondsOnBattery .1.2.2
upsEstimatedMinutesRemaining .1.2.3   upsEstimatedChargeRemaining .1.2.4
upsBatteryTemperature .1.2.7   upsInputVoltage .1.3.3.1.3   upsOutputSource .1.4.1
upsOutputPercentLoad .1.4.4.1.5   upsAlarmsPresent .1.6.1
# APC PowerNet (repli) 1.3.6.1.4.1.318.1.1.1.2.2.x
upsAdvBatteryReplaceIndicator .4   upsAdvBatteryRunTimeRemaining .3
# scalaires (scan + test SNMP)
sysDescr 1.3.6.1.2.1.1.1.0   sysName 1.3.6.1.2.1.1.5.0
```

Le Wi-Fi (palier 7a) n'utilise **pas** SNMP : lecture côté poste via
`netsh wlan show interfaces` / `… networks mode=bssid` (Windows),
`iw dev … link` / `… scan` (Linux), `system_profiler -json SPAirPortDataType`
(macOS, best-effort). Aucune dépendance.

GET, GET typé et GETNEXT (walk) sont faits main (encodage BER dans `app.py`,
`_snmp_get` / `_snmp_get_typed` / `_snmp_walk` / `_ber_decoder_*`).
`_snmp_get` ne renvoie que les OCTET STRING (sysDescr/sysName) ; **utiliser
`_snmp_get_typed`** dès qu'un scalaire entier est attendu (UPS-MIB, etc.).
**Aucune dépendance** (pas de pysnmp).

### SNMPv3 (USM, authNoPriv — v2.19.29)

Constat d'audit #7 : les pare-feux, box de FAI récentes et caméras managées
sont souvent en v3 uniquement, et un échec SNMP ne disait pas s'il s'agissait
d'un refus ou d'un appareil qui ne fait pas de SNMP.

- **`_v3_discover(ip)`** : découverte d'engine SNMPv3, **sans authentification**
  — tout agent SNMP (même v3-only, même mal configuré en v1/v2c) y répond.
  C'est LA sonde universelle « y a-t-il un agent SNMP ici ? ». Cache 300 s
  (positif) / 90 s (négatif).
- **authNoPriv** (MD5 / SHA-1 / SHA-224/256/384/512, **pas de priv**) : quand
  `diag_snmp_v3_user` + `diag_snmp_v3_auth_pass` sont renseignés,
  `_snmp_get_typed` et `_snmp_walk` tentent une requête v3 **en premier**, avec
  repli automatique sur les communautés v1/v2c (jamais de régression pour un
  agent réellement v2c). `_v3_ku`/`_v3_kul` (RFC 3414 §2.6, vecteurs A.3
  vérifiés), HMAC tronqué inséré dans `msgAuthenticationParameters`
  (`_v3_signer`), resynchronisation `notInTimeWindow`/`unknownEngineID`
  (une seule nouvelle tentative). Crypto = `hashlib` + `hmac` (stdlib).
- **Le scan réseau reste en v1/v2c pur** (`_scan_host` passe `_essai_v3=False`)
  : une découverte d'engine par hôte non-SNMP ajouterait ~1,5 s à chaque
  adresse d'un /24.
- **`_snmp_presence(ip, communautes) → (present, exploitable, detail)`** :
  distingue « agent SNMP présent mais refusé » (mauvaise communauté, ACL,
  v3 exigé) de « pas de matériel SNMP ». Utilisé par le bouton **« Tester
  SNMP »** (`api_diag_test_snmp`) et par **« Deviner le brassage »**
  (`analyser_brassage_baie` → `switchs_snmp_refuse`, bannière 🔒 dans la modale).

---

## Tables de base de données (`app.py:init_db()`)

| Table | Rôle | Rétention |
|-------|------|-----------|
| `diag_reseau_evenements` | évènements dédoublonnés par `signature` (`nb_occurrences`, `resolu`, `appareil_id`) | `diag_reseau_max_jours` (résolus uniquement) |
| `diag_reseau_runs` | un enregistrement par snapshot (durée, `resume_json` avec les temps de phase) | `diag_reseau_max_jours` |
| `diag_snmp_releves` | compteurs par port horodatés (delta) | `diag_reseau_max_jours` |
| `diag_topologie` | instantané FDB/LLDP/STP par équipement (+ `nb_macs_port`, `est_uplink`, `vlan`, `stp_etat`, `stp_amont`, `voisin_ip`, `voisin_modele`) | remplacée à chaque poll |
| `diag_topologie_mouvements` | transitions d'un relevé à l'autre : appareil apparu / disparu / déplacé de port | `diag_reseau_max_jours` |
| `diag_metriques` | série temporelle (liaison, débit/erreurs port) pour la baseline | `diag_reseau_max_jours` |

---

## Déploiement

- **Paliers 1, 3, 4, 5, 6** : rien à installer. Le SNMP suppose seulement que
  les équipements réseau acceptent une communauté en lecture.
- **Palier 2 (capture passive)** :
  - Windows : installer **Npcap** ; lancer ParcInfo en administrateur.
  - Linux/macOS natif : lancer en root (ou capabilities `CAP_NET_RAW`).
  - **Docker** : décommenter dans `docker-compose.yml` `network_mode: host` +
    `cap_add: [NET_RAW, NET_ADMIN]` (incompatible avec `ports:`). En bridge, le
    palier 2 renvoie l'état `docker_bridge` et ne fait rien.
- **Visibilité** : les évènements actifs remontent dans le widget « Alertes
  critiques » du tableau de bord et sur `/m/diag-reseau` (mobile, lecture seule).

---

## Points d'entrée code

| Besoin | Repère |
|--------|--------|
| Détections palier 1/2 | `network_diag.py` : `detecter_*`, `mesurer_qualite_liaison`, `capture_passive` |
| SNMP (palier 3) | `interroger_equipement`, `_analyser_snmp`, `interroger_equipements_client`, `etat_snmp` |
| Topologie (palier 4) | `decouvrir_topologie` (récursive, parallèle, sous budget), `_topologie_equipement`, `_journaliser_mouvements`, `_stp_switch`, `_entite_physique`, `_sous_reseaux_equipement`, `etat_topologie`, `proposer_topologie_baie` / `appliquer_topologie_baie`, `lancer_cartographie` / `statut_cartographie` |
| Sous-réseaux supplémentaires (page Scan réseau) | `network_diag.sous_reseaux_detectes` (sonde `_snmp_presence` + `_sous_reseaux_equipement` par équipement, agrégé), route `GET /api/scan/sous-reseaux`, `templates/scan_reseau.html` (`chargerSousReseaux`/`ajouterSousReseau`) ; `app._parse_cidrs` / `network_diag._parse_plages` (plage_ip_locale multi-valeur) ; `app._appareil_sur_reseau_courant` (confiance étendue à tout le site une fois une plage confirmée) |
| MAC via SNMP/capture sur un sous-réseau routé (Scan réseau) | `network_diag.hotes_vus_snmp` (table ARP des routeurs/switchs SNMP, `_ip_depuis_suffixe_arp`) et `network_diag.capture_arp_sightings` (écoute ARP passive, scapy) ; `app._scan_host(..., snmp_arp_par_ip=, capture_arp_par_ip=)` en repli sur `_mac_from_arp` uniquement ; `app._run_scan` lance les 2 relevés en parallèle ; badges 🌐/📡 dans `templates/scan_reseau.html` |
| Baseline (palier 5) | `enregistrer_metriques_liaison`, `evaluer_baseline`, `serie_metrique` |
| Rapport / remédiation (palier 6) | `_REMEDIATION`, `remediation`, `generer_rapport_diag`, `tache_rapport_planifie` |
| Vue d'activité baie (LEDs live) | `network_diag.py` : `_activite_loop` (cadence adaptative), `_cycle_activite` (relevé SNMP mutualisé par IP → switch multi-slots), `_noms_interfaces`/`_maj_noms_interfaces` (async), `_poll_switch_ports` (+ `sysUpTime` pour un Δt exact), `_poll_poe` (POWER-ETHERNET-MIB), `_lire_sysinfo` (modèle/uptime), `_mapping_baie_ifindex` (nom_port RJ seulement, `divergences`) + `_port_physique_depuis_nom`, `_etat_led` (plafond débit/pps, bouclage 32 bits, `reboot`), `activite_baie`, `calibrer_port_baie` / `calibrer_decalage_baie`, `_prises_murales_activite` (v2.19.18 : LED des prises murales d'un bandeau RJ via le port de switch au bout du cordon `lie_slot_id`/`lie_port_numero`), `_fdb_switch` + `_voisins_port` (v2.19.19 : FDB bridge-MIB « live » → contrôle de câblage MAC déclarée ↔ MAC apprise, repli `diag_topologie` ; + appareils vus par port dans les infobulles), `analyser_brassage_baie` / `_elements_baie` / `_classer_cascade` / `_fdb_corriger` (v2.19.22-23 : bouton « 🧠 Deviner le brassage » → carte réseau proposée, route `GET /api/baie/brassage/proposer` lecture seule ; 4 groupes de propositions + cascades + hors inventaire ; répare une FDB tronquée par un agent SNMP buggé) ; état : `_activite_sut` (Δt), `_activite_hist` (sparkline), `_activite_sysinfo`, `_activite_fdb`/`_activite_fdb_baseport`/`_activite_fdb_dialecte`/`_activite_fdb_echec`, `_activite_echecs`, `_activite_thread_lock` ; `app.py` : `_snmp_bulk_cols` (GETBULK), routes `GET /api/baie/activite` + `POST /api/baie/activite/calibrer{,/decalage}` ; `baie_brassage.html` (`#sel-activite`, `.prise-murale.pm-act-*`, badge ⚠ `.pm-cable-ko`) ; widget `network-activity` ; colonne `baie_slot_ports.if_index` |
| Moniteur réseau de la baie (modale) | `network_diag.py` : `moniteur_baie` (GET, lecture seule), `_journal`/`_activite_journal` (conditions permanentes émises une seule fois), `_activite_detail`, `assistant_calibration`/`_maj_assistant_calibration` (calibrer par débranchement — décision quand le réseau se calme, écriture par `_activite_loop`), `capturer_trafic`, `lancer_capture_baie`/`statut_capture_baie` ; `_activite_calib` + `_noms_interfaces` (flag `maj_en_cours`) sous `_activite_lock` ; routes `GET /api/baie/activite/moniteur` + `POST /api/baie/activite/capture` + `POST /api/baie/activite/calibrer/assistant` ; `baie_brassage.html` (`#moniteur-modal`, `MoniteurModal` : en-tête modèle/uptime, colonne « tendance » (`sparkline`), « décaler de N », marqueur divergence, gel du re-render pendant qu'un `<select>` est manipulé, poll suspendu sur `document.hidden`) |
| Orchestration | `_run_snapshot` (snapshot), `_moniteur_loop` / `_moniteur_cycle` (continu), `_enregistrer_evenements`, `_purger_anciens` |
| Walk SNMP + BER | `app.py` : `_snmp_get`, `_snmp_get_typed`, `_snmp_walk` (GETNEXT), `_snmp_bulk_cols` (GETBULK v2c multi-colonnes, auto-descriptif), `_ber_decoder_oid`, `_ber_decoder_valeur` — tous vérifient le request-id de la réponse |
| Routes | `app.py` : `grep "@app.route('/api/diag-reseau"` + `/diag-reseau` + `/diag-reseau/rapport.{pdf,html}` |
