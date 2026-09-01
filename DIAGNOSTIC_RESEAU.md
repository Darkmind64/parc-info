# Diagnostic réseau — référence

Module `network_diag.py` + routes `/api/diag-reseau/*` dans `app.py`. Page
**Inventaire → Diagnostic réseau** (`/diag-reseau`). Analyse la santé du réseau
du **client actif** ; les évènements sont rattachés à ce client.

> Ce document décrit le comportement au 2026-09-01 (v2.19.3). En cas de doute
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

**Deux modes** : *snapshot* à la demande (bouton « Lancer un diagnostic », mode
rapide possible) et *surveillance continue* (`diag_surveillance_active`, thread
démon `_moniteur_loop` calqué sur le watchdog ping, période `diag_intervalle_s`).

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
# scalaires (scan + test SNMP)
sysDescr 1.3.6.1.2.1.1.1.0   sysName 1.3.6.1.2.1.1.5.0
```

GET et GETNEXT (walk) sont faits main (encodage BER dans `app.py`,
`_snmp_get` / `_snmp_walk` / `_ber_decoder_*`). **Aucune dépendance** (pas de
pysnmp).

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
| Orchestration | `_run_snapshot` (snapshot), `_moniteur_loop` / `_moniteur_cycle` (continu), `_enregistrer_evenements`, `_purger_anciens` |
| Walk SNMP + BER | `app.py` : `_snmp_get`, `_snmp_walk`, `_ber_decoder_oid`, `_ber_decoder_valeur` |
| Routes | `app.py` : `grep "@app.route('/api/diag-reseau"` + `/diag-reseau` + `/diag-reseau/rapport.{pdf,html}` |
