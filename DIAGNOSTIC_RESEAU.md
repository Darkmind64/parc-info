# Diagnostic réseau — référence

Module `network_diag.py` + routes `/api/diag-reseau/*` dans `app.py`. Page
**Inventaire → Diagnostic réseau** (`/diag-reseau`). Analyse la santé du réseau
du **client actif** ; les évènements sont rattachés à ce client.

> Ce document décrit le comportement au 2026-09-01 (v2.19.8). En cas de doute
> sur une valeur par défaut, vérifier `config_helpers.py:CFG_DEFAULTS` et
> `network_diag.py`.

---

## Les six paliers

| # | Nom | Ce qu'il observe | Prérequis | Limites |
|---|-----|------------------|-----------|---------|
| 1 | **Diagnostic actif** | Tables ARP de l'OS, ping en rafale (perte/latence/gigue), DHCPDISCOVER, requêtes NBNS/DNS | Aucun | Le DHCP pirate est best-effort (port 68 souvent occupé) |
| 2 | **Capture passive** | Trames vues sur l'interface (ARP, DHCP, STP, RA IPv6, en-têtes TCP) | `scapy` + privilèges + Npcap (Windows) ; en conteneur `network_mode: host` + `cap_add: [NET_RAW, NET_ADMIN]` | Ne voit que le trafic qui atteint la sonde (pas au-delà du switch) |
| 3 | **Interrogation SNMP** | Compteurs par port des switchs/routeurs/NAS (erreurs, duplex, débit, état) | SNMP v1/v2c en lecture seule sur les équipements ; `diag_snmp_actif` | Deux relevés successifs nécessaires pour un delta |
| 4 | **Topologie L2** | Tables MAC (bridge-MIB FDB) + LLDP → quel appareil sur quel port | Palier 3 actif + `diag_topologie_active` | FDB volatile ; VLAN non pris en compte finement |
| 5 | **Tendances & baseline** | Historique des métriques (liaison, ports) → dégradation *relative* | `diag_baseline_active` (défaut on) ; ~8 points d'historique | Ne remplace pas les seuils absolus, les complète |
| 6 | **Rapport & remédiation** | — (synthèse) | reportlab pour le PDF (repli HTML sinon) | — |
| 7a | **Wi-Fi (poste)** | État Wi-Fi du poste ParcInfo + AP visibles (`netsh wlan` / `iw` / `system_profiler`) | `diag_wifi_active` (défaut on) ; un adaptateur Wi-Fi | Vision depuis un seul point ; scan macOS best-effort |
| 7b | **Onduleurs SNMP** | UPS-MIB (source secteur/batterie, charge, autonomie, batterie, alarmes) + repli APC | Palier 3 actif + `diag_ups_active` ; appareil de type `Onduleur / UPS` avec IP | Dépend de ce que la carte réseau de l'onduleur expose |

**Deux modes** : *snapshot* à la demande (bouton « Lancer un diagnostic », mode
rapide possible) et *surveillance continue* (`diag_surveillance_active`, thread
démon `_moniteur_loop` calqué sur le watchdog ping, période `diag_intervalle_s`).

---

## Vue d'activité de la baie (LEDs live) — v2.19.6, revue v2.19.8

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
- **Relevé SNMP CIBLÉ** (v2.19.8) : `_iftable_switch` walk une fois par switch
  (caché 60 s) l'ensemble des ifIndex ethernet (`ifType==6`) ; `_poll_ports_cibles`
  fait ensuite un **GET groupé** (`_snmp_get_typed`, ~6-8 ifIndex/paquet) sur les
  seuls ports mappés — `ifOperStatus`, `ifHighSpeed`/`ifSpeed`, `ifHCInOctets`/
  `ifHCOutOctets` (repli 32 bits), `ifHCInUcastPkts`/`ifHCOutUcastPkts` (repli
  32 bits), `ifInErrors`/`ifOutErrors`. Remplace l'ancien walk de table entière
  (~300 paquets/cycle pour un switch 48 ports → ~2 paquets/switch/cycle) : bien
  moins sensible à la perte d'un paquet UDP isolé.
- **Association port baie ↔ ifIndex** : `_mapping_baie_ifindex` — d'abord la
  topologie (`diag_topologie` : l'appareil branché est vu par le switch sur un
  ifIndex précis → mapping fiable, `calibre=True`, source `topologie`), sinon
  repli naïf `numero == ifIndex` (RJ, source `repli_rj`) / `numero-1000` (SFP) /
  `numero-2000` (WAN, source `repli_sfp`). Le bandeau affiche « non calibré »
  quand le repli sert.
- **Sémantique LED** (`_etat_led`, priorité décroissante) : `down` (éteinte) ·
  `stale` si ≥ 3 relevés manqués consécutifs (grise, v2.19.8 — un port ne
  s'éteint plus jamais brutalement à cause d'un seul paquet SNMP perdu) · `err`
  si Δ(in+out errors) > `diag_baie_activite_seuil_err` (rouge) · `sature` si
  débit ≥ `diag_snmp_seuil_saturation_pct` % de la vitesse du lien (orange) ·
  `traffic` si **paquets/s ≥ `diag_baie_activite_pps_mini`** OU débit > 2 kbit/s
  (vert, période de clignotement ∝ paquets/s — v2.19.8 : clignote sur les
  PAQUETS, pas seulement au-delà d'un % de bande passante, sinon un poste au
  trafic bureautique normal restait classé « calme ») · `idle` (vert fixe,
  aucune activité). Lissage EMA (α≈0,5) sur débit/paquets pour absorber la
  variance d'un relevé à l'autre.
- **Aucune persistance** : `_activite_prev` / `_activite_resultat` /
  `_activite_detail` / `_activite_journal` vivent en mémoire, purgés 120 s après
  le dernier battement. Les tendances (`diag_metriques`) restent alimentées
  uniquement par le cycle de surveillance normal. Prérequis : `diag_snmp_actif`.

### Moniteur réseau de la baie (modale) — v2.19.8

Bouton **📊 Moniteur** dans la barre d'outils de `/baie` → modale à 3 onglets,
poll `GET /api/baie/activite/moniteur` toutes les 2 s (entretient aussi le
battement du collecteur d'activité) :

- **Ports** : par switch (IP, durée du dernier poll, communauté utilisée,
  compteurs 64/32 bits, nb d'ifIndex ethernet, nb de relevés manqués) puis par
  port (interface, état, débit, paquets/s, % du lien, compteurs bruts in/out,
  source du mapping). Rend transparent ce qui alimente les LEDs.
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
| `diag_baie_activite_pps_mini` | `1` | vue d'activité baie : paquets/s sous lesquels un port up reste « calme » (pas de clignotement) |
| `diag_baie_capture_duree_s` | `20` | moniteur baie : durée de la capture de trafic à la demande |

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
dot1qTpFdbPort        1.3.6.1.2.1.17.7.1.2.2.1.2
# LLDP-MIB
lldpRemSysName  1.0.8802.1.1.2.1.4.1.1.9
lldpRemPortId   1.0.8802.1.1.2.1.4.1.1.7
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

---

## Tables de base de données (`app.py:init_db()`)

| Table | Rôle | Rétention |
|-------|------|-----------|
| `diag_reseau_evenements` | évènements dédoublonnés par `signature` (`nb_occurrences`, `resolu`, `appareil_id`) | `diag_reseau_max_jours` (résolus uniquement) |
| `diag_reseau_runs` | un enregistrement par snapshot (durée, `resume_json` avec les temps de phase) | `diag_reseau_max_jours` |
| `diag_snmp_releves` | compteurs par port horodatés (delta) | `diag_reseau_max_jours` |
| `diag_topologie` | instantané FDB/LLDP par équipement | remplacée à chaque poll |
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
| Topologie (palier 4) | `decouvrir_topologie`, `_topologie_equipement`, `etat_topologie`, `appliquer_topologie_baie` |
| Baseline (palier 5) | `enregistrer_metriques_liaison`, `evaluer_baseline`, `serie_metrique` |
| Rapport / remédiation (palier 6) | `_REMEDIATION`, `remediation`, `generer_rapport_diag`, `tache_rapport_planifie` |
| Vue d'activité baie (LEDs live) | `network_diag.py` : `_activite_loop`, `_cycle_activite`, `_iftable_switch`, `_poll_ports_cibles`, `_mapping_baie_ifindex`, `_etat_led`, `activite_baie` ; route `GET /api/baie/activite` ; `baie_brassage.html` (`#sel-activite`) ; widget `network-activity` (`client_dashboard.html`) |
| Moniteur réseau de la baie (modale) | `network_diag.py` : `moniteur_baie`, `_journal`/`_activite_journal`, `_activite_detail`, `capturer_trafic`, `lancer_capture_baie`/`statut_capture_baie` ; routes `GET /api/baie/activite/moniteur` + `POST /api/baie/activite/capture` ; `baie_brassage.html` (`#moniteur-modal`, `MoniteurModal`) |
| Orchestration | `_run_snapshot` (snapshot), `_moniteur_loop` / `_moniteur_cycle` (continu), `_enregistrer_evenements`, `_purger_anciens` |
| Walk SNMP + BER | `app.py` : `_snmp_get`, `_snmp_walk`, `_ber_decoder_oid`, `_ber_decoder_valeur` |
| Routes | `app.py` : `grep "@app.route('/api/diag-reseau"` + `/diag-reseau` + `/diag-reseau/rapport.{pdf,html}` |
