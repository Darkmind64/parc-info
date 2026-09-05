# CHANGELOG - ParcInfo

## [2.19.43] - 2026-09-05 🧪

### 🧪 Audit réseau, lot 3 : couverture de tests + 2 bugs réels découverts

Suite des lots 1 et 2. Cinq zones du réseau qui n'avaient jusqu'ici aucun test reçoivent une couverture dédiée — et en les écrivant, deux bugs réels et bien réels sont apparus, tous deux corrigés :

- **Un détecteur DHCP actif n'a en réalité jamais envoyé le moindre paquet, depuis un temps indéterminé.** `detecter_dhcp_pirate` appelle `_mac_locale()` pour obtenir l'adresse MAC de ce poste — mais une fonction totalement différente, portant le même nom (`_mac_locale(mac)`, un test « MAC localement administrée ? » sans rapport), était définie plus bas dans `network_diag.py` et écrasait purement et simplement la première au chargement du module. L'appel levait donc systématiquement une erreur, absorbée en silence par la gestion d'erreurs de la fonction. Renommée en `_mac_locale_poste()` pour lever l'ambiguïté.
- **Le paquet DHCPDISCOVER envoyé par ce même détecteur était 4 octets trop court** par rapport au format DHCP standard (le bloc `ciaddr/yiaddr/siaddr/giaddr` ne réservait que 12 octets au lieu de 16), décalant tout ce qui suit dans le paquet. Corrigé.

Nouvelle couverture :
- La capture réseau passive (`capture_passive`, palier 2) — jusqu'ici zéro test alors qu'elle alimente à elle seule six catégories de détection (ARP spoofing, tempête broadcast, MAC flapping, DHCP pirate, Router Advertisement pirate, instabilité STP, retransmissions TCP) — est désormais vérifiée par une suite dédiée avec un faux module de capture, sans dépendance à scapy ni au réseau réel.
- Le calcul du sens des liens réseau par protocole STP (racine, port racine, pont amont) et la découverte récursive de topologie avec son plafond de profondeur sont désormais testés.
- Le diagnostic Wi-Fi sous Linux (`iw`) et macOS (`system_profiler`) est désormais testé — seul Windows l'était jusqu'ici.
- SNMPv3 est désormais vérifié de bout en bout avec les 5 protocoles d'authentification documentés (MD5, SHA-224, SHA-256, SHA-384, SHA-512), pas seulement SHA-1.
- Le détecteur de conflit de noms NetBIOS (`detecter_conflits_noms`) est désormais testé.

## [2.19.42] - 2026-09-05 🖱️

### 🖱️ Audit réseau, lot 2 : cohérence et performance du frontend

Suite du lot 1 (network_diag.py / app.py), 4 correctifs sur les pages Scan réseau, Diagnostic réseau et Baie de brassage :

- Le tri par « Statut » du tableau de résultats du scan réseau ne triait rien (aucun champ `statut` n'existe sur un résultat, le badge Nouveau/Inventaire est calculé à l'affichage) tout en affichant quand même la flèche de tri comme si ça avait fonctionné. Le tri calcule désormais ce même badge pour trier réellement.
- Les tableaux de résultats du scan réseau (sondage toutes les 800 ms) et de la liste de constats du diagnostic réseau (900 ms) étaient entièrement reconstruits — tri, filtre, remplacement complet du DOM — à chaque tick, même quand rien n'avait changé, jusqu'à plus d'une fois par seconde sur un scan complet. Ils ne se reconstruisent désormais que quand le nombre de résultats a réellement évolué, ou à la toute fin.
- Un menu déroulant de la baie de brassage (mode de lecture de la table MAC, dans l'avertissement de fiabilité de « Deviner le brassage ») n'avait ni classe ni largeur définie, contrairement à un menu quasi identique juste à côté — il pouvait s'étirer et chevaucher son étiquette.
- Un nom de fabricant/modèle détecté via WMI/UPnP peut faire 40 à 60 caractères ; la cellule correspondante du scan réseau n'était pas tronquée et élargissait toute la colonne. Troncature ajoutée avec la valeur complète en infobulle ; l'adresse MAC du tableau « hors inventaire » de la baie reçoit elle aussi une infobulle, comme les autres colonnes tronquables du même tableau.

## [2.19.41] - 2026-09-05 🔍

### 🔍 Audit réseau, lot 1 : 12 correctifs de fiabilité et performance

Audit complet des fonctionnalités réseau demandé par l'utilisateur (cohérence, bugs, ralentissements, blocages), mené par 4 relectures indépendantes de `network_diag.py`, des primitives SNMP/scan de `app.py`, du frontend et de la couverture de tests. Douze constats corrigés dans ce lot :

- **Un rebouclage normal de compteur SNMP 32 bits n'est plus pris pour un redémarrage de switch.** Sur un lien chargé, un compteur boucle en quelques dizaines de secondes ; `_analyser_snmp` utilisait auparavant la valeur brute entière du compteur comme delta, générant des débits en térabits fantômes et de fausses alertes de saturation/erreurs. Réutilise désormais la même logique déjà écrite et testée pour les LEDs de la baie.
- **Le badge « vu via SNMP/capture » du scan réseau n'est plus attribué à tort à un appareil résolu normalement en local.** La provenance de la MAC était déduite après coup par égalité de valeur — un appareil du réseau local était réétiqueté « vu via SNMP, sous-réseau routé » dès que le routeur de l'inventaire connaissait aussi cette IP, le cas courant pour n'importe quelle passerelle. Elle est désormais tracée au moment où la MAC est trouvée.
- Le comptage « N switches / N muets » de la baie de brassage compte sur la même base (déduplication par IP) : un switch injoignable occupant deux emplacements de rack ne compte plus double.
- Le test de capture de la baie n'accuse plus systématiquement des « privilèges insuffisants » pour n'importe quelle erreur (interface débranchée, erreur scapy interne...).
- Un switch qui n'expose ni `sysName` ni `sysDescr` n'est plus re-sondé à chaque cycle d'activité.
- La barre de progression de la cartographie ne recule plus quand la découverte récursive trouve de nouveaux équipements en cours de route.
- Un switch jamais interrogé ne peut plus être sondé plusieurs fois en parallèle par la boucle d'activité, une cartographie et le moniteur en même temps.
- Le relevé de la table MAC d'un switch (jusqu'à 45 s sur un gros switch) ne bloque plus l'activité de tous les autres switches de la baie : verrou par switch au lieu d'un verrou global.
- SNMPv3 vérifie désormais que la réponse reçue correspond bien à la requête envoyée (request-id), comme le fait déjà le SNMP v1/v2c — une réponse tardive à un échange précédent n'est plus acceptée par erreur.
- Une résolution DNS inverse lente (sans délai propre) ne peut plus retenir un hôte du scan au-delà du budget de temps affiché.
- Fuite de socket corrigée sur le cas normal (timeout, la plupart des hôtes n'ont pas NetBIOS) de la détection NetBIOS.
- Les échecs de découverte UPnP/mDNS/ONVIF sont désormais journalisés, comme les sondes SNMP/capture voisines.

## [2.19.40] - 2026-09-05 🩹

### 🩹 Case à cocher « Interrogation SNMP » invisible dans le diagnostic réseau

Signalé par l'utilisateur. La règle CSS globale de `base.html` qui stylise tous les champs de formulaire (`input, select, textarea { width:100%; padding:.55rem .8rem; ... }`) ne mettait pas les cases à cocher / boutons radio à part — toute case sans classe de style dédiée héritait donc d'une largeur à 100 % et d'un padding pensé pour du texte, s'étirant en une barre plate d'environ 230 px de large sur 13 px de haut au lieu d'un carré normal.

Dans la plupart des cas (une case dans une cellule de tableau étroite, par exemple la sélection des lignes du scan réseau) le dégât restait discret. Mais la case « Interrogation SNMP » du panneau de configuration du diagnostic réseau (`templates/diag_reseau.html`) n'a pas cette chance : son texte d'accompagnement (communautés SNMP configurées, utilisateur SNMPv3 le cas échéant) peut être long, et une carte élargie par ce texte combinée à une case étirée sur toute la largeur produit, dans les cas extrêmes, un effondrement complet de la mise en page de la grille — la case finit hors du cadre visible.

Deux correctifs complémentaires :
- `templates/base.html` : les sélecteurs `input[type="checkbox"]`/`input[type="radio"]` sont désormais exclus de la règle globale, et reçoivent à la place une couleur d'accent cohérente avec le thème.
- `templates/diag_reseau.html` : la carte de configuration protège aussi le cas d'un texte réellement très long (`min-width:0` + retour à la ligne du texte plutôt que débordement, case à cocher qui ne rétrécit jamais) — le même principe déjà appliqué ailleurs dans l'app face à ce type de contenu variable (voir les correctifs de défilement horizontal de la v2.19.33/34).

## [2.19.39] - 2026-09-05 📱

### 📱 Interface mobile enrichie + correctifs intégrés

Release qui rassemble, en plus du scan réseau SNMP/capture de 2.19.38, le travail terminé et testé de plusieurs sessions parallèles :

- **Interface mobile (PWA) enrichie** — le dashboard `/m` affiche désormais disponibilité, garanties, périphériques, valeur du parc et activité récente ; la fiche appareil mobile expose marque/modèle/localisation, dernier ping, licences logicielles (masquées), clés de récupération BitLocker (déchiffrées à la demande, auditées comme côté bureau) et les documents joints, réellement téléchargeables ; les contrats affichent montant/numéro, interventions liées et documents ; les utilisateurs, leurs notes. Aucune nouvelle action d'écriture : uniquement des lectures supplémentaires, avec les mêmes règles ACL et de masquage que le reste de l'application.
- **Correctif** : le CSS personnalisé (thème, couleurs) s'affichait en texte brut en haut de la page mobile au lieu de s'appliquer — il n'était pas enveloppé dans une balise `<style>`, contrairement au template bureau.
- **Correctif** : une connexion avec mot de passe à changer obligatoirement, lancée depuis `/m`, renvoyait vers l'interface bureau au lieu de revenir sur mobile une fois le mot de passe changé.
- **Correctif** : la section « Périphériques associés » de la fiche appareil avait disparu (conteneur JS manquant depuis un changement de template), provoquant une erreur à chaque ouverture du formulaire d'édition.

## [2.19.38] - 2026-09-05 🔀

### 🔀 Scan réseau : croiser SNMP et capture passive pour détecter un maximum d'appareils

Demande directe : détecter le plus d'appareils possible, avec le plus d'informations utiles, y compris sur un autre sous-réseau ou VLAN, en croisant plusieurs méthodes (SNMP, écoute réseau...).

La limite de fond, déjà documentée en 2.19.37, reste entière : l'ARP de ce poste ne résout **jamais** une IP située derrière un routeur, même quand la plage est désormais correctement scannée (`sous_reseaux_detectes`). Un appareil sur un VLAN routé restait donc sans MAC ni fabricant, faute d'une résolution de niveau 2 possible sur ce segment-là depuis ce poste. Deux sources viennent maintenant compléter l'ARP local — uniquement en repli, jamais pour écraser une résolution directe déjà obtenue :

**`network_diag.hotes_vus_snmp(client_id)`** interroge la table ARP *des routeurs/switchs SNMP de l'inventaire* — sonde d'existence `_snmp_presence` d'abord (même principe qu'en 2.19.37, ~1 s, coupe court sur un équipement muet plutôt que d'attendre son expiration complète), puis lecture de `ipNetToMediaPhysAddress` avec repli sur `ipNetToPhysicalPhysAddress` selon l'agent. La table ARP d'un routeur est une résolution de niveau 2 *réelle*, faite par cet équipement sur chacune de ses propres interfaces : la MAC qui en ressort est celle de l'appareil visé, jamais celle du routeur — elle traverse donc le VLAN là où l'ARP de ce poste ne le peut pas. `_ip_depuis_suffixe_arp()` extrait l'IP portée par l'index SNMP, identique dans sa structure entre les deux formats de table.

**`network_diag.capture_arp_sightings(duree)`** écoute brièvement le trafic ARP local (scapy, déjà utilisé par la capture passive du palier 2) pour capter un appareil qui ne répond à *aucune* sonde active — ping, ports — mais parle ARP normalement, ce qui couvre nombre d'objets connectés, caméras et imprimantes qui filtrent tout le reste. Complémentaire de la source SNMP : celle-ci ne voit jamais au-delà du segment où tourne ParcInfo, alors que SNMP voit les sous-réseaux distants du routeur.

`_scan_host()` n'utilise ces deux sources qu'en repli, quand la résolution ARP locale ne renvoie rien — jamais en écrasement d'une MAC déjà trouvée directement. La provenance est tracée explicitement (`mac_source: 'snmp'` ou `'capture'`, avec la liste des équipements sources pour SNMP) plutôt que fondue silencieusement avec une résolution locale. Les deux relevés tournent en parallèle des découvertes UPnP/mDNS/ONVIF déjà existantes, sous leur propre budget de temps.

Dans la liste des résultats, un badge 🌐 (SNMP, infobulle nommant l'équipement source) ou 📡 (capture) apparaît à côté de la MAC quand elle provient de l'une de ces sources ; le badge « muet » 🔇 précise « sur un sous-réseau routé (VLAN) » plutôt que le message générique « pare-feu / ICMP bloqué » quand la source est SNMP.

## [2.19.37] - 2026-09-05 🌐

### 🌐 Détection automatique des sous-réseaux supplémentaires d'un site

Signalé par un utilisateur : des appareils sur un second `/24` routé (le même routeur desservant deux réseaux locaux, par exemple `192.168.1.0/24` et `192.168.0.0/24`) n'apparaissaient nulle part — ni le scan réseau, ni la baie de brassage, ni le diagnostic réseau — faute que quiconque pense à taper cette seconde plage à la main.

Le routeur, lui, connaît ses propres sous-réseaux. `network_diag.sous_reseaux_detectes(client_id)` interroge chaque routeur/switch SNMP de l'inventaire (sonde d'existence `_snmp_presence` d'abord, pour ne pas traîner sur un équipement pas encore configuré pour le SNMP), lit sa table IP/routes — la même lecture que la cartographie utilise depuis 2.19.32 — et agrège les sous-réseaux vus, en excluant ceux déjà déclarés. La page **Scan réseau** affiche désormais un encart « Sous-réseaux détectés via SNMP » sous les plages déjà saisies, avec un bouton « + Ajouter » par plage proposée. Rien n'est ajouté ni scanné sans un clic : le mécanisme de scan multi-plages existait déjà, il manquait simplement la suggestion.

`parc_general.plage_ip_locale` accepte désormais plusieurs plages séparées par une virgule. Amélioration associée dans `_appareil_sur_reseau_courant` (surveillance ping en tâche de fond) : une fois le site confirmé sur *au moins une* des plages déclarées, les *autres* plages du même site sont aussi considérées joignables, même quand elles ne chevauchent pas directement l'interface réseau du poste ParcInfo — sans ce correctif, un appareil importé depuis le second sous-réseau serait resté invisible pour le watchdog même après avoir complété `plage_ip_locale`, puisque routé plutôt que directement rattaché.

Limite assumée, et non un défaut à corriger : un appareil découvert sur un sous-réseau **routé** n'aura pas d'adresse MAC résolue (l'ARP ne traverse pas un routeur), donc pas de fabricant détecté ni de corrélation avec la topologie/FDB des switchs. Pour apparaître correctement placé dans la baie de brassage, c'est le switch qui dessert *physiquement* cet appareil qui doit être ajouté à l'inventaire et interrogé en SNMP — détecter le sous-réseau ne suffit pas à ça.

## [2.19.36] - 2026-09-04 🔍

### 🔍 Le motif exact d'un équipement muet s'affiche directement dans la cartographie

Retour terrain sur 2.19.35 : la cartographie va désormais au bout (le blocage est corrigé), mais quand un équipement configuré pour le SNMP est marqué « sans SNMP exploitable », il fallait rouvrir « Tester SNMP » à côté pour savoir *pourquoi* — refus de communauté, utilisateur SNMPv3 invalide, ou silence total. Cette raison était déjà calculée par `_snmp_presence` à l'intérieur de `_topologie_equipement`, puis simplement jetée.

`_topologie_equipement` retourne maintenant le motif exact en plus du booléen « muet » ; `decouvrir_topologie` le porte dans `muets` sous la forme `{ip, detail}` au lieu d'une simple liste d'IP (un équipement qui n'a pas eu le temps d'être relevé avant la fin du budget porte le motif « budget de cartographie atteint pendant le relevé »). La bannière sous la carte affiche désormais ce motif à côté de chaque IP, avec une légende expliquant la différence entre « aucune réponse » (agent silencieux — à vérifier : allumé, bon port/VLAN, pare-feu) et « présent mais refusé » (un agent répond, mais la communauté ou l'utilisateur SNMPv3 configuré ne convient pas).

Correctif de documentation au passage : un paragraphe de `DIAGNOSTIC_RESEAU.md` avait été corrompu par un appel `python -c "..."` exécuté via PowerShell — le backtick y est le caractère d'échappement, donc chaque `` `<lettre> `` d'un code span markdown avait été mangé, avec un caractère BEL invisible produit dans le mot « as_completed ». Corrigé, et vérifié qu'aucun autre fichier du dépôt ne porte la même corruption.

## [2.19.35] - 2026-09-04 🩹

### 🩹 Cartographie qui se figeait + schéma des cascades dans l'infobulle de la baie

**La cartographie ne rendait jamais la main.** Le bouton « Cartographier maintenant » restait sur « Relevé de 1/2 équipements » indéfiniment. Trois causes cumulées, toutes introduites en 2.19.32.

D'abord, la progression n'était émise **qu'une fois par lot**, avant le pool de relevés : avec deux switchs et six ouvriers il n'y a qu'un seul lot, donc un seul message, du début à la fin. Elle est maintenant rapportée à chaque équipement terminé. Ensuite, `as_completed` était appelé **sans délai** et le budget n'était vérifié qu'entre deux lots — donc jamais quand il n'y en a qu'un : un seul équipement lent immobilisait tout le job. Le délai restant lui est transmis, et l'exécuteur est fermé avec `wait=False` pour ne pas bloquer sur un `recvfrom` en cours. Enfin, `_topologie_equipement` enchaîne une vingtaine de parcours SNMP (LLDP ×7, CDP ×5, `lldpRemManAddr`, STP ×4, ENTITY-MIB ×5, IP-MIB ×3, FDB, PVID, ARP) : sur un équipement muet, chacun expirait sur son propre délai, soit près d'une minute par équipement. Une sonde `_snmp_presence` (un `GET sysDescr`, ~1 s) passe désormais **en premier** et rend la main aussitôt si l'agent n'est pas lisible ; une échéance est propagée pour sauter les relevés secondaires quand le budget se tend.

Les équipements muets ne sont plus passés sous silence : ils remontent dans `muets`, le message final les compte, et l'interface indique quoi vérifier (alimentation, communauté ou utilisateur SNMPv3). Quatre tests de non-régression couvrent désormais l'agent muet, l'agent qui refuse le SNMP, l'agent lent face au budget, et la progression.

**Schéma des cascades dans l'infobulle de la baie.** Un port sur lequel plusieurs machines sont détectées se contentait d'un décompte (« 10 appareils · switch non géré probable »). Il affiche maintenant un petit schéma : le port, l'équipement intermédiaire déduit — switch ou borne Wi-Fi selon `_classer_cascade` — et les machines qui parlent à travers lui, chacune avec le symbole de son type (poste, portable, imprimante, caméra, téléphone, NAS, onduleur…), les appareils vus mais absents de l'inventaire étant marqués distinctement. `_voisins_port` renvoie pour cela un `detail` (`{nom, type, id, mac, connu}`) : la liste de noms ne portait pas le type. `_cycle_activite` classe aussi la cascade pour les ports de **switch**, ce qui n'était fait que pour les prises murales. La ligne « Trafic vu » s'efface quand le schéma est présent, sinon les mêmes noms apparaîtraient deux fois, et l'infobulle passe à 340 px (`.tip-large`) parce que le dessin ne tient pas dans les 260 px par défaut. Dessin en SVG inline, sans aucune dépendance.

## [2.19.34] - 2026-09-04 📐

### 📐 Balayage du défilement horizontal : 28 pages mesurées, 4 causes corrigées

Suite de 2.19.33. Les 28 pages de l'application ont été mesurées à 375 px, puis les coupables corrigés et l'ensemble re-vérifié à 320 px. Trois pages débordaient, chacune pour une raison différente, et un quatrième défaut est apparu au passage sur les largeurs de portable.

**Tableau de bord.** Neuf gabarits posent un `display: flex` en style *inline* sur `.page-header` (titre à gauche, boutons à droite) sans jamais déclarer `flex-wrap` : la rangée de boutons, large de 314 px, dépassait son en-tête de 311 px. Une règle partagée dans `base.html` autorise le passage à la ligne sous 700 px, ce qui couvre les neuf gabarits d'un coup. La rangée porte aussi `flex-shrink: 0` en inline : elle ne rétrécissait donc jamais, ses enfants n'avaient jamais « besoin » de passer à la ligne et le `flex-wrap` restait sans effet — d'où le `!important`, seul moyen de contrer une déclaration inline.

**Étiquettes QR.** `.position-box` combine `min-height: 160px` et `aspect-ratio: 1.875/1`, ce qui imposait au bloc une *largeur* de 300 px dans une colonne de grille de 69 px. La planche AVERY J8159 garde ses trois colonnes ; c'est l'aspect-ratio qui dérive désormais la hauteur de la largeur. Les grilles à deux colonnes du formulaire (choix de l'appareil, cases à cocher, paires de champs) passent à une colonne sous 480 px.

**Historique des maintenances.** `.chart-container` est un élément de grille, donc `min-width: auto` : sa largeur minimale était celle de son contenu (un canvas Chart.js de 300 px plus 2 rem de marge de chaque côté, soit 366 px) dans un conteneur de 311 px — et cela malgré la media query `grid-template-columns: 1fr` déjà présente, qui ne pouvait rien contre un minimum de contenu. `min-width: 0` suffit.

**Et un défaut de portable, trouvé au passage.** La navigation horizontale, qui est le mode *par défaut*, réclame environ 1725 px pour ses onze entrées : en dessous, elle débordait et faisait défiler toute la page — donc sur 1280, 1366, 1440 et 1536 px, les largeurs de portable les plus courantes. Entre 901 et 1750 px, les marges et l'interlettrage sont resserrés (ce qui suffit à partir de ~1520 px), le badge du client actif est masqué puisqu'il est déjà affiché dans la barre juste au-dessus, et ce qui ne rentre toujours pas passe à la ligne. Le repli à la ligne a été préféré à `overflow-x: auto`, qui rognerait les menus déroulants (`.nav-dropdown` est en position absolue), et au masquage d'entrées, qui les rendrait inatteignables. La navigation verticale (`body.nav-left`) est explicitement exclue : un `flex-wrap` sur une colonne de 100 vh créerait une seconde colonne au lieu de défiler.

Vérifié à 320, 375, 480, 620, 760, 900, 1000, 1280, 1366, 1440, 1600 et 1920 px. Au-delà de 1750 px, l'affichage est strictement inchangé.

## [2.19.33] - 2026-09-04 📱

### 📱 Barre supérieure compacte sur écran étroit

La barre client exigeait environ 780 px de large : un sélecteur de client à `min-width: 200px`, le libellé « Client : », l'état du watchdog, le bloc de synchronisation et quatre liens écrits en toutes lettres. Comme elle est `sticky` et pleine largeur, elle imposait un **défilement horizontal à toute la page** sur un mobile de 375 px, quelle que soit la page consultée.

Aucune action n'a été retirée, la barre rétrécit. Sous 900 px, le libellé « Client : » et l'état du watchdog (purement informatifs) sont masqués et le sélecteur devient élastique, plafonné à 22 rem pour ne pas s'étirer jusqu'à 440 px sur une tablette. Sous 620 px, les libellés des liens deviennent leurs icônes (« + Nouveau » → ＋, « Gérer → » → ☰) et le nom de l'utilisateur disparaît à côté de son icône : « Nouveau client » et « Gérer les clients » ne sont atteignables que depuis cette barre, la navigation ne les propose pas, il n'était donc pas envisageable de les supprimer. Sous 400 px enfin, la pastille de niveau d'accès cède la place pour garantir un nom de client lisible (plancher de 5 rem), puisque c'est l'information dont cette barre a la charge.

Deux pièges au passage : le bloc doit être placé **après** `.wd-topbar` et `.client-bar-actions` dans la feuille de style (mêmes spécificités, c'est l'ordre qui tranche), et il faut neutraliser leurs `margin-left: auto` — une marge automatique absorbe l'espace libre *avant* que `flex-grow` ne le distribue, ce qui laissait le menu déroulant à 25 px de large, illisible.

Vérifié à 320, 375, 480, 620, 760, 900 et 1280 px : la barre ne déborde plus à aucune largeur, les sept actions restent accessibles partout, et l'affichage au-delà de 900 px est strictement inchangé.

## [2.19.32] - 2026-09-04 🗺

### 🗺 Audit de la découverte de topologie réseau : 21 constats, tous traités

Audit complet du palier 4 (topologie L2), des primitives SNMP qui l'alimentent et de son interface.

**Exactitude.** Trois défauts écrivaient de fausses données dans la baie de brassage. `appliquer_topologie_baie` prenait l'**ifIndex SNMP pour un numéro de port de façade** — alors que `_mapping_baie_ifindex`, dans le même fichier, documente que cette égalité n'est « presque jamais » vraie, au point que le repli naïf est désactivé par défaut pour les LED. Sur un switch qui numérote ses ifIndex en 10101, la fonction créait un port fantôme 10101 dans le rack. Elle passe désormais par `_mapping_baie_ifindex` et n'invente plus de port. Deuxième défaut : aucune garde sur les ports d'**uplink**, qui apprennent toutes les MAC d'en aval — le premier appareil arbitraire vu derrière un uplink était affecté au port d'uplink. Nouvelles colonnes `nb_macs_port` / `est_uplink` dans `diag_topologie` ; seuls les ports d'accès alimentent une affectation automatique (`_mapping_baie_ifindex` les filtre aussi). Troisième : le constat `cablage_incoherent` comparait `baie[ifindex]` à un dictionnaire indexé par numéro de façade, soit un faux positif soit un silence — mapping inversé, sources fiables uniquement, et le libellé nomme maintenant le port de façade. Enfin, `_snmp_walk_octets` ne tentait jamais **SNMPv3** : sur un switch v3-only, le port, la MAC et les capacités des voisins LLDP, la table ARP et les MAC d'infra étaient tous perdus (donc plus aucune détection de lien switch ⇄ switch) — chemin v3 ajouté, ainsi que dans `_snmp_bulk_cols`. Et la FDB était **tronquée en silence à 800 entrées** : plafond porté à 5 000, troncature remontée dans le `meta` de `_releve_mac_switch`.

**Vitesse.** `app._snmp_walk` était un GETNEXT strict, un aller-retour UDP par ligne — c'est lui qui parcourt la table MAC, la table LLDP et `dot1dBasePortIfIndex`. Il fait désormais du **GETBULK v2c** (max-repetitions 25) avec repli GETNEXT à l'identique si l'agent refuse : ~25 fois moins de paquets, sans changer une seule ligne appelante. `_v3_walk` aussi. Le snapshot faisait **deux `interroger_equipement` complets par switch** (phase SNMP puis phase topologie, qui n'en gardait que les noms de ports) : la topologie consomme maintenant le cache `_noms_interfaces`. Les switchs, indépendants, sont interrogés **de front** (pool de 6) au lieu d'être mis bout à bout. Et le relevé VLAN par VLAN, qui sondait les 32 premiers VLAN déclarés (vides compris), suit maintenant `dot1qPvid` — les VLAN réellement portés par un port, du plus chargé au moins chargé, sous budget.

**Exhaustivité.** La découverte ne sortait jamais de l'inventaire : un switch bien réel mais jamais saisi n'était pas interrogé, même quand son voisin annonçait son adresse de gestion. `lldpRemManAddr` est lu et la découverte devient **récursive** (profondeur 3) ; les équipements trouvés hors inventaire sont proposés à l'ajout, jamais créés d'office. Ajout du **VLAN par port**, du **STP** (`dot1dStpRootPort` / `PortDesignatedBridge` / `PortState`, qui donne le sens des liens même sans LLDP), de l'**ENTITY-MIB** (modèle, numéro de série, composition d'un stack) et des **sous-réseaux du routeur** (`ipAddrTable` / `ipCidrRouteTable`) proposés comme plages de scan — rien n'est balayé sans validation. Nouvelle table `diag_topologie_mouvements` : `diag_topologie` étant écrasée à chaque passage, un appareil qui changeait de port ne laissait aucune trace ; les transitions (apparu / disparu / déplacé) sont journalisées avec la rétention `diag_reseau_max_jours`.

**Interface.** La page s'appelait « Topologie découverte » mais rendait des tableaux plats à trois colonnes, sans liens ni hiérarchie, où un uplink chargé de trente MAC était une ligne comme une autre. Nouvel onglet **Topologie** avec une **carte L2 en SVG inline** (aucune dépendance) : un nœud par équipement, les liens LLDP/CDP orientés par le STP, les appareils feuilles comptés par switch. Les **uplinks sont repliés** en une ligne « → SW-Etage1 · N appareils en aval », dépliable au clic. La **fraîcheur** de la carte est affichée (« relevée il y a 3 j », grisée au-delà de 24 h) — la donnée était en base mais n'était jamais remontée, alors que la FDB oublie une machine éteinte en cinq minutes. La cartographie peut être **lancée seule** en tâche de fond (elle n'était relevée qu'en avant-dernière phase d'un diagnostic complet et sautée dès que le budget de 120 s était dépassé). Enfin, « Reporter dans la baie » écrivait en base après un simple `confirm()` : c'est maintenant une **modale d'aperçu** ligne par ligne, avec cases à cocher, l'origine du numéro de port et la liste explicite des cas non proposés.

## [2.18.43] - 2026-08-23 🧪

### 🧪 Couverture de tests /login + ACL CRUD, 2 correctifs trouvés au passage

Chantier suivant de l'audit du 2026-08-23 : le mécanisme d'ACL le plus critique du projet (can_write) et le seul point d'entrée non authentifié (/login) n'avaient jusqu'ici aucune couverture de test dédiée. Ajout de test_login.py (identifiants corrects/incorrects, compte désactivé, rate-limiting 10 tentatives/5min avec reset après succès, protection open-redirect sur `next`, timeout de session 8h) et test_acl_crud.py (pour appareil/contrat/identifiant : création/modification/suppression bloquées avec un accès lecture seule, autorisées avec un accès écriture). Deux bugs réels trouvés en écrivant ces tests, corrigés au passage : 1) login.html est une page autonome qui n'affichait jamais les messages flash (get_flashed_messages jamais appelé) — le message de timeout de session posé par login_required atterrissait dans le vide. page_login() reprend désormais le premier message flash comme erreur affichée s'il n'y en a pas déjà une. 2) supprimer_identifiant lisait le nom de l'identifiant à journaliser sans filtrer par client_id (contrairement à supprimer_contrat) : la suppression elle-même était déjà bien scopée et donc sans effet cross-tenant, mais le nom d'un identifiant d'un autre client pouvait fuiter dans l'historique du client actif. Scopé par id ET client_id.

## [2.18.42] - 2026-08-23 🔒

### 🔒 Correctif faille d'isolation multi-client (IDOR) sur la fiche appareil

Trouvé lors d'un audit du projet : editer_appareil et supprimer_appareil ne filtraient les appareils que par leur id, jamais par client_id — contrairement à toutes les routes CRUD comparables (contrats, documents, BitLocker). Un utilisateur disposant d'un accès en écriture sur un seul client pouvait donc consulter, modifier ou supprimer l'appareil de n'importe quel autre client en devinant/incrémentant l'id dans l'URL. Corrigé en scopant toutes les lectures/écritures (SELECT/UPDATE/DELETE, y compris le nettoyage des clés BitLocker et des collectes liées) par id ET client_id, avec un test de non-régression permanent (test_isolation_client_appareil.py) qui vérifie à la fois le blocage cross-tenant et que le chemin légitime fonctionne toujours. Profite aussi à la fiabilité de la CI : la liste de tests de build-release.yml était figée à la main et avait fini par oublier 12 fichiers de test sur 31, dont celui qui vérifie l'application de can_write() — remplacée par une découverte automatique de tout test_*.py, pour qu'un nouveau test ne soit plus jamais oublié avant une publication.

## [2.18.41] - 2026-08-22 🍎

### 🍎 Correctif nom DNS macOS + doublons stockage APFS (collecteur 3.15)

Signalé sur une vraie fiche système macOS, deux problèmes distincts. 1) Nom DNS fantaisiste : get_fqdn() utilisait socket.getfqdn(), notoirement peu fiable sur macOS — il déclenche gethostbyaddr(gethostname()), et quand cette résolution retombe sur le bouclage IPv6 (::1) sans enregistrement PTR configuré, certains résolveurs renvoient tel quel le nom de la requête plutôt qu'une erreur. Constaté : dns_name valait "1.0.0.0.[...]0.0.ip6.arpa" (la représentation PTR standard de ::1) au lieu du vrai nom de machine. Remplacé sur macOS par `scutil --get LocalHostName` (+ suffixe .local) : lit directement le nom Bonjour configuré localement, sans passer par une résolution réseau. 2) Stockage qui semblait "multiplié" : même après le correctif critique de la 2.18.34 (qui avait réglé le total gonflé), chaque volume de service macOS/APFS (Preboot, VM, Update, xarts, iSCPreboot, Hardware — présents sur tout Mac moderne) restait affiché individuellement, donnant l'illusion visuelle de 6 à 8 "disques" quasi identiques là où il n'y en a physiquement qu'un, chacun rapportant à `df` la même capacité totale que le conteneur partagé. Ces points de montage sont désormais exclus de l'affichage ET du total — seuls `/` (Système) et `/System/Volumes/Data` restent, les deux volumes où vit réellement le contenu de la machine. Sans effet sur Linux (mêmes points de montage jamais produits par un système de fichiers non-APFS).

## [2.18.40] - 2026-08-22 📱

### 📱 Interface mobile enrichie (documents, licences, BitLocker)

Après un premier passage en conditions réelles sur téléphone, complète la consultation mobile avec les données déjà disponibles côté bureau mais absentes de la première version. Dashboard : disponibilité, garanties, périphériques, valeur du parc, activité récente. Appareils : marque/modèle et localisation en liste, dernier ping, licences logicielles (masquées), clés de récupération BitLocker (déchiffrement à la demande, audité comme côté bureau), documents joints réellement téléchargeables (avant : simple compteur renvoyant vers la version bureau). Contrats : montant/numéro en liste, interventions liées et documents téléchargeables en détail. Utilisateurs : notes affichées. Aucune nouvelle action d'écriture — uniquement des lectures supplémentaires, mêmes règles ACL et de masquage des mots de passe que le reste de l'app.

## [2.18.39] - 2026-08-22 🎨

### 🎨 Correctif : CSS visible en texte brut sur mobile

Remonté après test réel sur téléphone : le CSS dynamique (couleurs d'accent personnalisées) apparaissait comme du texte brut sur les pages de l'interface mobile au lieu d'être appliqué. Cause : {{ dynamic_css }} était injecté nu dans <head>, hors de toute balise <style> — la fonction qui le génère ne renvoie que des règles CSS brutes, jamais la balise elle-même. Un navigateur qui rencontre du texte orphelin dans <head> le pousse dans <body> et l'affiche tel quel. Le template bureau enveloppait déjà correctement ce même appel dans <style id="cfg-vars">; le template mobile ne le faisait pas. Correctif : même enveloppe appliquée côté mobile.

## [2.18.38] - 2026-08-22 🔑

### 🔑 Correctif : connexion depuis l'interface mobile

Remonté après test réel sur téléphone : se connecter depuis /m avec un compte dont le mot de passe doit être changé (dont l'admin par défaut) renvoyait vers la page bureau de changement de mot de passe, sans jamais revenir sur mobile ensuite — l'utilisateur se retrouvait sur l'interface bureau (large, non adaptée) en pensant que la version mobile n'avait jamais été appliquée. Correctif : la destination d'origine (next=/m/...) est désormais portée à travers le changement de mot de passe forcé et restituée une fois celui-ci effectué.

## [2.18.37] - 2026-08-22 📱

### 📱 Interface mobile de consultation (PWA)

Demandé : une version Android/mobile de ParcInfo, orientée consultation plutôt que modification, avec une ergonomie simple pour un usage au smartphone. Réponse : une nouvelle surface /m, PWA installable sur l'écran d'accueil Android, strictement en lecture seule (aucune route n'accepte POST/PUT/DELETE). Dashboard, appareils, contrats, utilisateurs et identifiants, avec recherche et filtres instantanés côté client. Mêmes ACL et helpers de formatage que la version bureau — aucune règle de sécurité dupliquée ou affaiblie. Les mots de passe restent masqués par défaut ; ceux stockés chiffrés (table identifiants) ne sont déchiffrés qu'à la demande via l'endpoint existant /api/identifiant/<id>/mdp, jamais rendus en clair côté serveur pour la liste — plus strict que la page bureau équivalente. Le service worker ne met en cache que les icônes, jamais les pages ni les données du parc, pour éviter qu'un téléphone partagé ou perdu ne conserve des informations sensibles hors ligne.

## [2.18.36] - 2026-08-21 🚪

### 🚪 Sortie d'App Translocation sans Terminal (collecteur 3.14)

Question directe après la 2.18.35 : faut-il quand même passer par xattr (donc par le Terminal) pour éviter App Translocation ? Réponse : non — le message affiché par le collecteur GUI dans ce cas ne proposait jusqu'ici QUE la commande xattr -cr, alors qu'une solution purement Finder existe et est même la plus simple : glisser-déposer l'app vers un autre dossier (Applications, Bureau, peu importe). macOS ne déclenche l'App Translocation que pour une app restée dans son dossier de téléchargement d'origine sans jamais avoir été déplacée — le simple fait de la bouger une fois avec le Finder suffit à lever la protection définitivement, sans commande à taper. Message mis à jour en conséquence (README + boîte de dialogue du collecteur GUI) : la méthode Finder est désormais présentée en premier, xattr -cr reste mentionné en alternative pour qui préfère le Terminal. Corrigé au passage : le message citait encore une commande "sudo" à relancer, une référence obsolète depuis le passage à l'authentification via osascript en 2.18.35 (aucun sudo n'est plus jamais affiché).

## [2.18.35] - 2026-08-21 🔐

### 🔐 Élévation macOS sans ligne de commande (collecteur 3.13)

Demandé : une façon de lancer le collecteur GUI macOS Intel avec les droits administrateur sans passer par le Terminal. Jusqu'ici, faute d'équivalent fiable à ShellExecuteW(runas) côté macOS pour relancer tout un process GUI avec élévation, la proposition d'élévation affichait juste la commande sudo à taper soi-même — la préoccupation étant qu'une tentative automatique via `osascript ... with administrator privileges` puisse laisser le technicien sans aucune fenêtre ouverte si l'authentification échouait, sans moyen fiable de le détecter avant que le process courant ne se soit déjà fermé. Résolu autrement : `_relancer_macos_eleve()` déclenche bien l'invite d'authentification macOS standard (la même que Réglages Système ou l'installation d'un logiciel) via osascript, mais en fire-and-forget — la fenêtre courante n'est JAMAIS fermée automatiquement. Au pire, une fois authentifié, le technicien se retrouve avec deux fenêtres ouvertes au lieu d'une ; jamais avec aucune. Toujours pas de ligne de commande à taper : juste le mot de passe/Touch ID dans l'invite système native.

## [2.18.34] - 2026-08-21 💾

### 💾 Correctif critique stockage macOS + GPU absent (collecteur 3.12)

Signalé : le collecteur macOS Intel ne remontait rien sur le stockage ni le GPU. 1) Stockage — bug critique : `_unix_disks()` (partagé macOS/Linux) ne reconnaissait que le format de taille GNU (Linux, « 494G ») dans son parseur `to_gb()`. `df -h` BSD (macOS) suffixe les unités binaires d'un « i » (« 494Gi », « 11Mi » — même base 1024, suffixe différent), qu'aucune des trois vérifications `endswith('T'/'G'/'M')` ne matchait jamais. Résultat : `size_gb` valait systématiquement None sur macOS, chaque ligne était silencieusement filtrée, et la section stockage entière ressortait vide — sans qu'aucune exception ne le signale. Remplacé par une expression régulière tolérant les deux formes. Effet de bord corrigé dans la foulée : disk_total_gb/disk_free_gb sommaient bêtement TOUTES les lignes df. Sur macOS/APFS, plusieurs volumes d'un même conteneur (Système, Données, VM, Preboot...) rapportent TOUS la même capacité totale et le même espace libre partagé — un Mac de ~500 Go avec 8 volumes dans son conteneur aurait affiché ~2,5 To de stockage total. Corrigé par déduplication par couple de valeurs identiques avant sommation, sans effet sur Linux. 2) GPU — absent, pas un bug : aucune collecte n'avait jamais été implémentée côté macOS. Ajouté (gpu/gpu_details), réutilisant le même appel system_profiler SPDisplaysDataType que les écrans (un seul appel pour les deux).

## [2.18.33] - 2026-08-21 🌐

### 🌐 Réseau macOS étoffé : passerelle, DNS, proxy, écrans (collecteur 3.11)

Signalé après examen d'une vraie fiche système macOS : passerelle par défaut, IP publique, FAI, suffixes DNS et marque/année d'écran manquants ou vides. 1) Passerelle par défaut/DNS/proxy n'avaient tout simplement jamais été implémentés côté macOS (_win_network() n'a jamais eu d'équivalent) : default_gateway/gateways via `route -n get default`, dns_servers/ dns_suffixes via `scutil --dns` (tous les blocs resolver, pas seulement le premier — un VPN/split-DNS peut en ajouter), proxy via `scutil --proxy`. 2) IP publique/FAI (get_public_ip_info()) sont déjà cross-plateforme (simple appel HTTPS, sans code spécifique à un OS) — vérifié, aucun bug trouvé ; une absence isolée est plus probablement un aléa réseau au moment de la collecte (service tiers ipapi.co, timeout de 6 s) qu'un manque côté collecteur. 3) Marque/année d'écran : la marque était câblée à vide, jamais tentée. Devinée depuis le premier mot du nom EDID (souvent « DELL U2720Q ») contre une petite liste de marques connues — jamais depuis l'identifiant vendeur EDID brut, dont l'encodage exact est trop incertain selon la version macOS pour être fiable sans matériel réel. Année lue quand l'EDID de l'écran externe l'expose. Pas de taille physique ajoutée : system_profiler ne l'expose pas de façon fiable. 4) Bug de longue date trouvé en chemin et corrigé : `dns_suffixes` est documenté et rendu partout (fiche, PDF) comme une LISTE, mais _win_network() y stockait une chaîne déjà jointe par PowerShell — build_summary_sections() la rejoignait alors CARACTÈRE PAR CARACTÈRE au lieu de suffixe par suffixe. Resté inaperçu car invisible sur un poste sans suffixes de recherche configurés (le cas le plus courant). Reconverti en liste réelle côté Windows, symétrique de macOS ; PDF et fiche système, qui affichaient jusqu'ici la chaîne déjà jointe telle quelle, rejoignent désormais eux aussi une vraie liste.

## [2.18.32] - 2026-08-21 🛠

### 🛠 Correctif : commande sudo inutilisable sous App Translocation (collecteur 3.10)

Signalé après la 2.18.31 : la commande sudo affichée par la proposition d'élévation macOS échouait avec "command not found" une fois collée dans un Terminal. Cause : l'app n'avait jamais été ouverte/déplacée depuis son téléchargement, donc encore sous protection Gatekeeper « App Translocation » — macOS l'exécute dans ce cas depuis une copie temporaire en lecture seule à un chemin aléatoire (/private/var/folders/.../AppTranslocation/...) au lieu de son vrai emplacement. sys.executable/argv[0] reflètent ce chemin temporaire, propre à la session de lancement en cours : la commande sudo construite dessus n'est pas exploitable depuis un Terminal séparé. Corrigé : _proposer_elevation() (system-info-collector-gui.py) détecte désormais ce cas (/AppTranslocation/ dans le chemin) et affiche à la place la marche à suivre pour lever la translocation (xattr -cr sur le vrai chemin de l'app, récupéré en la glissant dans le Terminal) plutôt qu'une commande fausse. Le README documente aussi la prévention (xattr -cr avant le tout premier lancement).

## [2.18.31] - 2026-08-21 📡

### 📡 Correctif adresse IP macOS + proposition d'élévation GUI (collecteur 3.9)

Signalé après la 2.18.30 : le connecteur transmettait bien les données à ParcInfo, mais pas l'adresse IP sur macOS. Cause : get_ip_addresses() utilisait socket.gethostbyname_ex(hostname), peu fiable sur macOS — le hostname y est souvent un nom mDNS en « .local » (Bonjour) que cette résolution classique ne fait pas toujours aboutir, la liste ressortant alors vide SANS lever d'exception (rien qu'un try/except ne pouvait détecter). Corrigé par un repli sur un socket UDP « connecté » à une adresse externe (aucun paquet réellement envoyé — connect() sur UDP ne fait qu'interroger la table de routage) : donne l'IP de l'interface que le système utiliserait réellement pour sortir, fiable sur les trois OS. Demandé dans la foulée : que le collecteur GUI propose de relancer avec les droits administrateur au lancement plutôt que de collecter en silence une fiche incomplète. Ajouté avec une asymétrie assumée entre plateformes.

**Windows** : relance automatique via ShellExecuteW(..., "runas", ...) sur confirmation — le code de retour se lit de façon synchrone et fiable (annulation UAC détectée sans ambiguïté).

**macOS/Linux** : pas de relance automatique — aucun équivalent fiable à ShellExecuteW pour relancer un process GUI entier avec élévation ; une tentative via osascript risquerait de laisser le technicien sans aucune fenêtre ouverte si l'authentification est annulée, sans moyen de le détecter avant que le process courant se soit déjà fermé. La boîte de dialogue affiche donc la commande sudo à lancer soi-même, puis continue sans élévation — jamais de fenêtre perdue.

## [2.18.30] - 2026-08-21 🔍

### 🔍 Diagnostic macOS étoffé (collecteur 3.8)

Demandé après les correctifs des 2.18.28/2.18.29 : étudier ce que la collecte macOS Intel pourrait encore gagner à remonter pour le diagnostic, le dépannage et l'enregistrement des caractéristiques machine. La plupart des ajouts réutilisent des champs déjà génériques (rendus sans condition d'OS par la fiche système et le PDF, même principe que les précédentes passes macOS) — trois seulement ont demandé d'ajouter des lignes à des tables existantes. 1) os_name/os_build via sw_vers (plus fiable que platform.mac_ver(), qui a traîné derrière plusieurs versions majeures macOS) + nom marketing (« macOS Sequoia ») via une table de correspondance tenue à la main ; bios_version/bios_manufacturer désormais aussi lus (Boot ROM Version). 2) Nouveaux champs de sécurité/gestion : SIP (csrutil), Gatekeeper (spctl), inscription MDM (profiles status -type enrollment) — posture de sécurité macOS sans équivalent Windows direct, mais du même ordre d'utilité que TPM/Secure Boot. 3) Sauvegardes Time Machine (tmutil) réutilisant restore_points (Windows : Restauration système) — même question de fond, pertinente pour la gestion de parc. 4) Diagnostic réutilisant system_incidents (Windows : écrans bleus, erreurs disque) : plantages applicatifs et paniques noyau, comptés par lecture de répertoire (~/Library/Logs/DiagnosticReports), jamais par parsing de contenu — robuste aux changements de format entre versions macOS. 5) startup_programs réutilisé pour les LaunchAgents/LaunchDaemons (lus via plistlib, format stable, plutôt qu'en scrapant launchctl list) ; top_processes_cpu/ram via ps (rss en Ko converti en Mo, PAS %mem — un bug trouvé et corrigé avant publication : %mem est un pourcentage, l'utiliser aurait rendu ram_mb silencieusement incohérent d'un poste à l'autre). 6) SSID Wi-Fi enregistrés (networksetup), jamais le mot de passe : le lire depuis le Trousseau déclencherait une invite Touch ID/mot de passe PAR RÉSEAU, incompatible avec une collecte automatisée.

## [2.18.29] - 2026-08-21 🐛

### 🐛 Correctif critique : "Expected str, bytes or os.PathLike object, not NoneType" à l'envoi (macOS)

La 2.18.28 avait corrigé le lancement de ParcInfo-Collector.app depuis le Finder (chemin de journal relatif). Signalé juste après : l'envoi d'une collecte plantait maintenant avec "Expected str, bytes or os.PathLike object, not NoneType" — même famille de bug, un cran plus loin. Cause : generate_pdf_report()/generate_html_report() (collector_core.py) écrivent le rapport sous un nom de fichier RELATIF (_report_filename()), résolu contre le répertoire courant — "/" sous Finder/LaunchServices macOS, non inscriptible. L'échec d'écriture était déjà rattrapé côté collector_core (retourne `report_file=None`, contenu quand même renvoyé), mais system-info-collector-gui.py appelait ensuite sans garde `Path(report_file).resolve()` pour retenir le chemin du dernier rapport — `Path(None)` lève exactement ce TypeError. Corrigé à la racine : le collecteur GUI se repositionne désormais sur le dossier personnel de l'utilisateur (os.chdir(Path.home())) dès le lancement, avant toute écriture — rend inscriptible tout chemin relatif écrit par la suite, pas seulement celui identifié ici. Garde défensive ajoutée en plus (report_file vérifié avant Path(...)), pour qu'un futur échec d'écriture (disque plein, etc.) dégrade proprement au lieu de planter.

## [2.18.28] - 2026-08-21 🐛

### 🐛 Correctif critique : ParcInfo-Collector.app ne se lançait pas sur macOS

Signalé par l'utilisateur juste après la 2.18.27 : double-cliquer sur ParcInfo-Collector.app (fraîchement compilé pour macOS Intel) ne faisait STRICTEMENT rien — pas de fenêtre, pas d'icône dans le dock, pas d'erreur. Lancer le binaire directement depuis un terminal fonctionnait parfaitement (connexion serveur, récupération des clients) : le bug n'était donc pas dans la compilation PyInstaller/Rosetta, mais dans le code lui-même. Cause : system-info-collector-gui.py configurait son journal avec un chemin RELATIF (`logging.FileHandler('collector-gui.log')`), résolu contre le répertoire courant au démarrage. Sous Windows, double-cliquer un .exe positionne ce répertoire sur le dossier de l'exe (inscriptible) — sous macOS, Finder/LaunchServices lance l'app avec pour répertoire courant "/" (racine), où un utilisateur normal n'a pas le droit d'écrire. La construction de FileHandler levait donc un PermissionError non rattrapé dès l'import du module — avant même la fenêtre Tk — et l'app (compilée en mode windowed, sans console pour afficher la trace) se fermait aussitôt en silence. Corrigé : chemin absolu (~/.parcinfo-collector-gui.log, à côté du fichier de config qui, lui, utilisait déjà Path.home()), et construction du FileHandler protégée par try/except pour qu'un home dir inaccessible ne bloque plus jamais le lancement, quelle que soit la plateforme.

## [2.18.27] - 2026-08-21 🍎

### 🍎 Collecteur macOS Intel compilé (exécutables GitHub Releases)

La 2.18.26 avait étoffé les DONNÉES collectées sur macOS mais laissé de côté la distribution : seul Windows avait des exécutables compilés (system-info-collector.exe/-gui.exe) sur la page Releases, un technicien Mac devait lancer `python system-info-collector.py` à la main. Corrigé : le job build-macos-intel (déjà en place pour ParcInfo.app, croisement Rosetta x86_64) construit maintenant aussi system-info-collector (binaire brut, CLI) et ParcInfo-Collector.app (GUI Tkinter) pour macOS Intel, attachés à la release au même titre que les exécutables Windows. Les deux étapes sont marquées best-effort (continue-on-error) — la GUI en particulier dépend d'un empaquetage Tk sous Rosetta moins éprouvé que le reste de la chaîne — et la génération du SHA256SUMS/la liste des fichiers de la release ont été rendues tolérantes à leur absence éventuelle, pour qu'un échec ponctuel de ces deux artefacts ne bloque jamais la release des autres (Windows, macOS ARM, Docker).

## [2.18.26] - 2026-08-21 🖥

### 🖥 Collecteur macOS étoffé (v3.5)

Le collecteur système macOS était resté bien plus sommaire que la version Windows (35 étapes) : rien sur les imprimantes, les ports en écoute, l'accès distant, les agents de télémaintenance/EDR, ou les mises à jour système disponibles. Rapproché de la richesse Windows avec des données réellement pertinentes sur macOS (pas de TPM/BitLocker/GPO façon Windows, qui n'existent pas sur cet OS) : 1) Imprimantes (lpstat/CUPS), ports TCP en écoute (lsof), accès distant — Connexion à distance/SSH et Partage d'écran/VNC-ARD — et agents de télémaintenance/RMM/EDR (AnyDesk, TeamViewer, CrowdStrike, SentinelOne, Jamf, Kandji...) détectés par recoupement process + applications installées. 2) Mises à jour système et apps Apple disponibles (softwareupdate -l), distinct des mises à jour Homebrew déjà détectées par logiciel. 3) Inventaire logiciel enrichi : version et éditeur réels via system_profiler SPApplicationsDataType, plus seulement le nom. Ces six champs (imprimantes, ports en écoute, accès distant, agents, mises à jour, logiciels) étaient déjà rendus sans condition d'OS par la fiche système et le rapport PDF — aucune modification de template ni de rapport n'a donc été nécessaire, seule la collecte macOS elle-même a été étoffée.

## [2.18.25] - 2026-08-21 🗺️

### 🗺️ Reprise en profondeur du scan réseau : base OUI, ARP, complétude

Refonte demandée pour obtenir une image la plus complète et fiable
possible du réseau, sans identification manuelle par IP/MAC.

**Bug critique : la base OUI complète ne se chargeait jamais en exécutable packagé**

Trouvé en creusant l'imprécision du scan signalée en usage réel : le
chemin de `oui.txt` était calculé depuis `__file__`, qui pointe en
exécutable packagé (PyInstaller `--onefile`) vers un dossier d'extraction
**temporaire recréé à chaque lancement** — jamais le dossier réel de
l'exe. Aucun `.exe`/`.app` ParcInfo n'a donc jamais pu charger la base OUI
complète (~60 000 fabricants), même en suivant la documentation à la
lettre (télécharger `oui.txt`, le placer à côté de l'exe) — replié
silencieusement, pour toujours, sur les ~930 préfixes embarqués,
largement insuffisant pour identifier correctement la plupart du matériel
récent. Corrigé : `oui.txt` vit désormais dans le même dossier que la
base de données, comme le reste des données persistantes.

**Téléchargement et mise à jour automatique de la base OUI**

- Téléchargement en arrière-plan au démarrage si le fichier est absent,
  jamais bloquant.
- Rafraîchissement automatique (cron quotidien) si le fichier a plus de
  30 jours — nécessaire pour une instance qui tourne en continu (Docker)
  sans jamais redémarrer.
- Bouton « 🔄 Mettre à jour » sur la page Scan, avec statut en direct
  (nombre de préfixes, date de dernière mise à jour).
- Remplacement atomique du fichier ; un échec réseau ne casse jamais la
  base déjà chargée.

**Détection ARP des appareils silencieux**

Un appareil qui bloque ICMP (pare-feu, très nombreux objets connectés)
répond quand même forcément à une résolution ARP pour exister sur le
réseau local — le scan abandonnait jusqu'ici dès l'échec du ping, ratant
purement et simplement ces appareils. Ils apparaissent désormais dans les
résultats (IP, MAC, fabricant) avec un badge 🔇 discret expliquant
pourquoi peu d'informations sont disponibles, plutôt que d'être
totalement absents de la liste.

**Découverte mDNS élargie**

Ajout de Matter (`_matter._tcp`/`_matterc._udp` — standard smart-home
unifié, adoption rapide) et de la présence générique de poste
(`_workstation._tcp`, capte un nom d'hôte là où NetBIOS/DNS inverse ne
répondent pas).

## [2.18.24] - 2026-08-21 📶

### 📶 Hiérarchie de priorité explicite entre les méthodes de détection au scan

Suite du signalement selon lequel les méthodes de détection s'écrasaient
encore entre elles. `_deviner_type()` documente désormais explicitement
6 niveaux de confiance (structuré > fabricant MAC > fabricant réseau
générique > SNMP actif > ports ambigus > TTL), pour qu'un signal fort ne
soit plus jamais écrasé par un signal plus faible arrivé après lui dans
le code.

**SNMP actif devient un signal fort à lui seul**

Un agent SNMP qui répond (community « public ») est désormais vérifié
*avant* les heuristiques de port SMB/22 : un switch, un onduleur ou une
imprimante SNMP dont aucun mot-clé hostname/sysDescr n'a matché par
ailleurs ne retombe plus sur « PC (Windows) » via la seule présence du
port 445 — un PC ou un Mac de bureau n'expose pratiquement jamais SNMP.
RDP (3389) reste prioritaire même sur SNMP : aucun équivalent légitime
n'existe hors Windows.

**Raspberry Pi reconnu avant les heuristiques de port**

Un Pi-hole ou un Home Assistant tournant sur Raspberry Pi, avec Samba
(445) ou SSH (22) actif, ressortait comme « PC (Windows) » par la même
mécanique — le fabricant MAC officiel (Raspberry Pi Foundation) est
désormais vérifié en amont.

**sysDescr SNMP : couverture élargie**

Mots-clés ajoutés pour les OS réseau non ambigus (FortiOS, SonicOS,
DD-WRT, OpenWrt, OPNsense — famille pare-feu/routeur) et pour ceux
partagés entre routeurs et switches chez un même constructeur (JUNOS,
RouterOS, Comware, VRP, ArubaOS), qui retombent sur la catégorie
générique « Switch/AP » plutôt qu'un sous-type deviné à tort. Timeout
SNMP porté de 0,5 s à 0,8 s pour laisser un peu plus de marge à un agent
plus lent à répondre.

**Recherche complémentaire** : le fingerprinting SMB2 (dialectes,
capacités) a été envisagé mais jugé insuffisamment fiable pour être
exploité proprement — il ne fournit qu'une estimation, jamais une
identification précise. Non implémenté plutôt que fait à moitié.

## [2.18.23] - 2026-08-20 🔌

### 🔌 Identification réseau affinée : SMB, objets connectés (ESP32/Tuya/Shelly)

Retour d'usage réel après la 2.18.22 : un Mac ressortait comme « PC
(Windows) », un switch comme « Serveur », un ESP32 ou une prise connectée
comme « PC ». Recherche sur les pratiques de fingerprinting réseau (nmap,
Fingerbank) à l'appui de ces correctifs.

**SMB n'implique plus Windows à tort**

Les ports 135/445 (SMB) l'emportaient systématiquement sur le TTL, alors
que ce n'est pas un signal Windows fiable à lui seul : Samba (Linux) et le
partage de fichiers natif de macOS y répondent aussi. Un Mac avec le
partage de fichiers activé ressortait donc comme « PC (Windows) »,
corrigé — le TTL garde désormais la priorité quand il pointe clairement
ailleurs (Linux/Unix, macOS) ; ces ports ne servent de repli Windows que
faute de meilleur indice. RDP (3389), lui, reste un signal fort à lui
seul : aucun équivalent légitime n'existe hors Windows.

**Fin des faux « Serveur »**

Le mot-clé « server »/« srv » nu (hostname ou bannière de service)
suffisait seul à conclure « Serveur » — or un switch ou une imprimante bon
marché non reconnu par ailleurs peut très bien afficher « Print Server »
ou « Web Server Login » sur sa page d'administration embarquée. Un vrai
port de service serveur (MySQL, MSSQL, PostgreSQL, SMTP, IMAP...) est
désormais exigé à l'appui ; les mots-clés non ambigus (Exchange, vCenter,
ESXi, contrôleur de domaine) suffisent toujours seuls.

**Nouvelle catégorie « Objet connecté »**

La quasi-totalité des prises, capteurs et relais grand public (Tuya,
Sonoff, Shelly...) sont bâtis sur des puces Espressif (ESP32/ESP8266),
revendues en marque blanche sous des dizaines de noms commerciaux — une
liste de mots-clés ne peut pas suivre le marché. Le fabricant MAC officiel
de la puce radio, lui, est un signal bien plus robuste : c'est le même
principe qu'utilise Fingerbank, la référence du secteur pour le
fingerprinting réseau. Complété par les noms de firmware les plus
courants en hostname (Tasmota, ESPHome, Shelly, Sonoff, Tuya, ESP32) et
par 3 nouveaux services mDNS interrogés (HomeKit, Shelly, ESPHome).

**Limite connue, non implémentée** : le fingerprinting DHCP (options
55/60) est le signal le plus fiable du secteur, mais nécessite une capture
passive du trafic — incompatible avec l'architecture actuelle du scan
(sondes actives par IP). Notée comme limite plutôt que contournée à
moitié.

## [2.18.22] - 2026-08-20 📡

### 📡 Fin de l'audit architecture : identification réseau, SNMP, baie de brassage

Suite directe de la 2.18.21 (5 correctifs rouges), les points orange et
jaune restants de l'audit architecture, plus un retour d'usage réel sur la
précision du scan.

**Modale de sélection client (scan) et 3 correctifs orange**
- Modale de choix du client : une règle CSS globale (`input{width:100%}`)
  étirait les boutons radio, créant un grand vide entre la puce et le
  libellé — corrigé, liste défilante conservée au-delà de 260px.
- Suppression d'une entrée d'historique / vidage des erreurs : ne
  vérifiaient que la connexion, jamais les droits d'écriture — un
  utilisateur en lecture seule pouvait purger le journal.
- Widget « Alertes critiques » du dashboard : seuil de 30 jours en dur au
  lieu du délai configuré (garantie_alerte_jours), et ignorait le drapeau
  « ignorer cette alerte » — un appareil mis en sourdine réapparaissait
  quand même.
- Notifications de maintenance : la vérification anti-doublon ne
  regardait que « notifiée aujourd'hui », donc une maintenance dans sa
  fenêtre de préavis de 3 jours recevait un email par jour au lieu d'un
  seul.

**Fin du doublon de représentation appareil ↔ périphérique**

La colonne historique `peripheriques.appareil_id` coexistait avec la
table pivot N:N `peripheriques_appareils`, jamais nettoyée après une
migration à moitié faite. Elle n'est désormais plus jamais écrite (le
dédoublonnage USB du collecteur, qui s'appuyait dessus, a été reporté sur
la table pivot) ; l'historique ne journalise plus de faux écarts sur ce
champ ; la suppression d'un périphérique nettoie enfin les tables liées
(un oubli du correctif équivalent sur appareils/contrats en 2.18.21).

**Identification au scan réseau — retour d'usage réel**

Signalé : un MacBook ressortait comme « machine Linux », switches et
bornes WiFi n'étaient pas identifiés comme tels.
- Le TTL seul ne distingue pas macOS de Linux (les deux à 64 par défaut) :
  le fabricant MAC officiel Apple et le hostname mDNS typique
  (`...-MacBook-Pro.local`) affinent désormais la détection — un Mac n'est
  plus jamais classé « PC/Serveur (Linux) ».
- Un fabricant réseau reconnu (Ubiquiti, MikroTik, TP-Link, Netgear, Aruba,
  Zyxel, Ruckus, Meraki...) sans indice de sous-type dans le hostname
  tombe sur une catégorie réseau neutre plutôt que d'être systématiquement
  supposé « Routeur/Pare-feu » (cas réel : une borne ou un switch
  Ubiquiti/UniFi devenaient tous les deux « routeur »).
- NAS reconnu comme catégorie propre (n'était auparavant que « Serveur »).
- Détection réseau enrichie par **SNMP** (sysDescr/sysName) — absente
  jusqu'ici. Client SNMPv1 GET minimal, encodage BER écrit à la main
  (aucune dépendance ajoutée, même logique que la vérification DNS déjà
  construite à la main) : un switch ou une imprimante managés
  s'identifient désormais souvent dès le premier scan, sans configuration
  supplémentaire.
- Corrigé au passage : les valeurs renvoyées par le scan ne correspondaient
  pas toujours exactement à la liste canonique des types d'appareils — un
  type hors liste fait silencieusement retomber le sélecteur de la fiche
  appareil sur la première option au prochain enregistrement, effaçant le
  type détecté sans avertissement.

**Baie de brassage et lien croisé**

`parc_general` (résumé switch/routeur/UPS) et la baie de brassage
(position physique par U) décrivaient le même matériel sans jamais se
recouper. La fiche parc général affiche désormais un lien vers le plan
visuel de la baie avec le nombre d'emplacements réellement positionnés ;
et puisque le scan sait maintenant identifier marque/modèle des
équipements réseau, `/api/scan/importer` **suggère** (jamais n'impose)
d'ajouter à la baie tout équipement détecté qui n'y est pas encore — la
position physique dans le rack reste à saisir, elle ne peut pas être
déduite d'un scan réseau.

**Champ Utilisateur (fiche appareil)**

Texte libre rattaché aux fiches utilisateurs par comparaison de nom : une
coquille ou un surnom cassait le lien silencieusement, sans le moindre
signalement. Le champ reste du texte libre (aucune migration), mais
propose désormais une autocomplétion sur les utilisateurs connus du
client et affiche un avertissement quand la saisie ne correspond à aucune
fiche — avec la même normalisation (accents, casse, ordre Nom/Prénom) que
la résolution réelle côté serveur.

## [2.18.21] - 2026-08-20 🛡️

### 🛡️ 5 correctifs issus de l'audit architecture (scan, collecteur, cascades)

Suite à un audit complet de la logique de fonctionnement (obtention des
données, recoupement entre rubriques, sélection client, remplissage,
détection), 5 points concrets corrigés.

**Scan réseau — sélection explicite du client cible**

Le scan pouvait jusqu'ici importer ses résultats dans le mauvais client si
le sélecteur en barre supérieure n'était pas celui attendu. Une modale
demande désormais toujours le client cible au clic sur « Lancer le scan » :
sans client pré-sélectionné, sauf si un seul client correspond sans
ambiguïté au réseau physique actuel du poste ParcInfo (comparaison à
`parc_general.plage_ip_locale`, même détection que la surveillance ping
introduite en 2.18.20) — zéro ou plusieurs correspondances, rien n'est
pré-coché. Confirmer bascule automatiquement le client actif si besoin,
puis lance le scan.

**API collecteur — jeton dédié par client**

`/api/device-info` et les endpoints associés ne vérifiaient qu'un jeton
global unique, sans notion de client : un jeton valide donnait accès en
écriture à l'inventaire de **tous** les clients, pas seulement celui visé.
Un client peut désormais définir son propre jeton (fiche client, section
Sécurité), en plus du jeton global existant qui continue d'agir comme
passe-partout — sans rien casser pour les déploiements qui n'utilisent
qu'un jeton global unique, ou aucun jeton du tout.

**Suppression appareil/contrat — fin des lignes orphelines**

Les FOREIGN KEY déclarées dans le schéma (`ON DELETE CASCADE`/`SET NULL`)
n'étaient jamais appliquées : `PRAGMA foreign_keys` n'était activé que pour
la suppression d'un client, pas pour un appareil ou un contrat isolé.
Périphériques, documents, licences, clés de récupération BitLocker,
maintenances, emplacements de baie de brassage s'accumulaient donc en
lignes orphelines à chaque suppression. Corrigé : le pragma est désormais
activé sur ces suppressions aussi, complété d'un nettoyage manuel pour les
quelques colonnes ajoutées après coup sans FK déclarée (`cles_recuperation`,
`collectes`, `av_contrat_id`/`edr_contrat_id`/`rmm_contrat_id`).

**Collecteur — marque/modèle/n° série figés une fois renseignés**

Contrairement à l'IP, l'OS, la RAM ou le CPU, la marque, le modèle et le
numéro de série d'une machine ne changent jamais dans son cycle de vie —
mais le collecteur les écrasait pourtant à chaque collecte, sans
vérification. Une détection mal formée (VM, matériel non standard) pouvait
ainsi effacer silencieusement une correction saisie à la main. Ces trois
champs ne sont désormais renseignés que s'ils sont encore vides ; tout le
reste (IP, MAC, OS, RAM, CPU, stockage, antivirus...) continue de se
resynchroniser à chaque collecte, exactement comme avant.

**Audit trail — imports enfin journalisés**

L'import des résultats de scan réseau et les imports CSV en masse
(appareils, périphériques) ne laissaient aucune trace dans l'historique :
un appareil pouvait apparaître ou changer sans qu'on sache jamais dire si
c'était via ces imports. Chaque création et mise à jour issue de ces trois
chemins est désormais journalisée, au même titre que la fiche appareil et
le collecteur.

## [2.18.20] - 2026-08-20 🔍

### 🔍 Scan réseau plus précis, surveillance ping limitée au bon client

Suite à une revue du code de détection matériel réseau, deux volets.

**Scan réseau (page Scan) plus précis**
- Les données WMI (marque, modèle, n° série, RAM, CPU, disque) étaient
  calculées mais jamais écrites à l'import — corrigé, sans jamais écraser
  une valeur déjà saisie à la main.
- Bannières de service sur les ports déjà trouvés ouverts (salutation SSH/
  FTP/SMTP/POP3/IMAP, en-tête Server + titre de page HTTP/HTTPS) pour
  affiner marque/type détectés.
- Découverte UPnP élargie à tout le segment (`ssdp:all`), pas restreinte à
  la box Internet comme côté collecteur — NAS, TV connectées, serveurs
  média identifiés avec leur vraie marque/modèle.
- mDNS élargi à 9 types de service courants (imprimantes IPP, Apple
  AirPlay, Chromecast, SMB, AFP) — `zeroconf`, déjà une dépendance, ne
  servait jusqu'ici qu'à retrouver d'autres instances ParcInfo.

**Surveillance ping limitée au bon client**

Signalé en usage réel : ParcInfo tourne souvent sur un poste qui se
déplace physiquement d'un client à l'autre (portable technicien). La
surveillance ping en tâche de fond pingeait jusqu'ici tous les appareils
de tous les clients à chaque cycle — dès qu'on quitte le réseau d'un
client, ses appareils passaient à « hors ligne » non pas parce qu'ils le
sont, mais parce que le ping échoue depuis un réseau différent.

Corrigé par détection automatique du réseau courant : chaque cycle
détermine les réseaux locaux actuels de ce poste (toutes interfaces
actives) et ne ping que les appareils plausiblement sur l'un d'eux — les
autres gardent leur dernier statut connu, jamais écrasé par un faux
« hors ligne ». Complété ensuite, signalé à raison : deux clients à la
même plage IP par défaut (`192.168.1.0/24`, très courant) ne pouvaient
pas être départagés par la seule IP — ajout d'une vérification par
adresse MAC (vue via ARP) qui identifie sans ambiguïté quelle machine
répond vraiment, avant de marquer quoi que ce soit.

## [2.18.19] - 2026-08-20 📡

### 📡 IP publique par appareil, options DNS/box internet au collecteur, curseur de luminosité corrigé

**Ajouté**
- IP publique et opérateur (FAI) collectés par appareil (`collector_core.
  get_public_ip_info()`), distinct du champ site `parc_general.ip_publique`
  (saisi à la main) : utile pour un poste itinérant dont l'IP publique
  change selon le lieu de connexion. Toujours resynchronisée à chaque
  collecte, comme `adresse_ip`. Affichée sur la fiche appareil, la fiche
  système et le rapport PDF.
- Vérification DNS (dnscheck.tools), option décochée par défaut (case à
  cocher / `--dns-check`) : requête DNS construite à la main (aucune
  dépendance ajoutée), interrogée sur le résolveur réellement configuré
  sur le poste — pas un résolveur public arbitraire. Résultat affiché
  **brut**, sans verdict OK/KO fabriqué : dnscheck.tools ne documente pas
  assez précisément l'interprétation de ses variantes (ECS, DNSSEC) pour
  qu'un tel verdict soit fiable.
- Infos de la box internet via découverte UPnP, option décochée par défaut
  (case à cocher / `--router-info`) : fabricant, modèle, IP WAN — best-effort,
  beaucoup de box grand public désactivent UPnP par défaut.

**Corrigé**
- Le curseur de luminosité des textes secondaires (ajouté en 2.18.17) ne
  persistait pas au changement de page. L'override CSS n'existait que côté
  JavaScript (prévisualisation en direct pendant qu'on bouge le curseur) —
  jamais ajouté à la génération du CSS injecté au chargement de chaque page
  côté serveur, contrairement aux autres préférences (accent, contraste).

## [2.18.18] - 2026-08-20 🍎

### 🍎 Deux régressions macOS corrigées, signalées en usage réel (Mac Intel)

Aucune des deux ne touchait Windows/PC.

**Corrigé**
- Synchronisation Turso en échec juste après une mise à jour
  (`CERTIFICATE_VERIFY_FAILED : unable to get local issuer certificate`,
  sur tous les push — `user_preferences`, `config`, `historique`,
  `appareils`). Cause : `launcher.py` pose `SSL_CERT_FILE` avec
  `os.environ.setdefault()`, en pointant vers le `cacert.pem` de son
  propre `_MEIPASS` — mais `applique_maj.environnement_propre()` (le
  correctif 2.18.14 pour `_MEIPASS2`) ne filtrait pas cette variable :
  le nouveau process héritait donc de la valeur de l'ANCIENNE instance,
  pointant vers un dossier temporaire supprimé dès qu'elle se termine
  juste après — `setdefault()` ne corrigeait alors jamais ce chemin
  devenu invalide. Corrigé aux deux endroits : affectation directe dans
  `launcher.py` (le chemin recalculé par CE process doit toujours
  l'emporter), et `SSL_CERT_FILE` ajouté aux variables filtrées lors de
  la relance.
- Après une mise à jour, ParcInfo redémarrait sur un port différent de
  3456 et y restait bloqué en permanence. Cause : le lancement direct
  macOS démarre volontairement la nouvelle instance AVANT d'arrêter
  l'ancienne (vérification introduite en 2.18.1, pour éviter qu'une mise
  à jour ratée ne tue les deux instances à la fois) — l'ancienne garde
  donc le port 3456 jusqu'à ~12 secondes après le démarrage de la
  nouvelle. `launcher.get_port()` n'avait aucune patience : port occupé
  → bascule aussitôt sur un port au hasard, sans jamais y revenir
  ensuite. Corrigé sans changer le comportement des lancements normaux
  (deux instances légitimes qui se partagent des ports distincts, cas
  courant avec Docker/PC/Mac) : la relance après mise à jour se signale
  désormais elle-même (`PARCINFO_RELANCE_MAJ`), et `get_port()` ne
  patiente (jusqu'à 15s) que dans ce cas précis.

Les deux corrigés avec un test dédié (`test_launcher_port.py` pour le
port, assertions supplémentaires dans `test_maj_macos_architecture.py`
pour les deux).

## [2.18.17] - 2026-08-19 🎨

### 🎨 Curseur de luminosité, ligne version corrigée, fiche appareil réparée

**Ajouté**
- Curseur de luminosité des textes secondaires/atténués (Paramètres →
  Apparence & Couleurs → Luminosité des textes secondaires) : éclaircit ces
  textes en mode sombre, les fonce en mode clair — dans les deux cas pour
  augmenter le contraste avec le fond plutôt que le réduire. Complète le
  niveau de contraste existant (3 paliers fixes) par un réglage continu ;
  recalculé à partir de la même base (mode + niveau de contraste) à chaque
  changement, donc jamais cumulatif.
- Bouton d'annulation (↩) sur chaque entrée « Modification » de
  l'historique inline de la fiche appareil, identique à celui du journal
  global (`/historique`).

**Corrigé**
- En navigation latérale (Réglages → Menu de navigation → Vertical), la
  ligne « v2.18.16 ⓘ » faisait 50px de haut au lieu d'environ 33px : le
  lien ⓘ « à propos » est un `<a>`, visé par la règle générale
  `nav a { height: 50px }` pensée pour la barre horizontale (où 50px = la
  hauteur pleine de la barre). Dans la colonne étroite, cette hauteur
  imposée gonflait toute la ligne bien au-delà de son texte. Neutralisé en
  navigation latérale uniquement ; barre horizontale inchangée.
- Fiche appareil : l'historique des modifications restait bloqué sur
  « Chargement… ». Cause réelle : le script qui devait le charger était
  collé juste après `{% block title %}` sans balise `<script>` et sans
  fermer ce bloc — son corps finissait littéralement dans le texte du
  `<title>`, jamais exécuté comme JavaScript, et de toute façon jamais
  appelé. Déplacé au bon endroit (`{% block extra_scripts %}`), appel
  ajouté.
- Fiche appareil : la liste texte brut éditable des « ports ouverts
  détectés » (ex. `22,80,443`) faisait doublon avec les badges juste
  au-dessus, sans jamais avoir été destinée à l'édition manuelle (résultat
  d'un scan réseau) — passée en champ caché, la valeur continue de voyager
  avec le formulaire sans plus s'afficher deux fois.
- Fiche appareil : les badges de ports étaient tous gris uniformes.
  Reprennent maintenant les couleurs par type de service (SSH, HTTP,
  RDP...) déjà utilisées dans la liste des appareils, pilotables depuis
  Réglages → Noms des services.

## [2.18.16] - 2026-08-19 🧹

### 🧹 Audit complet du projet : code mort retiré, documentation corrigée

**Demandé explicitement** : vérifier tout le projet pour du code mort et
des incohérences, et s'assurer que la documentation est à jour. Recherche
exhaustive (chaque candidat confirmé par recherche croisée — imports, CI,
templates, JS — avant d'être retiré, aucune suppression sur simple
intuition).

**Code retiré, jamais appelé nulle part :**
- 7 scripts racine obsolètes : `validate_optimizations.py`,
  `test_optimizations.py`/`_v2.py` (nécessitaient un serveur en marche sur
  une URL codée en dur, jamais branchés à la CI, superseded par la suite de
  tests actuelle), `check_indexes.py`, `INTEGRATION_EXAMPLE.py` (tutoriel
  d'intégration déjà réalisée en direct dans `app.py`), `system_checker.py`,
  `build.py` (ancien orchestrateur, remplacé par des appels `pyinstaller`
  directs dans le pipeline CI actuel)
- 2 gabarits HTML jamais rendus par aucune route : `dashboard.html`
  (remplacé par `client_dashboard.html`/`user_dashboard.html`, séparés par
  rôle) et `rapport_maintenance_pdf.html` (remplacé depuis par une
  génération PDF directe via reportlab, `/rapport/maintenances/pdf`)
- Une fonction TTL par socket ICMP brut, explicitement remplacée par une
  autre — sa remplaçante documente déjà dans son propre docstring pourquoi
  l'approche par raw socket a été abandonnée
- Un import et une fonction jamais appelés, une clé de configuration
  fantôme (`theme`, jamais lue ni écrite — la personnalisation réelle passe
  par des clés de couleur individuelles)

**Documentation corrigée** (claude.md, README.md, BUILD.md,
CONTRIBUTING.md, IMPLEMENTATION_GUIDE.md, DOCKERHUB_SETUP.md,
COLLECTOR_FIELD_MAPPING.md) :
- claude.md se contredisait lui-même sur la sécurité des uploads (« aucune
  validation » alors qu'extension, signature de fichier et taille sont
  bien vérifiées) ; référence de ligne fausse pour le middleware CSRF ;
  liste de dépendances tronquée (3 citées sur 11 réelles) ; lien mort vers
  `LICENSE.md` (le fichier s'appelle `LICENSE`)
- README.md gardait des URL placeholder (`your-org`) à côté de liens
  fonctionnels vers le vrai dépôt — même fichier, deux origines différentes
- BUILD.md affirmait l'inverse du code actuel sur deux points (UPX
  activé — en réalité désactivé pour réduire les faux positifs antivirus ;
  icône « à décommenter » — en réalité déjà active) et décrivait un
  binaire macOS « universel » qui n'existe pas (deux builds séparés,
  ARM et Intel) ; recommandation `codesign --deep` retirée (cas documenté
  de signature cassée sur bundle complexe, voir 2.17.1)
- CONTRIBUTING.md envoyait les contributeurs créer leur branche depuis
  `main` — la vraie branche par défaut est `master`
- IMPLEMENTATION_GUIDE.md affirmait l'authentification du collecteur « pas
  encore implémentée » alors qu'elle l'est ; commandes de téléchargement
  produisant un fichier corrompu (la route sert une archive ZIP, pas un
  script `.py` seul)

**SYNOLOGY_DEPLOYMENT.md réécrit** — son affirmation centrale était devenue
l'inverse de la réalité. Le document expliquait qu'un passage à Gunicorn
avait corrigé des soucis Hyper Backup/Centre de paquets sur Synology. En
réalité (retracé dans l'historique Git) : Gunicorn avait bien été
introduit (v2.6.1), mais ses workers plantaient avec le code 255 — cause
plus grave que le problème initial — et le code est revenu à Werkzeug
directement dès la v2.6.6, stable depuis. `docker-entrypoint.sh` installe
encore `gunicorn` par précaution mais ne l'invoque jamais.

**SOLUTION_403_VERSION.md archivé** dans `docs/archive/` (convention déjà
en place dans le projet) — incident pleinement résolu depuis plus de 12
versions. Sa seule information encore utile (pourquoi `/api/*` est exempté
de la vérification CSRF) reprise sous forme de note permanente dans
claude.md.

## [2.18.15] - 2026-08-19 ⚡

### ⚡ Inventaire des appareils allégé

Signalé en usage réel : l'inventaire un peu lent à s'afficher avec une
soixantaine d'appareils. La requête faisait `SELECT a.*`, ramenant sur
**chaque page** de la liste toutes les colonnes de la table `appareils` —
y compris `rapport_systeme_json` et `logiciels_installes_json`, qui peuvent
peser jusqu'à 1 Mo **chacune** une fois remplies par une vraie collecte
(icônes d'applications, liste complète des logiciels installés...). La
liste d'inventaire ne les affiche pourtant jamais — seule la fiche détail
d'un appareil les utilise.

La liste de colonnes de la requête est désormais construite dynamiquement
(via `PRAGMA table_info`, mise en cache) en excluant ces deux blobs,
plutôt qu'une énumération figée à la main qui se démoderait silencieusement
à la prochaine colonne ajoutée à `appareils` (une table qui, comme le note
`claude.md`, continue de grandir). La fiche détail d'un appareil continue
de tout ramener comme avant, sans changement.

## [2.18.14] - 2026-08-19 🧹

### 🧹 Environnement nettoyé avant la relance directe (mise à jour macOS)

**Suite directe du correctif de bit exécutable (2.18.12).** Le journal
`parcinfo.log` a confirmé la disparition de l'erreur de permission — mais a
révélé un troisième problème : le nouveau process démarrait bel et bien
(un PID lui était attribué), puis se terminait aussitôt, code de sortie
255, sans autre explication.

**La cause, déjà connue et corrigée ailleurs dans ce même code.** Sans
`env=` explicite, `subprocess.Popen()` fait hériter le nouveau process de
l'environnement de **l'ancienne** instance, encore vivante à cet instant
précis. Or `_MEIPASS2` et les autres repères du lanceur PyInstaller pointent
alors vers l'extraction de l'ANCIENNE version — que le bootloader de la
NOUVELLE tente de réutiliser au lieu de faire sa propre extraction,
incohérent avec son propre contenu. Ce mécanisme était déjà identifié et
neutralisé côté Windows (`applique_maj.environnement_propre()`, utilisé par
`_install_windows` depuis le début) ; jamais repris lors du passage de la
relance macOS vers un lancement direct en 2.18.10.

**Le correctif** applique exactement le même traitement déjà éprouvé côté
Windows, plus un répertoire de travail explicite (le domicile utilisateur,
plutôt qu'hérité de l'ancienne instance). Vérifié par un test dédié qui
piège un repère de lanceur périmé dans l'environnement et confirme qu'il
n'atteint jamais le nouveau process.

> Troisième cause distincte trouvée et corrigée sur cette seule mise à jour
> macOS depuis que `parcinfo.log` existe (2.18.7) : translocation (2.18.10),
> bit exécutable (2.18.12), et maintenant l'environnement hérité — chacune
> confirmée par une preuve directe plutôt que devinée.

## [2.18.13] - 2026-08-19 🎨

### 🎨 Numéro de version et lien « à propos » sur une seule ligne

Signalé en usage réel : en navigation verticale (barre latérale, activable
dans Réglages → thème), le numéro de version et le lien ⓘ vers « à propos »
apparaissaient l'un sous l'autre au lieu d'être côte à côte — chaque enfant
direct de la barre de navigation y prend toute sa largeur, ce qui empilait
les deux au lieu de les aligner. Regroupés dans un même bloc, numéro à
gauche et ⓘ à droite comme demandé, désormais alignés sur une seule ligne
dans les deux modes de navigation (horizontale, déjà correcte, et verticale).

## [2.18.12] - 2026-08-19 🔓

### 🔓 Le vrai bug de la mise à jour macOS, enfin trouvé : bit exécutable perdu

**Ce que le journal a fini par révéler.** Le fichier `parcinfo.log` ajouté
en 2.18.7 a donné, cette fois, la preuve directe qui manquait depuis le
début de cette série : plus aucun blocage Gatekeeper, mais
`[Errno 13] Permission denied` sur l'exécutable fraîchement remplacé, au
moment précis du lancement direct introduit en 2.18.10.

**La cause, une fois qu'on la cherchait.** Le zip macOS est créé par
`zip -r` (voir les workflows de build), qui embarque correctement le mode
Unix de chaque fichier dans ses métadonnées. Mais `zipfile.ZipFile.
extractall()`, de la bibliothèque standard Python, **ignore silencieusement
cette métadonnée** — un gotcha connu et documenté de ce module. L'exécutable
ressortait donc de chaque extraction sans son bit `+x`. `open` (Launch
Services), utilisé jusqu'à la 2.18.9, tolérait apparemment cette absence ;
`subprocess.Popen()` directement sur le binaire (2.18.10), non.

**C'est le changement pensé pour régler la translocation qui a démasqué ce
problème.** Il était là depuis le tout début de cette série de correctifs,
cerné années durant par des diagnostics Gatekeeper qui n'en étaient jamais
la vraie cause — sans le passage au lancement direct (nécessaire pour
éliminer la translocation, voir 2.18.10), cette permission perdue serait
peut-être restée invisible indéfiniment.

**Le correctif.** Les permissions Unix de chaque fichier extrait sont
désormais restaurées depuis les métadonnées du zip (`ZipInfo.
external_attr`), avec un `chmod +x` de sécurité supplémentaire juste avant
la tentative de lancement, au cas où quoi que ce soit d'autre l'aurait
perdu en route. Vérifié par un test dédié qui reproduit exactement le
mécanisme (bit restauré pour un exécutable, jamais ajouté à tort à un
fichier ordinaire, aucune exception sur une entrée sans métadonnées Unix).

> À valider au prochain cycle de mise à jour réel — mais pour la première
> fois dans cette série, la preuve technique (le message d'erreur exact,
> pas une hypothèse) pointe directement vers ce correctif.

## [2.18.11] - 2026-08-19 🔎

### 🔎 Contrôle complet du système de synchronisation

**Demandé explicitement** : un audit du système de sync Turso — conflits,
cohérence, restes devenus inutiles. Lecture complète de `database.py` et de
toute la mécanique de triggers dans `app.py`. Quatre trouvailles concrètes,
toutes corrigées.

**La plus significative : la page « À propos » mentait sur l'état de la
synchronisation, exactement là où elle a été consultée plusieurs fois cette
série pour diagnostiquer un souci de sync.** Elle n'affichait le statut Turso
qu'en mode `db_type='turso'` (chaque requête interroge Turso directement,
sans base locale) — jamais en mode `'sync'` (base locale au quotidien,
synchronisée en tâche de fond), qui est le mode réellement utilisé par des
instances Docker/PC/Mac qui se synchronisent entre elles. Résultat : la page
affichait *« Turso n'est pas configuré — cette installation utilise
uniquement la base locale »*, alors que la synchronisation tournait
activement en arrière-plan. Corrigé pour se baser sur « autre chose que
`local` » plutôt que sur un seul mode particulier.

**Un risque de panne silencieuse permanente, corrigé avant qu'il ne se soit
jamais produit.** `_sync_applying` — la garde qui empêche les triggers de se
redéclencher pendant qu'un pull applique des données reçues — n'était jamais
purgée au démarrage de l'application. Un crash exact au mauvais moment
(coupure de courant, kill du process) entre la pose de cette garde et son
retrait laisserait la ligne survivre au redémarrage : plus aucun trigger ne
se déclencherait jamais plus, donc plus aucune modification locale ne
serait journalisée ni poussée vers les autres instances — sans la moindre
erreur pour le signaler. La ligne est maintenant systématiquement purgée à
chaque démarrage, une preuve suffisante qu'aucun pull n'est en cours.

**Nettoyage de code mort.** `_sync_deletions` et ses triggers `_trg_del_*` :
un mécanisme antérieur à `_sync_journal`, jamais relu par la synchronisation
actuelle, mais encore alimenté à chaque suppression sur 23 tables — avec une
liste restée figée à l'ancienne, jamais mise à jour contrairement à celle
qui alimente réellement la sync. Retiré.

**Documentation corrigée.** La docstring de `sync_once()` affirmait une
résolution de conflit par `date_maj` qui n'existe pas dans le code actuel
depuis le passage au journal de modifications. Corrigée pour décrire
honnêtement la vraie règle — le dernier écrit côté Turso gagne, ligne
entière, sans fusion des champs — et sa vraie limite, documentée pour la
première fois : deux instances qui modifient le **même** enregistrement
avant d'avoir chacune synchronisé verront l'une écraser silencieusement
l'autre. En usage normal (des appareils différents modifiés depuis des
instances différentes), cette situation ne se présente jamais ; elle reste
une limite réelle si la même fiche est éditée depuis deux instances au même
moment.

> `test_sync_turso.py`, qui couvrait déjà une partie de ce mécanisme, n'était
> jamais exécuté en intégration continue — corrigé au passage, avec deux
> nouvelles sections couvrant les deux correctifs ci-dessus.

## [2.18.10] - 2026-08-18 🚀

### 🚀 Mise à jour macOS relancée directement — fin (probable) de la saga Gatekeeper

**Reprise complète de la logique, documentation externe à l'appui.** Après
plusieurs correctifs incrémentaux sur la même mise à jour macOS bloquée
(2.16.1, 2.17.1, 2.18.1, 2.18.5, 2.18.6, 2.18.9), la cause a été confirmée
par recherche plutôt que par une nouvelle hypothèse non vérifiée : la
**translocation macOS** (Gatekeeper Path Randomization), un mécanisme
documenté et **distinct** de l'évaluation Gatekeeper elle-même.

Un bundle non notarié est copié par macOS vers un chemin aléatoire en
lecture seule dès que deux conditions sont réunies : porter encore une
trace de quarantaine, **et** être ouvert via Launch Services — Finder ou la
commande `open`. Notre mise à jour utilisait justement `open` pour relancer
l'application après remplacement. Le processus démarrait alors bel et bien,
mais depuis cette copie translocée plutôt que `/Applications/ParcInfo.app`
— invisible à la vérification par chemin exact (`pgrep`) mise en place en
2.18.1, expliquant les échecs signalés sans le moindre avertissement
Gatekeeper à partir de la 2.18.6 (`xattr`/signature/`spctl --add` tous
silencieux, car le bundle était bel et bien accepté).

**Le correctif : ne plus jamais passer par `open`.** Lancer l'exécutable
directement (comme depuis un terminal) élimine cette condition d'office,
quel que soit l'état de la quarantaine — confirmé par plusieurs sources
techniques indépendantes (voir liens ci-dessous). La relance utilise
maintenant `subprocess.Popen` directement sur le binaire, dont l'indicateur
de vie (`.poll()`) sert aussi de vérification de démarrage — plus fiable
qu'une recherche par chemin, puisqu'il vient directement du fork/exec et ne
dépend plus d'où macOS a pu faire tourner le process.

**Diagnostic permanent ajouté.** ParcInfo journalise désormais lui-même, à
chaque démarrage, s'il tourne depuis un chemin transloqué
(`sys.executable` contient « AppTranslocation ») — cette question ne devra
plus jamais être devinée après coup depuis un journal incomplet.

> Sources consultées pour ce correctif :
> [App Translocation — lapcatsoftware.com](https://lapcatsoftware.com/articles/app-translocation.html),
> [Untranslocating Apps — Synack](https://www.synack.com/blog/untranslocating-apps/),
> [Sparkle framework, documentation officielle](https://sparkle-project.org/documentation/).

## [2.18.9] - 2026-08-18 🔍

### 🔍 Diagnostic de translocation macOS (mise à jour toujours en échec)

**Ce que le journal de la 2.18.7 vient de révéler.** Premier vrai bénéfice
du fichier journal ajouté hier : le dernier échec de mise à jour sur le Mac
Intel suivi depuis plusieurs versions ne montre plus **aucun** avertissement
xattr/codesign/`spctl --add` entre le téléchargement et l'échec final —
signe que Gatekeeper a peut-être fini par accepter le bundle cette fois
(grâce au délai de grâce de la 2.18.6, ou à `spctl --add` de la 2.18.5).
Et pourtant, la vérification de démarrage échoue quand même.

**Piste la plus probable : la translocation macOS**, un mécanisme séparé de
Gatekeeper. Un bundle non notarié peut être exécuté par macOS depuis une
copie en lecture seule à un chemin aléatoire (`AppTranslocation/...`)
plutôt que `/Applications/ParcInfo.app` — le processus existerait alors
bien réellement, mais jamais au chemin exact que la vérification de
démarrage recherche jusqu'ici (`pgrep -f <chemin complet>`), la faisant
échouer à tort.

**Pas encore un correctif.** Après m'être trompé une fois cette série sans
preuve solide (le `--deep` de la 2.17.1), pas de nouvelle tentative à
l'aveugle : une recherche de secours (nom du processus seul, sans le
chemin) se déclenche maintenant quand la recherche stricte échoue, pour
distinguer clairement dans le journal les deux cas possibles — translocation
(l'app tourne ailleurs) ou échec de lancement pur et simple (rien ne tourne
du tout) — au prochain échec.

## [2.18.8] - 2026-08-18 🗑️

### 🗑️ Un appareil supprimé ne revient plus tout seul (ordre pull/push de la synchronisation)

**Signalé en usage réel, reproduit avec plusieurs appareils** (avec et sans
fiche système) : un appareil supprimé dans l'inventaire disparaissait bien
immédiatement sur toutes les instances (Docker/PC/Mac), mais réapparaissait
de lui-même après plusieurs cycles de synchronisation — sans qu'aucune
suppression manuelle n'ait eu lieu entre-temps.

**La cause : l'ordre PUSH puis PULL.** `_sync_using_journal` envoyait
d'abord le journal local vers Turso, avant de tirer ensuite les changements
distants. Une instance en retard (pas encore passée par un cycle de sync
depuis la suppression faite ailleurs) peut garder dans son propre journal
local une modification de cet appareil **datant d'avant** sa suppression —
localement, sur cette instance, l'appareil existe toujours. En poussant
avant de tirer, cette entrée périmée recréait l'appareil sur Turso (`INSERT
OR REPLACE`), avec un identifiant de journal **postérieur** à la suppression
d'origine — qui se propageait ensuite normalement, comme n'importe quelle
autre modification légitime, vers toutes les autres instances. La
suppression semblait alors s'annuler toute seule, plusieurs cycles plus
tard, sans qu'aucune action ne l'explique.

**Le correctif : tirer d'abord, pousser ensuite.** Une instance en retard
apprend ainsi la suppression et l'applique localement avant de pousser quoi
que ce soit — son entrée périmée ne trouve alors plus rien à lire localement
pour cet appareil (déjà supprimé par le pull qui vient de s'exécuter) et ne
pousse donc plus rien. Aucune logique de détection supplémentaire n'était
nécessaire : le simple réordonnancement suffit.

> Vérifié par un test dédié qui reproduit exactement le scénario signalé, et
> confirme — en simulant l'ancien ordre PUSH-avant-PULL sur les mêmes
> données — que l'appareil est bien ressuscité sans le correctif, et ne
> l'est plus avec.

## [2.18.7] - 2026-08-18 📋

### 📋 Fichier journal persistant pour les exécutables packagés

**Le vrai blocage n'était pas la mise à jour, c'était l'absence de preuve.**
En cherchant le détail exact d'un nouvel échec de mise à jour macOS (la
2.18.6 avait tout de même demandé le mot de passe administrateur, signe que
`xattr -cr` et la signature ad hoc avaient échoué malgré le délai de grâce),
`~/Library/Application Support/ParcInfo/_maj.log` — qu'on pensait être LE
journal de la mise à jour — s'est révélé introuvable. Pour cause : ce
fichier n'existe en réalité que sur **Windows**, écrit par le processus
séparé qui remplace l'exécutable verrouillé. Sur macOS, tout se passe dans
le processus déjà en cours d'exécution, qui n'a jamais utilisé ce mécanisme.

Pire en creusant : `logging.basicConfig()` n'écrit que sur la console —
or ParcInfo est construit **sans console** (`console=False`, pour ne pas
ouvrir une fenêtre noire à chaque lancement). Sur un exécutable packagé
(Windows ET macOS), tout ce que le code journalise part donc dans le vide,
sans qu'aucune fenêtre ne l'affiche ni qu'aucun fichier ne le conserve — y
compris le détail précis de chaque tentative de déblocage Gatekeeper
(`xattr`, signature, `spctl --add`) ajouté au fil de cette série de
correctifs. Un diagnostic qu'on croyait juste difficile à obtenir était en
réalité tout simplement perdu.

Ajout d'un fichier `parcinfo.log` (rotation automatique, 2 Mo × 3 copies) à
côté de la base de données, activé uniquement sur les exécutables packagés
— le mode source garde sa console existante. Le prochain échec de mise à
jour macOS pourra enfin être diagnostiqué avec le détail réel de chaque
étape, plutôt qu'en reconstituant des hypothèses sans preuve.

## [2.18.6] - 2026-08-18 ⏱️

### ⏱️ Délai de grâce avant d'abandonner xattr -cr (Gatekeeper macOS)

**La question qui a débloqué le diagnostic.** Signalée en usage réel : « un
script séparé qui ferait exactement ce que je fais à la main (télécharger,
remplacer, `xattr -cr`, lancer) ne fonctionnerait-il pas ? » En reconstituant
ce déroulé manuel commande par commande pour répondre, une différence
concrète est apparue — pas une question de « script séparé » ou non (`spctl`
évalue le fichier, pas qui l'appelle), mais de **timing** : un humain qui
tape ces commandes dans un Terminal laisse naturellement passer plusieurs
secondes entre `xattr -cr` et le lancement de l'app. Le code, lui,
enchaînait la vérification `spctl` immédiatement après le retrait de
quarantaine — potentiellement trop tôt pour que `syspolicyd` (le service
que `spctl` interroge) ait fini de la prendre en compte.

La vérification est maintenant retentée jusqu'à 5 fois, à une seconde
d'intervalle, avant de passer à la signature ad hoc — qui, elle, durcit
l'évaluation sur certains Mac plutôt que de l'assouplir (diagnostic de la
2.18.1). Si cette hypothèse est la bonne, `xattr -cr` seul devrait
désormais suffire dans la majorité des cas, sans jamais atteindre les
étapes plus intrusives (signature, `spctl --add`).

> Non confirmé sans Mac réel avant publication, comme les correctifs
> Gatekeeper précédents de cette série — à valider au prochain retour
> d'usage sur le Mac à l'origine du signalement.

## [2.18.5] - 2026-08-18 🔓

### 🔓 spctl --add en dernier recours pour le blocage Gatekeeper macOS (expérimental)

**Un troisième mécanisme, de nature différente.** Suite directe du blocage
Gatekeeper toujours ouvert sur le Mac Intel à l'origine du signalement : les
deux méthodes actuelles (`xattr -cr` puis signature ad hoc en repli) sont
toutes deux des *nettoyages* — elles espèrent que le bundle passera
l'évaluation de Gatekeeper sans jamais lui dire explicitement de faire
confiance. `spctl --add` fait l'inverse : il inscrit une exception en dur
pour ce bundle précis dans la base de confiance de Gatekeeper.

Tenté uniquement en dernier recours, après l'échec des deux méthodes
silencieuses — et pour cause : `spctl --add` modifie une base système
partagée, ce qui nécessite les droits administrateur. Demandés via
`osascript ... with administrator privileges`, qui déclenche l'invite mot
de passe/Touch ID native de macOS. La mise à jour cesse donc d'être
totalement silencieuse, mais uniquement dans ce cas précis (les deux
méthodes silencieuses ont déjà échoué). Une invite annulée par
l'utilisateur (erreur AppleScript -128) redescend proprement sur le message
d'échec déjà en place depuis la 2.18.1 — pas de plantage.

> **Expérimental, à valider en usage réel.** Apple a resserré `spctl --add`
> ces dernières années précisément pour empêcher ce genre d'auto-approbation
> par un logiciel — son comportement peut varier selon la version de macOS,
> et rien ne garantit qu'il lève le blocage constaté. Contrairement aux
> correctifs précédents de cette série, celui-ci n'a pas pu être vérifié
> avant publication faute d'accès à un Mac réel : le prochain retour
> d'usage sur le Mac concerné confirmera s'il fonctionne.

## [2.18.4] - 2026-08-18 🖥️

### 🖥️ Détection matérielle fiable sous Rosetta (mise à jour macOS)

**Le piège Rosetta.** Trouvé en creusant un blocage Gatekeeper signalé en
usage réel sur un Mac Intel : la sélection du binaire macOS à télécharger
(`_get_platform_key`) et le contrôle qui refuse d'installer une architecture
incompatible (`_install_macos`) s'appuyaient tous les deux sur
`platform.machine()` — qui reflète l'architecture du **processus en cours**,
pas celle de la puce. Sous Rosetta (la traduction automatique de macOS), un
exécutable Intel tournant sur un Mac Apple Silicon y répond « x86_64 »
indéfiniment. Concrètement : un Mac Apple Silicon qui se serait un jour
retrouvé avec le binaire Intel (ancien bug de sélection déjà corrigé en
2.15.2/2.16.2, téléchargement manuel malencontreux...) restait piégé dessus
pour toujours — chaque mise à jour reconfirmait « Intel » comme architecture
attendue et retéléchargeait le même binaire, sans jamais basculer vers l'ARM
natif qui aurait dû tourner nativement depuis le début.

Remplacé par une lecture matérielle directe (`sysctl hw.optional.arm64`),
qui répond correctement même pour un processus traduit par Rosetta, avec
repli sur `platform.machine()` en cas d'échec (Mac Intel authentique, où
cette commande sysctl échoue naturellement).

> Vérifié sur le Mac Intel à l'origine du signalement : il s'agit bien de
> matériel Intel authentique, pas d'un piège Rosetta — ce correctif ne
> change donc rien pour ce cas précis, mais protège tout futur poste Apple
> Silicon du même piège. Le blocage Gatekeeper de ce Mac reste ouvert : les
> deux stratégies automatisées actuelles (quarantaine seule via `xattr -cr`,
> puis signature ad hoc en repli) sont toutes deux rejetées par `spctl` sur
> cette machine — limite structurelle sans signature Apple reconnue
> (Developer ID + notarisation), qu'aucun script ne peut garantir de
> contourner. Le comportement de repli déjà en place depuis la 2.18.1 reste
> la meilleure protection réaliste : en cas d'échec, l'ancienne version
> continue de tourner sans rien perdre, avec un message clair invitant au
> geste manuel habituel.

## [2.18.3] - 2026-08-18 🔍

### 🔍 Découverte automatique des instances ParcInfo par le collecteur

**Le collecteur cherche maintenant les instances lui-même.** Suite directe
de la 2.18.2 (noms mDNS uniques par instance) : le collecteur GUI proposait
déjà un bouton « Scan Network » qui balayait le sous-réseau /24 local sur le
port 3456, mais uniquement en dernier recours et sans exploiter les
informations mDNS désormais fiables. La découverte combine maintenant deux
méthodes, dans l'ordre :

1. **mDNS** (immédiate, quelques secondes) : retrouve nom de poste, version
   affichée et badge Docker pour chaque instance qui s'annonce sur le réseau.
2. **Balayage de sous-réseau** (méthode existante, en complément) : reprend
   automatiquement pour toute adresse non trouvée en mDNS — pare-feu qui
   bloque le multicast, ancien poste, etc.

Les instances trouvées sont proposées dans une liste à choisir avant tout
transfert de données collectées, avec leur nom et version affichés plutôt
qu'une simple adresse IP.

> À savoir, sans changement depuis la 2.18.2 : une instance Docker en réseau
> « bridge » (le mode par défaut de `docker-compose.yml`) n'est généralement
> pas jointe par mDNS ni par le balayage de sous-réseau, son adresse IP
> n'étant pas celle de la machine hôte sur le réseau local. La sélection
> d'URL manuelle reste nécessaire pour une instance Docker dans ce cas.

## [2.18.2] - 2026-08-18 📡

### 📡 Détection multi-cartes réseau du collecteur + noms mDNS uniques

**Le collecteur retrouve le bon client.** Signalé en usage réel : la
présélection automatique du client déjà connu d'une machine ne fonctionnait
plus. La vérification rapide au démarrage du collecteur GUI utilisait
`get_mac_address()` (`uuid.getnode()`), qui n'a aucune notion de « la bonne »
carte réseau — sur un poste de développement réel, cette fonction a renvoyé
l'une de **sept** adresses MAC différentes (carte physique, VirtualBox,
Hyper-V/WSL), et c'est une carte **déconnectée** qui a été choisie, pas celle
réellement enregistrée côté serveur. Cette dernière vient d'un mécanisme
différent et plus sophistiqué (`_meilleure_carte_physique`, qui choisit la
carte physique connectée) exécuté seulement pendant la collecte complète —
jamais repris par cette vérification rapide, volontairement immédiate pour
ne pas attendre la minute que prend la collecte. Le collecteur envoie
maintenant toutes les adresses MAC visibles localement
(`get_all_mac_addresses`), le serveur reconnaît la machine sur n'importe
laquelle d'entre elles plutôt que sur une seule devinée au hasard.

**mDNS : un nom par instance.** Trouvé en creusant le même sujet, sans
rapport direct : chaque instance ParcInfo s'annonce sur le réseau local via
mDNS pour permettre sa découverte automatique — mais toutes s'annonçaient
sous le **même nom** (`"ParcInfo._http._tcp.local."`). Plusieurs instances
sur un même réseau (Docker + PC + Mac, par exemple, exactement le cas d'un
retour d'usage réel) entraient donc en conflit : une seule restait
visible, les autres invisibles à la découverte réseau, sans la moindre
erreur pour le signaler. Chaque instance a maintenant un nom unique (nom de
poste inclus) et publie sa vraie version dans ses informations mDNS (la
valeur était figée à « 2.6.22 » depuis des versions).

> À savoir : un conteneur Docker en réseau « bridge » (le mode par défaut de
> `docker-compose.yml` fourni) ne relaie généralement pas le trafic
> multicast mDNS vers le réseau local — ce correctif rend la découverte
> fiable entre plusieurs instances PC/macOS, mais une instance Docker en
> réseau bridge restera probablement invisible à la découverte automatique
> tant qu'elle n'est pas basculée en réseau `host`. Poursuite en cours sur
> la fonctionnalité de découverte automatique complète demandée en usage réel.

---

## [2.18.1] - 2026-08-17 🔓

### 🔓 Mise à jour automatique macOS enfin fiable + synchronisation qui ne perd plus rien

**Mise à jour macOS.** Après deux tentatives précédentes (2.16.1, 2.17.1) sur
le même Mac Intel encore bloqué par « Impossible d'ouvrir l'application
ParcInfo », un vrai diagnostic (`spctl -a -vv`, `codesign -dv --verbose=4`,
`xattr -l`, tous trois exécutés directement sur la machine qui échoue) a
enfin donné la réponse : `xattr -l` ne montrait plus aucune quarantaine, et
`codesign -dv` montrait une signature ad hoc parfaitement valide (pas de
corruption, contrairement à l'hypothèse de la 2.17.1 sur `codesign --deep`)
— et pourtant `spctl -a` rejetait toujours le bundle. La réparation manuelle
qui fonctionne, elle, ne signe jamais rien : juste `xattr -cr` après un
remplacement. C'est la signature ad hoc elle-même, sans Team ID ni
notarisation, qui durcit l'évaluation de Gatekeeper sur cette version de
macOS — un bundle nu passe, un bundle signé ad hoc sans identité de
confiance est jugé et recalé. La 2.16.1 avait pourtant rapporté l'inverse
(bundle non signé bloqué). Les deux peuvent être vraies selon la version de
macOS : `_debloquer_gatekeeper_macos()` ne suppose plus rien et vérifie
avec `spctl --assess` à chaque étape — la signature ad hoc n'est tentée que
si le simple retrait de quarantaine ne suffit pas.

Deuxième défaut trouvé en creusant le même mécanisme : l'ancienne instance
était arrêtée dès que le fichier était remplacé sur disque, sans jamais
vérifier que la nouvelle version démarrait réellement. Un blocage Gatekeeper
faisait donc disparaître les deux à la fois — l'ancienne tuée, la nouvelle
jamais ouverte, rien ne tournait, et rien n'expliquait pourquoi. La nouvelle
version doit maintenant prouver qu'elle a démarré (jusqu'à 10 secondes
d'attente) avant que l'ancienne soit arrêtée ; en cas d'échec, l'ancienne
continue de tourner et l'erreur précise s'affiche dans l'interface, en plus
d'être journalisée (visible depuis n'importe quel poste, la table étant
synchronisée entre instances).

**Synchronisation entre postes.** Signalé sans rapport avec ce qui précède :
les fiches système ne se synchronisaient plus entre plusieurs postes
ParcInfo, sans aucune erreur visible. Le curseur de lecture de la
synchronisation (`_sync_using_journal`, pull Turso → local) avançait
jusqu'à la fin du lot reçu même quand une table de ce lot avait échoué à
s'appliquer — l'entrée en échec n'était donc plus jamais retentée, perdue en
silence, et l'erreur elle-même ne survivait que le temps du cycle où elle
s'était produite avant de disparaître de l'état affiché. Le curseur ne
dépasse plus jamais la plus ancienne entrée en échec d'un lot : elle est
relue et retentée au cycle suivant, indéfiniment jusqu'à réussir — exactement
le comportement déjà en place côté push, maintenant symétrique côté pull.
Couvert par un nouveau test dédié (`test_sync_curseur_echec.py`).

---

## [2.18.0] - 2026-08-17 🎨

### 🎨 Vraies icônes d'applications, statut de mise à jour tri-état, correctif antivirus

**Vraies icônes d'applications.** La fiche système affichait jusqu'ici un
émoji approximatif déduit du nom de chaque application (navigateur, client
mail, type de fichier) — un même mot-clé produisant parfois un icône trompeur
pour deux logiciels sans rapport. Le collecteur extrait désormais la vraie
icône depuis l'exécutable lui-même (`ExtractIconEx`, Win32), en PNG 32×32
encodé en base64, à partir de la clé de registre `DefaultIcon` de chaque
ProgId. Au passage, une colonne de largeur fixe pour l'icône corrige un
défaut d'alignement signalé : la largeur variable des émojis faisait démarrer
les noms d'application à des positions différentes d'une ligne à l'autre.

Deux bugs trouvés en testant sur un poste réel, tous deux invisibles sans
exécution effective : un index d'icône négatif (identifiant de ressource,
ex. `-9403` pour Outlook) était converti en positif avant l'appel à
`ExtractIconEx`, qui gère pourtant nativement les deux conventions —
extraction silencieusement vide dans ce cas. Et le lecteur de registre
`HKCR:` n'existe pas dans le contexte non interactif où tourne le
collecteur (`powershell -NoProfile -NonInteractive`, seuls `HKCU:`/`HKLM:`
y sont montés) — bug préexistant qui, en plus de bloquer l'extraction
d'icône, faisait déjà silencieusement échouer la résolution du nom lisible
de certaines associations de fichiers (« Acrobat.Document.DC » affiché au
lieu de « Document Adobe Acrobat »). Les deux corrigés : index transmis tel
quel, `Registry::HKEY_CLASSES_ROOT\...` à la place de `HKCR:\...`.

**Statut de mise à jour tri-état.** Depuis la 2.17.0, un logiciel sans mise
à jour disponible et un logiciel simplement non suivi par le gestionnaire de
paquets affichaient tous deux un tiret — impossible de distinguer les deux.
Sur Windows, `winget list` est désormais recoupé avec `winget upgrade` : un
logiciel présent dans la liste complète avec une source réelle mais absent
des mises à jour est maintenant marqué « à jour » avec confiance, plutôt que
de rester muet. Sur Linux, `installed_software` provient déjà du même
gestionnaire que celui interrogé pour les mises à jour (apt/dpkg, dnf/rpm,
pacman partagent chacun leur propre base) : même principe, sans appel
supplémentaire. macOS reste volontairement sans ce statut — son inventaire
mélange /Applications, `pkgutil` et Homebrew sans distinguer leur origine,
rien ne garantirait la fiabilité d'un « à jour » affirmé à tort. Résultat
sur un poste réel : 208 logiciels avec un statut définitif (106 mises à jour
disponibles + 102 confirmés à jour), contre 106 seulement avant.

**Autres ajustements de la fiche système** (retour d'usage) : identifiants
RDP enregistrés affichés en badges plutôt qu'en liste texte séparée par des
virgules ; dix processus les plus gourmands listés au lieu de cinq.

**Correctif antivirus sur le collecteur.** Windows Defender signalait les
deux exécutables du collecteur (`Trojan:Win32/Sabsik.TE.A!ml`, une détection
heuristique, pas une signature connue) — déjà résolu une première fois pour
`ParcInfo.exe` par le commit `4f07720` (désactivation de la compression UPX,
ajout d'un manifeste Windows de confiance), mais jamais appliqué aux deux
specs du collecteur, restées avec `upx=True`. UPX est un déclencheur connu
de faux positifs : les logiciels malveillants l'utilisent aussi pour
échapper à la détection par signature, ce qui rend tout exécutable non signé
compressé avec UPX suspect aux yeux des moteurs heuristiques. Même correctif
appliqué aux deux specs, vérifié par une reconstruction réelle des deux
exécutables. Le fichier déjà publié en 2.17.1 reste flagué tel quel ; cette
version republie des binaires propres.

---

## [2.17.1] - 2026-08-17 🍏

### 🍏 Mise à jour automatique macOS : application « endommagée » sur Mac Intel

Signalé en usage réel juste après la 2.17.0 : sur Mac Intel, l'application
remplacée par une mise à jour automatique refusait ensuite de se lancer
(« impossible d'exécuter cette application »), alors qu'un remplacement
manuel de l'archive téléchargée, suivi d'un simple `xattr -cr`, fonctionnait
normalement — signe que le blocage ne venait pas de la quarantaine du
navigateur (déjà levée par les deux méthodes) mais d'une étape propre au
mécanisme automatique.

La différence : après remplacement, `_debloquer_gatekeeper_macos()` pose une
signature ad hoc (`codesign --sign -`, sans certificat Apple, introduite en
2.16.1) avec l'option `--deep` — qui re-signe récursivement tout le bundle,
Python.framework et les bibliothèques embarquées par PyInstaller compris.
C'est un comportement connu et documenté : `codesign --deep` gère mal les
bundles complexes et peut produire une signature invalide au lancement,
sans qu'aucune erreur ne remonte à la signature elle-même (le programme
rapporte un succès, l'échec n'apparaît qu'à l'ouverture). `--deep` n'était
pas nécessaire : signer le bundle suffit à signer l'exécutable principal
qu'il référence (`CFBundleExecutable`), sans toucher aux composants
imbriqués — exactement ce que fait la réparation manuelle, qui n'appelle
jamais `codesign`.

`--deep` retiré. Correctif basé sur l'analyse du code et un comportement
`codesign`/PyInstaller bien documenté par ailleurs — aucun accès direct à
un Mac Intel pour reproduire le blocage exact avant correction, à confirmer
par un prochain retour d'usage réel.

---

## [2.17.0] - 2026-08-17 📀

### 📀 Mises à jour logicielles détectées + stockage revu disque par disque

**Mises à jour logicielles.** Le collecteur signale désormais, pour chaque
logiciel installé, si une version plus récente est disponible : winget sur
Windows, brew sur macOS, apt puis dnf/yum puis pacman sur Linux (le premier
gestionnaire présent qui répond). Le statut ne bascule jamais sur « à jour » :
un gestionnaire de paquets n'indexe qu'une partie de ce que remonte le
registre ou les listes système — bien des installations manuelles lui
échappent — et son silence sur un logiciel donné signifie « non vérifiable
par cette source », pas « à jour ».

Repéré et corrigé pendant un test en conditions réelles : winget n'a pas de
sortie machine-readable pour `upgrade`, et son en-tête de tableau est traduit
selon la langue de Windows (« Nom »/« Disponible » en français, pas
« Name »/« Available »). La première version, qui cherchait l'en-tête anglais,
ne le trouvait jamais sur un poste français et retournait silencieusement
zéro résultat — un faux négatif total, indétectable sans exécution réelle.
Le parsing repose maintenant sur la position des colonnes (stable d'une
langue à l'autre), pas sur leur intitulé, et gère aussi le second tableau que
winget affiche parfois (paquets nécessitant un ciblage explicite).

**Stockage : vue par disque physique.** La « vue d'ensemble » des partitions
mélangeait jusqu'ici tous les volumes de tous les disques dans un seul ruban
— impossible d'y voir la structure réelle du parc, ni de savoir quelle
partition appartenait à quel disque. Chaque disque a maintenant sa propre
carte (modèle, type SSD/HDD, santé SMART) avec ses partitions à l'intérieur,
lettrées ou non : réservée système (MSR), EFI et récupération apparaissent
désormais aussi, avec leur taille et leur type — jusqu'ici, la vue ne
montrait que la partition de données et laissait croire à de l'espace non
identifié là où il n'y en avait pas. Un code couleur fixe par type de
partition, identique sur tous les disques de la fiche (données en cyan,
réservé en violet, récupération en ambre, EFI en sarcelle, non attribué en
gris), distinct du pourcentage de remplissage — toujours affiché en texte
sur le segment de données, mais qui ne pilote plus sa couleur.

Deuxième bug trouvé par le même test réel : `Get-PhysicalDisk` n'a pas de
paramètre `-DeviceId` — l'erreur de liaison de paramètre était avalée par le
`try/catch` existant, laissant santé et type de disque à « Inconnu » pour
tous les disques sans jamais rien signaler. La correspondance disque
physique ↔ numéro se fait maintenant via une table construite une seule
fois plutôt qu'interrogée par disque (plus correct, et plus rapide).
macOS/Linux regroupent les partitions par nom d'appareil (`/dev/sda1` →
`sda`, faute d'équivalent WMI) ; le type de partition individuelle n'y est
pas disponible, `df` ne voyant que les systèmes de fichiers montés.

Une fiche déjà collectée avant cette version n'a ni l'un ni l'autre champ :
la détection de mises à jour reste absente et la vue stockage retombe
automatiquement sur l'ancien ruban plat, le temps d'une prochaine collecte.

---

## [2.16.3] - 2026-08-17 🔄

### 🔄 Correction d'une erreur de synchronisation Turso

Signalé en production : `⚠ push documents_appareils: SQLite error: UNIQUE
constraint failed: _sync_journal.tbl, _sync_journal.record_id,
_sync_journal.action`.

Le trigger `_trg_journal_*` qui journalise chaque écriture pour la
synchronisation utilisait `INSERT OR REPLACE INTO _sync_journal`, sur une
table portant `UNIQUE(tbl, record_id, action) ON CONFLICT REPLACE` depuis sa
toute première version (vérifié dans l'historique — aucune dérive de schéma
locale). Le message d'erreur lui-même le confirme : la contrainte est bien
détectée. C'est sa résolution `REPLACE` qui ne semble pas s'appliquer de
façon fiable une fois le trigger exécuté à distance par Turso — un écart de
compatibilité SQLite/libSQL plausible pour ce genre de mécanisme moins
courant, plutôt qu'une erreur de schéma des deux côtés.

Plutôt que de chercher à confirmer la cause exacte sans accès direct à
l'instance concernée, le trigger a été réécrit pour ne plus en dépendre :
`DELETE FROM _sync_journal WHERE …` suivi d'un `INSERT` simple, en deux
instructions distinctes dans le corps du trigger. Reproduit avec succès le
scénario exact de production (une clé déjà présente dans le journal, puis
une nouvelle écriture dessus) dans une nouvelle suite de tests.

Se corrige automatiquement sur les instances déjà déployées : le mécanisme
qui compare et remplace un trigger périmé sur Turso (introduit en 2.9.9)
détecte la nouvelle définition au prochain cycle de synchronisation, sans
action manuelle.

---

## [2.16.2] - 2026-08-16 🍏

### 🍏 Mise à jour macOS : le bon binaire (ARM/Intel) est enfin choisi

Le vrai coupable derrière le blocage Gatekeeper de la 2.16.1 — repéré grâce
à un nouveau test réel, cette fois avec le message exact de macOS : « n'est
pas prise en charge par ce Mac ». Ce message-là n'est pas un blocage
Gatekeeper (application non identifiée) mais une incompatibilité
d'architecture — le genre d'erreur qu'un Mac Intel renvoie quand on lui
donne un binaire ARM, qu'aucune traduction Rosetta ne peut faire tourner
dans l'autre sens.

`_get_platform_key()` — la fonction qui choisit quel fichier télécharger
dans `version.json` — renvoyait sans condition `'macos_app'` (le zip ARM)
pour tout Mac, alors que `macos_app_intel` existe dans `version.json`
depuis la 2.15.1 et n'était tout simplement jamais branché au sélecteur.
Un Mac Intel qui cliquait sur « Installer » recevait donc systématiquement
le mauvais binaire, remplaçait sa version fonctionnelle par une version
qui refuse de démarrer, et se retrouvait bloqué jusqu'à une réinstallation
manuelle.

Corrigé sur `platform.machine()` — l'architecture du *processus en cours*,
pas seulement de la puce : un exécutable Intel qui tourne sous Rosetta sur
un Mac Apple Silicon continue ainsi de se mettre à jour vers un binaire
Intel, sans jamais basculer sous le tapis vers l'ARM. Un filet de sécurité
complète le correctif : avant de toucher à la version actuelle,
l'architecture du binaire téléchargé est vérifiée directement (commande
`file`) ; en cas de désaccord, la mise à jour est refusée et l'ancienne
version — celle qui fonctionne — reste en place, plutôt que d'être
remplacée par une version cassée. Couvert par une nouvelle suite dédiée
(`test_maj_macos_architecture.py`) : sélection correcte du fichier par
architecture, refus effectif en cas de désaccord, installation normale en
cas d'accord.

---

## [2.16.1] - 2026-08-16 🔓

### 🔓 Mise à jour macOS : déblocage Gatekeeper renforcé

Signalé après un test réel : la mise à jour lancée depuis le bouton
« Installer » sur macOS remplace correctement l'application, mais celle-ci
pouvait rester bloquée par Gatekeeper au redémarrage — obligeant à relancer
`xattr -cr ParcInfo.app` à la main, alors que ce nettoyage était censé
tourner automatiquement.

En relisant `_install_macos()`, deux défauts : l'appel `xattr -cr` existait
déjà, mais son échec passait totalement sous silence (`check=False`, jamais
consigné dans les logs) — impossible de savoir s'il avait seulement rendu la
main sans rien faire. Et surtout : lever la quarantaine ne suffit pas
toujours. Sur les versions récentes de macOS, un bundle totalement non signé
(ParcInfo ne l'a jamais été — aucun certificat développeur Apple) peut
rester bloqué par Gatekeeper même sans attribut de quarantaine, parfois sans
même proposer « Ouvrir quand même » dans Réglages Système.

Les deux étapes sont désormais regroupées dans `_debloquer_gatekeeper_macos()`,
appliquée après chaque remplacement du bundle : nettoyage de quarantaine
*et* signature ad hoc locale (`codesign --sign -`, sans certificat, sans
coût), chaque échec journalisé plutôt qu'avalé en silence. La signature ad
hoc se dégrade proprement si les outils en ligne de commande Xcode ne sont
pas installés sur la machine (log seulement, jamais d'écran bloquant).

Cette piste est la plus solide identifiée par relecture du code — sans accès
à un Mac pour reproduire le blocage exact, elle reste à confirmer sur le
prochain déploiement réel via le bouton « Installer ».

---

## [2.16.0] - 2026-08-16 🖱️

### 🖱️ Raccourcis de clic + page À propos / Contrôle

**Raccourcis de clic.** Dans l'inventaire des appareils, cliquer le nom
ouvrait jusqu'ici… rien — il fallait passer par le bouton « Éditer ». Le nom
ouvre désormais directement la fiche système. Le même principe a été
étendu après un passage sur chaque liste de l'application, avec une
destination choisie au cas par cas plutôt qu'un copier-coller aveugle :

- **Périphériques** : marque/modèle → édition (aucune fiche détail dédiée
  n'existe pour ce type d'enregistrement)
- **Utilisateurs** : nom → page Droits, la vue la plus riche disponible,
  volontairement distincte du bouton « Éditer » déjà présent
- **Maintenances** : appareil/périphérique référencé → fiche système ou
  édition, selon le type
- **Plans de disposition** : nom du plan → éditeur de plan
- **Tableau de bord client** : widget « État réseau », par cohérence avec
  le reste du tableau de bord où tout est déjà cliquable

Volontairement laissés tels quels : services (bouton déjà collé au nom),
identifiants (ligne sensible, risque de conflit avec le bouton
presse-papier), clients (cliquer un nom y évoque plutôt « sélectionner »
que « éditer », et l'édition est parfois masquée selon le niveau
d'accès) — contrats et historique l'étaient déjà.

**Page « À propos / Contrôle ».** Nouvelle icône ℹ dans la barre du haut,
ouvrant une page qui affiche version, mode d'exécution, adresse, durée
depuis le démarrage, et le statut de synchronisation Turso en direct (sans
appel réseau supplémentaire — elle relit l'état déjà tenu à jour par le fil
de synchronisation existant). Pour un administrateur, sur un exécutable
Windows ou macOS, elle ajoute ce qui manquait clairement : un bouton pour
arrêter ParcInfo. Jusqu'ici, la seule façon d'y mettre fin proprement était
de tuer le processus — aucun souci sur Windows, où une icône de barre
système existait déjà, mais rien du tout sur macOS, où la barre système
reste désactivée (incompatibilité AppKit, déjà rencontrée lors du travail
sur le build Intel). Un bouton « Redémarrer » complète le tableau. Le menu
de la barre système Windows gagne les deux mêmes actions, plus un accès
direct à cette page.

Sur macOS, la relance passe par `open` sur le bundle `.app` plutôt que par
un appel direct au binaire interne — un détail qui compte : appeler le
binaire directement fait tourner ParcInfo comme un simple process Unix nu,
sans rattachement au Dock ni à Launch Services.

---

## [2.15.3] - 2026-08-16 🔐

### 🔐 Certificats HTTPS et emplacement des données sur macOS

Deux corrections issues du premier vrai test sur un Mac, une fois le binaire
Intel enfin capable de démarrer (2.15.2).

**Turso, et toute connexion HTTPS, échouaient avec
`CERTIFICATE_VERIFY_FAILED`.** `database.py` ouvre une
`http.client.HTTPSConnection` sans contexte SSL explicite ; un `pip install`
normal trouve ses certificats via le magasin système ou le script
« Install Certificates.command » du Python.org installé, mais un
exécutable PyInstaller n'a accès à rien de tel sur la machine de
l'utilisateur. `certifi` est désormais embarqué (`parcinfo.spec`) et
`SSL_CERT_FILE` pointé dessus au tout début de `launcher.py`, avant tout
import réseau — ce qui couvre également, au passage, la vérification de
mise à jour elle-même (mêmes symptômes possibles, juste pas encore
observés).

**Plus grave : la base de données, les uploads et la clé de chiffrement des
identifiants vivaient à côté de l'exécutable — c'est-à-dire DANS le bundle
`.app`.** Ce choix est sûr sur Windows, où une mise à jour ne remplace que
le fichier `.exe` lui-même (`applique_maj.py`). Sur macOS, «&nbsp;à côté de
l'exécutable&nbsp;» veut dire `Contents/MacOS/` — à l'intérieur d'un bundle
qu'`update_checker.py::_install_macos()` remplace ENTIÈREMENT à chaque mise
à jour (ancien bundle déplacé de côté, puis supprimé). Le premier clic sur
« Installer » depuis la bannière aurait supprimé la base, les documents
joints et la clé de chiffrement — sans elle, les identifiants déjà stockés
deviennent illisibles pour toujours, sans aucun moyen de les récupérer.

Corrigé en stockant ces données dans `~/Library/Application Support/ParcInfo`,
hors d'atteinte du bundle. Une migration automatique rattrape les
installations déjà en place (`launcher.py`, au démarrage) et protège même la
toute première mise à jour lancée depuis une version antérieure au correctif
(`update_checker.py`, juste avant la suppression de l'ancien bundle — sans
quoi la migration côté `launcher.py` arriverait trop tard, l'ancien bundle
ayant déjà disparu). Couvert par une nouvelle suite de tests dédiée
(`test_migration_donnees_macos.py`) : localisation correcte selon l'OS,
rattrapage effectif, non-rejeu si des données existent déjà à la nouvelle
adresse, et protection du chemin de mise à jour in-app lui-même.

---

## [2.15.2] - 2026-08-16 🐛

### 🐛 Correction du build macOS Intel (crash au lancement)

Le binaire Intel publié en 2.15.1 plantait au lancement — confirmé par un
retour utilisateur réel : traceback lancé depuis le Terminal sur un vrai Mac
Intel, montrant `TypeError: unsupported operand type(s) for |: 'type' and
'NoneType'` au chargement de `app.py`.

Cause : `app.py` porte des annotations de module comme
`_sync_thread: threading.Thread | None = None` — la syntaxe `X | None`
(PEP 604) ne s'évalue au runtime qu'à partir de Python 3.10. Le job de build
Intel créait son venv depuis `/usr/bin/python3`, le Python système de macOS
(Xcode Command Line Tools), qui est en 3.9 — alors que tous les autres jobs
du pipeline (`tests`, `build` Windows/ARM) installent explicitement 3.11 via
`actions/setup-python`. L'étape de vérification d'architecture ajoutée en
2.15.1 (`lipo -archs`) ne portait que sur les extensions compilées (.so), pas
sur la version de l'interpréteur lui-même — elle n'aurait donc jamais pu
détecter ce problème.

Corrigé en installant Python 3.11 via `actions/setup-python` avant de créer
le venv (python.org publie des installeurs macOS universels depuis 3.9.1,
donc `arch -x86_64 python3.11` obtient toujours un interpréteur Intel
authentique). Une nouvelle étape de CI (`import app` direct, quelques
secondes) valide désormais que le module se charge sous ce Python avant de
lancer les ~5 minutes de compilation PyInstaller — pour attraper ce genre de
régression avant qu'un utilisateur ne la découvre.

---

## [2.15.1] - 2026-08-16 🍎

### 🍎 Build macOS Intel dans les releases

Jusqu'ici, un Mac Intel n'avait aucun binaire natif : le tableau de
téléchargements renvoyait vers Docker ou `ParcInfo-Windows.exe` via WSL. Le
pipeline de release construit et publie désormais `ParcInfo-macOS-Intel.zip`
à chaque tag, au même titre que les binaires Windows et macOS ARM.

**Deux bugs de CI, découverts en cascade.** `macos-latest` est un runner
Apple Silicon depuis un moment ; produire du x86_64 dessus demande de
croiser sous Rosetta. Le job existant (`build-macos-intel.yml`, jusqu'ici
non relié à la release) forçait `arch -x86_64` sur la ligne de création du
venv, mais pas sur les `pip install` qui suivaient — chaque appel `pip`
tournait donc nativement en arm64, et des extensions compilées (Pillow,
puis l'extension Rust de `cryptography`) s'installaient arm64-only à
l'intérieur d'un venv censé être x86_64. Corrigé en préfixant chaque appel
`python`/`pip` individuellement, avec un forçage de tag de wheel explicite
pour `cryptography` (`--platform` + `--target`, sa résolution de tag sous
Rosetta restant erronée même une fois le process forcé). Une étape de
vérification (`lipo -archs`) échoue désormais en quelques secondes si
l'architecture d'une extension régresse, plutôt qu'après deux minutes de
build PyInstaller.

**Un second bug, plus sérieux, sous le premier.** Une fois la compilation
correctement croisée, le job construisait `app.py` directement plutôt que
`parcinfo.spec` — la seule recette qui embarque `templates/`, `static/`,
`version.json`, les imports cachés (`apscheduler`, `zeroconf`, `pystray`) et
les métadonnées du bundle macOS. L'app produite aurait démarré sur un
`TemplateNotFound`. Le job utilise maintenant `parcinfo.spec`, qui lit déjà
une variable `TARGET_ARCH` prévue pour ce cas — vérifié binaire par
binaire : `Contents/MacOS/ParcInfo` est bien un exécutable Mach-O x86_64,
`Info.plist` porte la bonne version, `launcher.py` est le point d'entrée
analysé (pas `app.py`).

---

## [2.15.0] - 2026-08-16 🩹

### 🩹 Corrections du collecteur d'après retour d'usage réel

Dix corrections issues d'un premier déploiement réel du collecteur (v2.14),
plutôt qu'un thème unique — regroupées ici parce qu'elles ont toutes été
signalées dans le même retour et corrigées ensemble.

**Collecte non auto-démarrée.** Le GUI lançait `_collect_info()` dès
l'ouverture, avant même que l'utilisateur ait pu cocher ses options (débit,
mots de passe Wi-Fi) — obligeant à cocher puis relancer manuellement. La
collecte ne démarre plus qu'au clic sur « Rafraîchir les infos » ; la
reconnaissance du client, elle, reste immédiate (elle lit l'adresse MAC
directement, pas `self.system_info`).

**Fuseau horaire lisible.** `Get-TimeZone` était lu via `.Id` (« Romance
Standard Time », un identifiant de registre Windows) plutôt que
`.DisplayName` (« (UTC+01:00) Bruxelles, Copenhague, Madrid, Paris »).

**MAC/IP « canoniques » choisies sur une carte physique connectée.**
`uuid.getnode()` et la résolution DNS du hostname, utilisés pour poser le
MAC/IP « de la machine », retournent la première interface que le système
d'exploitation énumère — souvent une carte virtuelle ou débranchée.
Vérifié empiriquement sur la machine de développement : le MAC posé était
celui d'un adaptateur Bluetooth PAN débranché, pas celui de la carte
Ethernet réellement utilisée. `_meilleure_carte_physique()` corrige
maintenant le MAC/IP après collecte à partir des cartes physiques
détaillées, en préférant une carte connectée avec IP assignée — sans
casser le cas (rencontré sur la même machine) d'un pont Hyper-V où la carte
physique partage son MAC avec une carte virtuelle qui, elle, porte l'IP.

**Jauges plus lisibles.** La partie non occupée d'une barre d'utilisation
était de la même couleur que le fond de la carte — quasi invisible. Elle
utilise maintenant `var(--border)`, un gris foncé qui reste net sans
dominer la partie occupée.

**Vue d'ensemble « table de partitions ».** Nouvel affichage sur la fiche
système, en complément des barres détaillées par volume : un seul bandeau
segmenté, chaque segment large proportionnellement à la capacité du volume
qu'il représente, coloré selon son niveau de remplissage — un coup d'œil sur
la répartition du stockage entre tous les volumes, là où les barres
existantes ne montraient qu'un volume à la fois.

**Règles de pare-feu avec direction.** `netsh advfirewall` était interrogé
sans préciser de sens, et le champ « Direction » du texte retourné s'est
révélé peu fiable pour la distinguer. La direction est maintenant connue
par construction (deux passes, `dir=in` puis `dir=out`) et stampée sur
chaque règle ; la fusion par nom devient une fusion par (nom, direction)
pour qu'un programme aux comportements entrant/sortant différents (constaté
sur AnyDesk, ZeroTier One, MSMPI-*) affiche deux lignes distinctes plutôt
qu'une seule ambiguë.

**Toutes les cartes réseau affichées.** La rubrique Réseau ne listait que
les cartes au statut « Up » ; une carte désactivée ou débranchée reste une
information d'inventaire utile. Toutes les cartes sont désormais
répertoriées, triées cartes physiques connectées en premier, les
déconnectées légèrement atténuées.

**Associations de fichiers étendues, avec icônes.** Au-delà du navigateur
et du client mail par défaut déjà collectés, le programme par défaut est
maintenant relevé pour PDF, TXT, LOG, JPG, PNG, DOCX, XLSX et CSV
(`file_type_defaults`, `_EXTENSIONS_SUIVIES`). Un nouveau filtre Jinja
`app_icon` associe une icône par mot-clé au nom du programme — sans
extraire l'icône réelle de l'exécutable, ce qui alourdirait chaque collecte
pour un simple confort visuel.

**Nom d'appareil rattrapé.** Un appareil créé avant que son hostname soit
connu restait nommé d'après son IP ou « Device-XXXXXXXX » indéfiniment. Une
collecte ultérieure corrige maintenant ce nom de repli dès que le vrai
hostname est connu — un nom déjà personnalisé par un technicien, lui,
n'est jamais remplacé.

**Ports ouverts en badges.** La fiche appareil affichait la liste des ports
détectés en texte brut au-dessus du champ éditable ; elle affiche
maintenant les mêmes badges (icône + numéro + nom de service) que la fiche
système et la liste des appareils, réutilisant les filtres Jinja globaux
`port_icon`/`port_name` déjà en place.

---

## [2.14.0] - 2026-08-16 🔀

### 🔀 Redirections de port locales (netsh portproxy)

Troisième et dernière pièce d'un même trio, complétant le pare-feu (v2.12,
ce qui est autorisé à ENTRER) et le fichier hosts (v2.13, la redirection par
NOM) : `netsh interface portproxy` redirige au niveau du PORT, un mécanisme
indépendant des deux autres et invisible dans l'un comme dans l'autre — un
classique de dépannage insoupçonné (« pourquoi se connecter au port 8080 en
local tombe sur autre chose »).

**Parsing sans libellés, cette fois.** Contrairement à `netsh advfirewall`
(v2.12), ce tableau n'a ni export XML ni libellés de champs à faire
correspondre en français/anglais — juste des colonnes à largeur fixe. Le
parsing s'appuie donc sur la structure plutôt que sur le texte : toute ligne
à exactement 4 jetons dont le 2ᵉ et le 4ᵉ sont des nombres est une
redirection, ce qui élimine l'en-tête et le séparateur sans avoir à
connaître leur texte exact — et fonctionne de fait dans une langue non
prévue à l'avance, sans liste à tenir à jour.

Généralement vide sur un poste ordinaire : aucun filtre de volume nécessaire,
contrairement aux règles de pare-feu. N'a pas pu être vérifié sur une
redirection réelle (aucune configurée sur la machine de développement, et en
créer une pour tester — même temporairement — reviendrait à modifier la
configuration réseau du poste, ce qui reste refusé même avec l'accord de
l'utilisateur) : construit sur le format documenté, stable depuis Windows
XP, avec un parsing volontairement tolérant (rejette silencieusement toute
ligne inattendue plutôt que de produire une entrée erronée). Nouveaux tests
sur un texte synthétique reproduisant fidèlement ce format ; rendu réel de
la fiche système avec des redirections simulées inspecté dans le navigateur
avant publication.

---

## [2.13.0] - 2026-08-15 🗺️

### 🗺️ Redirections du fichier hosts

Premier champ réseau du collecteur qui n'est pas spécifique à Windows :
`get_hosts_file_entries()` lit `/etc/hosts` (ou son équivalent Windows)
directement — pas de commande à lancer, ni PowerShell ni `netsh`, valable
tel quel sur les trois OS, câblé depuis `collect_system_info()` plutôt que
`_WIN_STEPS`.

**Le filtre.** Un fichier hosts réel accumule vite du bruit qui n'est pas
une redirection volontaire : `localhost` (IPv4 et IPv6), les entrées
`ip6-*` que Linux inscrit lui-même, la propre entrée `127.0.1.1 <hostname>`
que Debian/Ubuntu écrivent automatiquement, et des doublons exacts — sur la
machine de test, deux outils de gestion de hosts différents géraient
chacun leur propre copie des mêmes blocages, `192.168.1.37 ALTAIR` compris.
Ce qui reste est dédupliqué et marqué **local** (redirection vers une IP
nulle ou loopback — blocage publicité/licence/télémétrie, ou serveur de dev)
ou **réseau** (correspondance nom↔IP réelle, ex. un poste désigné par son
nom plutôt que redécouvert par DHCP à chaque fois).

Nouveaux tests : chaque catégorie de bruit par défaut est bien écartée
individuellement, un doublon exact ne compte qu'une fois, la distinction
local/réseau est correcte, et l'exclusion `127.0.1.1 <hostname>` ne
s'applique que si le nom de la machine est effectivement fourni — pas à
l'aveugle. Vérifié aussi sur le fichier hosts réel de la machine de
développement (57 lignes brutes → 21 redirections utiles) et un rendu de la
fiche système inspecté dans le navigateur avant publication.

---

## [2.12.0] - 2026-08-15 🧱

### 🧱 Règles de pare-feu autorisées, filtrées et fusionnées

Demandé avec une contrainte explicite : une mise en forme compacte et
lisible, quitte à écarter les règles « par défaut » pour tenir le volume —
sans savoir à l'avance ce que ça représentait. Vérifié avant d'écrire une
ligne de code : plus de 1500 règles entrantes brutes sur une machine de
développement ordinaire, dont l'écrasante majorité pilotée comme un bloc par
Windows (Découverte réseau, Partage d'imprimantes…) ou générée en masse par
Docker/Hyper-V (`HNS Container Networking - <GUID> - 0`, un nouveau jeu de
règles à chaque réseau de conteneur créé, jamais nettoyé).

**Deux détours techniques, découverts en creusant.** `Get-NetFirewallRule`
(le cmdlet PowerShell « propre ») exige l'élévation ; `netsh advfirewall
firewall show rule`, lui, fonctionne sans droits particuliers — mais n'a pas
d'export XML comme `netsh wlan` ou `gpresult`, donc retour au parsing de
texte localisé (labels bilingues FR/EN, certains — `LocalPort`, `Profiles`
— restant en anglais même sur un Windows français). Plus sournois : ce texte
n'est **jamais** en UTF-8, mais dans la page de code OEM active du système
(850 en français, 437 en anglais US…) — le décoder comme le reste de la
collecte aurait silencieusement corrompu tout libellé accentué. `GetOEMCP()`
donne la page de code réellement active plutôt que d'en parier une
(`_win_console_output()`).

**Le filtre.** Actives, autorisées, sans groupe de fonctionnalité Windows
(déjà résumé par `firewall_profiles`), sans nom généré automatiquement — ce
qui reste, ce sont les trous ouverts par des logiciels installés, la vraie
question d'audit. Les entrées de même nom (plusieurs protocoles, mises à
jour successives du même logiciel) sont fusionnées en une ligne. Résultat
sur la machine de test : 1558 règles brutes → 94 lignes utiles. Entrant
seulement — c'est la direction qui répond à « qu'est-ce qui peut joindre ce
poste depuis l'extérieur ».

Nouveaux tests : parsing d'un texte `netsh` synthétique (règle groupée,
désactivée, bloquante, au nom généré — chacune doit être écartée ; deux
entrées de même nom correctement fusionnées, profils réunis sans doublon),
et placement dans « Sécurité ». Vérifié aussi sur les 1558 règles réelles de
la machine de développement, rendu réel de la fiche système inspecté dans
le navigateur avant publication.

---

## [2.11.0] - 2026-08-15 🔍

### 🔍 GPO, pilotes non signés, processus gourmands, code STOP des écrans bleus

Quatre pistes de diagnostic/audit en plus, toutes sans donnée sensible —
choisies parmi une liste de propositions, à l'exclusion explicite des mots de
passe de comptes mail et de session, qui ne seront pas ajoutés.

**Stratégies de groupe appliquées.** `gpresult /X` (export XML), pas
`gpresult /r` (texte) : même raison que pour les profils Wi-Fi de la version
précédente — le texte change de libellés selon la langue de Windows, le
schéma XML est fixe. Le périmètre utilisateur ne demande aucun privilège
particulier et est donc toujours présent ; le périmètre ordinateur
n'apparaît que si la collecte tourne élevée — signalé explicitement plutôt
que laissé silencieusement absent.

**Pilotes non signés.** `Win32_PNPSignedDriver.IsSigned`, pas `DriverDate` :
de nombreux pilotes Windows intégrés portent une date ancienne héritée de
leur toute première publication sans que ce soit un signal de problème — un
faux positif systématique sur la moitié du parc, écarté délibérément.
L'absence de signature, elle, est un fait vérifiable sans ambiguïté.

**Processus les plus gourmands.** `Get-Process` expose un temps CPU cumulé
depuis le lancement du processus, pas une charge instantanée — un
navigateur ouvert depuis trois jours dominerait le classement même
parfaitement inactif là, maintenant. Deux relevés espacés d'environ 600 ms
et leur delta donnent un vrai pourcentage instantané, normalisé par le
nombre de cœurs. Top 5 CPU et top 5 RAM, séparément.

**Code STOP des écrans bleus.** `system_incidents` savait déjà qu'il y avait
eu un écran bleu ; il ne disait pas lequel. Le code (ex.
`WHEA_UNCORRECTABLE_ERROR`, `MEMORY_MANAGEMENT`) est désormais extrait du
champ structuré `param1` de l'événement 1001, pas du texte localisé du
message — et entre dans la clé de regroupement, pour que deux écrans bleus
de causes différentes ne soient plus comptés comme un seul incident répété.

Nouveaux tests : extraction du code STOP (connu, inconnu, absent), lecture
d'un rapport `gpresult /X` (périmètres utilisateur et ordinateur, GPO
désactivée, GPO refusée, aucune GPO), et placement de chaque nouveau champ
dans la bonne rubrique. Vérifié aussi sur des données réelles collectées sur
la machine de développement (pilotes non signés, GPO locale, processus),
avec un écran bleu simulé pour confirmer l'enrichissement du libellé —
rendu réel de la fiche système inspecté dans le navigateur avant publication.

---

## [2.10.0] - 2026-08-15 📶

### 📶 Réseaux Wi-Fi enregistrés, remontés comme identifiants chiffrés

Le collecteur relève désormais les réseaux Wi-Fi enregistrés sur le poste —
pas seulement celui actuellement connecté (déjà collecté depuis longtemps),
tous les profils sauvegardés (`netsh wlan export profile`, un XML par réseau :
le schéma est fixe quelle que soit la langue de Windows, contrairement au
texte localisé de `netsh wlan show profile`).

**Chemin séparé, volontairement.** SSID et type de sécurité sont collectés et
envoyés systématiquement, comme le reste de la collecte. Le mot de passe en
clair, lui, ne l'est que sur un geste explicite — case décochée par défaut
côté GUI, `--wifi-passwords` côté CLI — et dans ce cas seulement, `netsh`
tourne avec `key=clear` ; sans la case cochée, le mot de passe n'est même pas
présent dans le XML exporté, il n'est jamais lu. Ni l'un ni l'autre n'entre
dans `system_report` : contrairement à tout le reste de la collecte, ces
données ne transitent jamais par la fiche appareil ni le PDF, où elles
seraient visibles en clair. Un appel API séparé
(`POST /api/device-info/wifi-credentials`) les range directement dans la
table `identifiants` (catégorie Wi-Fi, déjà existante — le formulaire manuel
l'utilisait déjà), chiffrées comme tout autre identifiant stocké.

**Pas de doublon à chaque collecte.** Un SSID déjà connu pour le client est
mis à jour plutôt que recréé ; son mot de passe existant n'est écrasé que si
la collecte en apporte un nouveau — jamais vidé par une collecte où la case
n'était pas cochée. Un nom ou une description personnalisés à la main, via le
formulaire identifiant, survivent à toute resynchronisation automatique.

**Dossier temporaire, nettoyé immédiatement.** `netsh wlan export profile`
écrit ses XML dans un dossier qui n'existe que le temps de la lecture — avec
`key=clear`, il contient des mots de passe en clair sur disque, supprimé y
compris en cas d'erreur.

Nouveaux tests (`test_wifi_credentials.py`) : lecture du XML (réseau protégé,
réseau ouvert, authentification non répertoriée renvoyée telle quelle plutôt
que forcée), le mot de passe n'est jamais lu sans le geste explicite même
présent dans le XML, mise à jour sans duplication, préservation du nom/de la
description personnalisés, isolation entre clients, et bout en bout via
l'API — vérifié aussi par rendu réel de la page Identifiants et relecture du
mot de passe via son point de reprise habituel.

---

## [2.9.9] - 2026-08-15 🔄

### 🔄 Trigger de synchronisation Turso figé sur une définition périmée

Repéré en production : le journal de synchronisation affichait, sur un site
utilisant le mode multi-instance (Turso), `push documents_appareils: SQLite
error: UNIQUE constraint failed: _sync_journal.tbl, _sync_journal.record_id,
_sync_journal.action`. Aucune donnée perdue — `appareils` avait synchronisé
195/195 dans le même cycle — mais un changement resté coincé sur cette seule
table.

**Cause.** `_ensure_turso_schema()` réplique vers Turso les triggers
`_trg_journal_*` qui alimentent son propre journal de changements (nécessaire
pour que chaque instance sache ce que les autres ont modifié). Cette
réplication utilisait `CREATE TRIGGER IF NOT EXISTS` : un trigger déjà présent
sur Turso — créé par une version antérieure du code — n'était donc **jamais**
remis à jour, quoi que dise le code actuel. Le même défaut avait déjà été
corrigé côté local il y a plusieurs versions (`_TRACKED_JOURNAL` dans
`app.py`, en DROP+CREATE explicite), sans que le chemin de réplication vers
Turso reçoive le même traitement.

**Fix.** DROP+CREATE appliqué désormais côté Turso aussi — mais seulement
quand la définition locale diffère de celle déjà présente sur Turso (comparée
texte à texte). Un DROP+CREATE aveugle à chaque cycle (~30 s, potentiellement
depuis plusieurs instances) aurait ouvert une fenêtre où le trigger n'existe
pas, pile au moment où une autre instance pourrait écrire — la comparaison
préalable évite cette contrepartie tout en rendant la réplication réellement
capable de se corriger elle-même.

Deux nouveaux tests dans `test_sync_turso.py` (connexions SQLite en mémoire,
sans dépendance à un vrai Turso) : un trigger périmé est bien remplacé et la
ré-insertion sur une clé déjà journalisée ne lève plus d'erreur ; un trigger
déjà à jour n'est pas retouché.

---

## [2.9.8] - 2026-08-14 🦠

### 🦠 Détections antivirus, erreurs système/applicatives, arrêts & redémarrages

Trois historiques en plus, demandés directement : les détections de virus et
malwares, les erreurs système, et les erreurs logicielles — plus un quatrième
proposé en cours de route et retenu, l'historique des arrêts/redémarrages.

**Détections antivirus.** `Get-MpThreatDetection` + `Get-MpThreat` (Windows
Defender), joints sur `ThreatID`, sur une fenêtre d'un an plutôt que les 30
jours habituels — une détection reste pertinente longtemps après avoir été
traitée. Catégorie et niveau de gravité dérivent du **préfixe** du nom de la
menace (`Trojan:…`, `PUA:…`, `Ransom:…`), pas du `CategoryID` numérique dont
seules deux valeurs étaient confirmées sur cette collecte — le préfixe est une
convention Microsoft documentée et stable. Atterrit dans « Sécurité &
conformité ».

**Erreurs système.** Journal Système, niveaux Erreur/Critique, groupées par
fournisseur+ID sur 30 jours — en excluant explicitement les couples déjà
couverts par les incidents système existants, pour ne rien compter deux fois
entre les deux rubriques.

**Erreurs applicatives.** Plantages (ID 1000) et blocages/« ne répond plus »
(ID 1002) du journal Application. Les deux partagent le même journal mais pas
le même schéma de champs : un 1002 n'a pas d'équivalent aux positions
module/exception/chemin d'un 1000, et les y lire renvoyait un horodatage et un
GUID pris pour un nom de module et un code d'exception — repéré sur une
collecte réelle (un blocage de « SD Card Formatter.exe »), corrigé en
distinguant explicitement les deux schémas plutôt qu'en supposant qu'ils
coïncident. Les codes NTSTATUS courants (`c0000005`, `c0000374`…) sont
traduits en clair quand ils sont reconnus.

**Arrêts & redémarrages.** ID 1074, seul de ce lot à avoir des champs XML
nommés plutôt que positionnels — distingue un arrêt/redémarrage planifié
(mise à jour, action utilisateur) d'un arrêt inattendu.

Chaque ajout suit le chemin déjà établi — collecteur, aperçu du collecteur,
fiche système, rapport PDF — et atterrit dans la rubrique déjà en place qui
lui correspond (Sécurité & conformité pour les détections antivirus,
Diagnostic pour le reste), pas dans une nouvelle liste séparée. Vérifié sur
une collecte réelle ; nouveaux tests sur le nettoyage des chemins Defender, la
catégorisation d'une menace et la non-confusion des schémas 1000/1002.

---

## [2.9.7] - 2026-08-11 🕵️

### 🕵️ Agents détectés, mots de passe, maintenance et démarrage

Quatre familles d'informations en plus, réparties dans les rubriques déjà en
place plutôt qu'ajoutées en vrac.

**Agents détectés — remplissent des champs qui existaient déjà.** La fiche
appareil a toujours eu `av_nom`, `edr_nom`, `rmm_nom` et l'identifiant AnyDesk,
saisis à la main jusqu'ici. Le collecteur les propose désormais tout seul :
l'ID AnyDesk se lit directement sur le poste (`anydesk.exe --get-id`,
documenté par l'éditeur — aucun fichier de configuration à interpréter) ;
EDR et agents de télémaintenance (CrowdStrike, SentinelOne, TeamViewer,
ScreenConnect, NinjaOne, Datto, N-able…) sont recherchés parmi les services,
par sous-chaîne de leur nom affiché — au mieux, pas une preuve, et sans jamais
écraser une valeur déjà saisie. Les dropdowns EDR/RMM de la fiche appareil,
jusqu'ici vides faute de valeurs par défaut, sont peuplés au passage.

**Mots de passe & accès.** Politique de mot de passe local (longueur, complexité,
verrouillage — lue via `secedit`, dont les clés restent en anglais quelle que
soit la langue de Windows, contrairement à `net accounts`) ; membres réels du
groupe Bureau à distance (résolu par SID, comme le groupe Administrateurs
depuis la 2.9.5) ; identifiants Bureau à distance enregistrés dans le
Gestionnaire d'identifiants Windows — un serveur qui n'existe plus dans cette
liste est une piste de nettoyage.

**Maintenance & hygiène.** Plan d'alimentation actif, démarrage rapide, date
de la dernière analyse antivirus, versions du framework .NET installées
(Framework 3.5/4.x et Core/5+).

**Disque, démarrage & connexions distantes.** Style de partition (GPT/MBR) et
mode de démarrage (UEFI/Legacy) — utile avant une réinstallation ou un
remplacement de disque ; historique des connexions Bureau à distance entrantes
récentes, qui complète le journal de sécurité existant par ce qui a réussi, à
la différence des échecs déjà suivis.

**Correction en cours de route.** Le mode de démarrage se lisait d'abord via
`bcdedit /enum`, qui s'est révélé exiger les droits administrateur pour la
simple lecture — y compris sur un poste sans rien d'inhabituel (constaté, pas
supposé). `Get-ComputerInfo -Property BiosFirmwareType` donne la même réponse
sans élévation.

Chaque ajout suit le chemin déjà établi — collecteur, aperçu du collecteur,
fiche système, rapport PDF, `champs_deduits_du_collecteur` — et atterrit dans
la rubrique déjà en place qui lui correspond (Sécurité, Accès distant,
Environnement & hygiène, Stockage), pas dans une nouvelle liste séparée.
Vérifié sur une collecte réelle ; nouveaux tests sur la détection des agents,
la reconnaissance d'un identifiant AnyDesk et le placement de chaque champ.

---

## [2.9.6] - 2026-08-11 🗂️

### 🗂️ Fiche système et rapport PDF réorganisés par thème

Les rubriques s'étaient accumulées dans l'ordre où elles ont été ajoutées, pas
dans un ordre qui se lit facilement — le PDF en particulier avait fini par
couper certains sujets en deux blocs distants de plusieurs pages. Revu dans la
fiche système, le PDF et l'aperçu du collecteur, avec un objectif : une même
information à un seul endroit, entourée de ce qui lui ressemble.

**Sécurité et accès, un seul bloc.** Accès distant & exposition, Journal de
sécurité et Certificats à renouveler rejoignent Sécurité & conformité — ils
vivaient jusqu'ici après Environnement/Applications par défaut, ou dans un
groupe « Diagnostic » sans rapport. Périphériques en erreur rejoint
Périphériques USB.

**Deux ressources réseau réunies.** Les partages exposés par la machine et les
lecteurs mappés depuis d'autres machines vivaient respectivement dans
« Démarrage » et « Applications par défaut » — aucun rapport avec l'un ni
l'autre. Ils forment maintenant « Partages & lecteurs réseau », à côté de la
configuration réseau. Le redémarrage en attente, qui n'avait rien à faire dans
« Applications par défaut », a rejoint « Environnement & hygiène système ».

**Cycle de vie regroupé.** Mises à jour disponibles et correctifs déjà
installés — le même sujet — se suivent désormais, plutôt que d'être séparés
par sept autres rubriques. Comptes utilisateurs et profils utilisateurs (leur
occupation disque) sont adjacents.

**Doublons supprimés.** Le rattachement au domaine s'affichait deux fois
(une version sommaire, une plus complète avec le contrôleur de domaine) : la
version sommaire disparaît. Dans le PDF spécifiquement — plus touché que la
fiche, ayant accumulé une liste de rattrapage en fin de rapport — les comptes
utilisateurs et les adaptateurs réseau apparaissaient chacun deux fois, sous
deux formats différents ; ils ne sortent plus qu'une fois, sous leur forme la
plus complète.

**Le PDF rattrape aussi deux angles morts propres à lui.** L'identification et
le détail matériel (processeur, carte mère, mémoire) étaient scindés en deux
blocs séparés par Sécurité, Disques, Ports, USB et Licences — la même machine
se décrivait à deux endroits distants du rapport ; ils sont fusionnés. La carte
mère s'affichait comme une structure Python brute au lieu de son nom — un bug
de longue date, invisible tant que personne n'avait ouvert un PDF avec cette
page sous les yeux. Les disques physiques, passés en tableau avec badge SMART
dans la fiche (2.9.3), restaient en phrase brute dans le PDF ; RAM totale et
disponible n'y apparaissaient nulle part. Corrigés au passage.

Vérifié sur une collecte réelle, fiche et PDF, plus un test qui fige
l'emplacement de chaque donnée déplacée — pour qu'un futur ajout ne la
laisse pas retomber dans « Diagnostic » ou « Applications par défaut ».

---

## [2.9.5] - 2026-08-11 🔎

### 🔎 Trois nouvelles familles d'informations collectées

Le collecteur remonte, et la fiche système comme le rapport PDF affichent :

**Accès distant & exposition.** Une section dédiée réunit toutes les voies
d'entrée, avec leur *état réel* (pas seulement un port qui écoute) : Bureau à
distance (RDP, avec/sans NLA), WinRM / PowerShell Remoting, OpenSSH Server,
serveur et client Telnet, Assistance à distance, Registre distant. Chacune est
notée par criticité — un serveur Telnet actif ou un RDP sans NLA passent en
rouge. S'y ajoute l'**ouverture automatique de session**, signalée comme un
contournement d'authentification, avec un drapeau si le mot de passe traîne en
clair dans le registre (jamais sa valeur). RDP, qui figurait sous « Hygiène »,
rejoint cette section.

**Comptes de messagerie — sans les mots de passe.** Outlook (classique) et
Thunderbird sont lus complètement : adresse, protocole, serveurs entrant/sortant
et ports ; le nouvel Outlook est détecté (ses comptes ne sont pas énumérables de
façon fiable, c'est dit tel quel). Les pseudo-entrées d'Outlook (carnet
d'adresses, fichier de données) sont écartées, et l'adresse des comptes Exchange
est récupérée depuis le nom affiché.

> **Les mots de passe ne sont pas collectés, et ce n'est pas une limite
> technique.** Les blobs Outlook (DPAPI) et Thunderbird (NSS) sont déchiffrables
> sous le compte de l'utilisateur ; les extraire ferait de l'outil un voleur
> d'identifiants, et ces rapports se répliquent d'une instance à l'autre. Seule
> la *présence* d'un mot de passe enregistré est notée.

**Applications par défaut & lecteurs réseau.** Navigateur et client mail par
défaut, navigateurs installés avec leurs versions, lecteurs réseau mappés, et un
indicateur de redémarrage en attente (avec sa raison).

Tout suit le même chemin que le reste : collecteur → aperçu du collecteur →
fiche système → rapport PDF, sous garde du test de parité. Vérifié sur une
collecte réelle (7 voies d'accès, 8 comptes mail, 5 lecteurs mappés).

---

## [2.9.4] - 2026-08-11 🧹

### 🧹 Un reliquat verrouillé retardait la mise à jour de 26 secondes

Le nouveau mécanisme a fonctionné pour son premier passage réel (2.9.2 → 2.9.3),
mais son journal a révélé un défaut que lui seul rendait visible :

```
tentative 1 : fichier encore verrouillé ([WinError 5] Accès refusé:
              'D:\Parcinfo\ParcInfo-Windows.exe.old')
… treize fois, sur vingt-six secondes …
remplacement vérifié, empreinte identique
```

**Ce qui se passait.** La mise à jour précédente, faite par l'ancien script
`.bat`, n'avait pas réussi à supprimer sa copie de sauvegarde `.old` — et ne
l'avait pas signalé. Le fichier est resté verrouillé plus de deux heures. Or le
remplacement commençait par effacer cette copie : une opération de ménage, sans
rapport avec la mise à jour en cours, bloquait donc toute l'opération.

**Correction.** Le ménage n'a plus droit de blocage. Si l'ancienne sauvegarde
résiste, un nom libre est pris immédiatement et le remplacement se poursuit ;
le reliquat est signalé dans le journal et supprimé au démarrage suivant.
L'attente reste réservée au seul cas qui la justifie : l'exécutable en cours
que Windows n'a pas encore relâché.

Le cas est reproduit dans les tests, avec un fichier réellement verrouillé :
**0,0 s au lieu de 26 s**.

---

## [2.9.3] - 2026-08-11 💽

### 💽 Fiche système : disques physiques et placement des badges

**Disques physiques.** Ils s'affichaient en liste à puces, une longue phrase par
disque — il fallait la lire en entier pour retrouver la capacité ou l'état. Ils
sont désormais présentés en tableau : nom, type, capacité **alignée à droite en
chasse fixe** (seule façon de comparer sept disques d'un coup d'œil) et **état
SMART en badge** — Sain, À surveiller, Défaillant. Un état opérationnel dégradé
s'ajoute en second badge. Un format non reconnu (macOS, Linux) reste affiché tel
quel plutôt qu'interprété de travers.

**Badges des autres rubriques.** Une cause commune expliquait leur placement :
les grilles étiquette/valeur utilisaient `auto-fill`, qui créait **trois pistes
alors que ces rubriques n'ont que deux colonnes**. La troisième restait vide —
un tiers de la largeur perdu — pendant que les valeurs se serraient dans 190 px
et renvoyaient leurs badges à la ligne, détachés de ce qu'ils qualifient.
`auto-fit` réduit la piste vide à zéro : la colonne de valeur passe de **191 à
449 px**, et plus aucun des 232 badges de la page ne se retrouve à la ligne.
Vérifié par mesure dans le navigateur, pas à l'œil.

Trois défauts corrigés au passage :

- un badge posé sur la ligne de base paraissait s'enfoncer sous le texte
  qu'il accompagne — il est maintenant centré verticalement ;
- un libellé d'état pouvait être coupé en deux et ne se lisait plus comme un
  badge ; deux badges consécutifs se touchaient ;
- **sur écran étroit** (sous 1000 px), l'étiquette fixe accaparait plus de la
  moitié de la colonne : elle prend désormais sa propre ligne. Les tableaux trop
  larges défilent dans leur rubrique au lieu de décaler la fiche entière.

Enfin, une machine ne remontant que ses disques physiques n'affichait **aucune**
rubrique Stockage : elle manquait à la condition d'affichage.

---

## [2.9.2] - 2026-08-11 🔁

### 🖥️ Nouvelle icône

Écran, engrenage, document et pastille de validation. Elle remplace l'ancienne
icône partout : exécutable Windows, collecteurs (qui n'en avaient aucune),
barre système — qui dessinait encore un rond bleu de substitution — et onglet
du navigateur, où il n'y avait pas de favicon du tout.

Elle est **dessinée par `generer_icone.py`**, et non redimensionnée depuis une
image : sous 64 px, un trait mis à l'échelle passe sous le pixel et l'icône
devient une tache grise. Chaque format du `.ico` a donc son propre rendu, avec
un trait épaissi et moins de détail pour les plus petits — à 16 px, les lignes
du document sont retirées et la pastille passe en aplat. Pour la retoucher,
modifiez le script et relancez-le :

```bash
python generer_icone.py static
```

### 🔁 Le nouvel exécutable applique lui-même la mise à jour

**D'abord, une rectification.** Le correctif annoncé en 2.9.1 reposait sur une
cause supposée — des variables d'environnement du lanceur PyInstaller héritées
par l'application relancée. Reconstruction d'un cas réel à l'appui, cette
explication **ne tient pas** : le redémarrage aboutit avec ou sans elles. La
2.9.1 ne corrigeait donc rien de ce défaut.

**Ce qui change ici.** Le remplacement de l'exécutable était confié à un script
`.bat` écrit à la volée par l'application sortante. Trois défauts :

- il ne laissait **aucune trace exploitable** : quand le redémarrage échouait,
  il ne restait qu'une boîte de dialogue et rien à examiner ;
- c'était **l'ancienne version** qui pilotait le remplacement, donc un
  correctif du mécanisme ne s'appliquait jamais à la mise à jour qui
  l'installait — il fallait attendre la suivante ;
- un script batch impose ses propres contraintes (encodage, guillemets, codes
  de retour) sur un enchaînement qui demande de la précision.

Le travail revient désormais au **binaire téléchargé**, relancé avec
`--appliquer-maj`. Il n'est pas verrouillé, son empreinte vient d'être
vérifiée, et il porte la version la plus récente du mécanisme. Son déroulé :

1. il attend la sortie du processus qu'on lui a désigné (interrogation du
   système, pas une attente fixe : une machine chargée met plus longtemps) ;
2. il met l'ancien exécutable de côté, en réessayant tant que Windows le tient ;
3. il se recopie sur lui, puis **recalcule l'empreinte du fichier écrit** — une
   copie tronquée ou amputée par un antivirus donnerait un exécutable qui ne
   démarre pas, exactement le symptôme signalé ;
4. au moindre écart, il **remet l'ancienne version en place** : mieux vaut la
   version précédente qu'aucune application ;
5. il relance, et consigne chaque étape dans **`_maj.log`**, à côté de
   l'exécutable.

Le dossier de téléchargement et l'ancien exécutable sont effacés au démarrage
suivant : à l'instant du remplacement, le processus s'exécute depuis ce dossier
et Windows tient encore l'ancienne image — la suppression y échoue.

**Éprouvé pour de bon.** Le scénario complet a été rejoué avec de vrais
exécutables PyInstaller *onefile* : remplacement effectué, empreinte conforme,
et la nouvelle version démarre réellement. Les suites de tests couvrent le
déroulé sur fichiers réels, y compris le retour à l'ancienne version après une
copie infidèle.

> **Une fois installée**, cette version applique elle-même les mises à jour
> suivantes : un correctif du mécanisme prendra effet dès la mise à jour qui
> l'apporte, au lieu de la suivante.

---

## [2.9.1] - 2026-08-10 🔌

### 🔌 « Failed to load Python DLL » au redémarrage

> ⚠️ **Rectification (2.9.2).** L'explication ci-dessous a été vérifiée après
> coup sur de vrais exécutables PyInstaller : elle **ne tient pas**. Le
> redémarrage aboutit que ces variables soient héritées ou non. Cette version
> ne corrigeait donc pas le défaut annoncé. Voir 2.9.2.

**Votre installation n'est pas abîmée.** La mise à jour a bien eu lieu ; seul
le redémarrage automatique échouait. Lancer l'application depuis l'explorateur
fonctionne — c'est d'ailleurs ce qui a mis sur la piste.

**La cause supposée, démentie depuis.** L'application packagée se décompresse dans un dossier temporaire,
et le lanceur PyInstaller pose des variables d'environnement pour le désigner.
Le script de remplacement, lancé *par* l'application, en héritait, puis relançait
la nouvelle version qui en héritait à son tour. Celle-ci croyait donc avoir déjà
été décompressée et allait chercher `python311.dll` dans le dossier temporaire
du processus précédent — supprimé au moment où celui-ci s'est arrêté.

- ✅ Un environnement **explicitement débarrassé** de ces variables est transmis
  au script de remplacement
- ✅ Le script les efface **à son tour**, au cas où il serait relancé depuis une
  console elle-même héritée
- ✅ Les noms ont été relevés **dans le binaire du bootloader** livré avec
  PyInstaller 6, pas de mémoire : `_PYI_APPLICATION_HOME_DIR`,
  `_PYI_ARCHIVE_FILE`, `_PYI_PARENT_PROCESS_LEVEL`, plus `_MEIPASS` et
  `_MEIPASS2` des versions antérieures

### 🧪 COUVERTURE
Le test vérifie qu'un environnement explicite est transmis, qu'aucune de ces
variables n'y figure, et que le reste de l'environnement est bien conservé —
un environnement vidé empêcherait le script de trouver `cmd`.

---

## [2.9.0] - 2026-08-10 ✍

### ✍ LA FICHE APPAREIL SE REMPLIT DEPUIS LA COLLECTE

**Pourquoi les champs Antivirus restaient vides.** Le collecteur écrivait
l'antivirus détecté dans une colonne `antivirus` que le formulaire n'affiche
pas : la fiche système annonçait « Windows Defender », mais les champs *Marque*
et *Nom* de la fiche appareil restaient vides. La donnée était là, dans un
champ invisible.

Sont désormais renseignés à partir de la collecte :

- **Utilisateur** — la session ouverte au moment de la collecte, domaine retiré
  (`MONDOMAINE\Éric` donne `Éric`)
- **Marque et nom d'antivirus** — rapprochés des listes de référence :
  *Bitdefender Endpoint Security Tools* donne la marque **Bitdefender**,
  *Windows Defender* donne *Windows Defender / Microsoft Defender*. Un produit
  inconnu n'est **jamais** rapproché de force d'une entrée de la liste : la
  valeur brute est conservée plutôt qu'une marque fausse
- **Logiciels métier** — ceux de la liste du client effectivement installés

### 🛡 UNE RÈGLE : NE JAMAIS ÉCRASER UNE SAISIE
Seules les cases **restées vides** sont remplies. Une valeur corrigée par un
technicien survit à toutes les collectes suivantes — même principe que pour le
type d'appareil, déjà protégé. Un champ contenant seulement des espaces est
traité comme vide, sans quoi un champ effacé serait resté bloqué à jamais.

### 🔁 LES APPAREILS DÉJÀ COLLECTÉS SONT RATTRAPÉS
Leur rapport est déjà en base : inutile de relancer le collecteur sur chaque
poste. Le rattrapage lit les rapports existants, remplit les cases vides, et
ne s'exécute **qu'une fois par base**.

### 🧪 COUVERTURE
`test_remplissage_fiche.py` : rapprochement avec les listes, refus de rapprocher
un produit inconnu, saisie manuelle préservée sur plusieurs collectes, champ
d'espaces traité comme vide, rendu effectif dans le formulaire, et rattrapage
qui ne rejoue pas.

### 📝 AU PASSAGE
Un identifiant d'appareil à quatorze chiffres m'a paru anormal : il est
délibéré. Un décalage aléatoire est appliqué aux compteurs pour éviter que deux
instances ne produisent le même identifiant. Rien à corriger.

---

## [2.8.4] - 2026-08-10 🎯

### 🎯 « WinError 5 : Accès refusé » — la vraie cause
Le téléchargement aboutissait, puis échouait à la dernière seconde en
renommant le fichier.

**Le binaire publié s'appelle `ParcInfo-Windows.exe`, et c'est aussi le nom
sous lequel l'application tourne** quand on l'a prise sur la page des versions.
Le téléchargement se terminait donc en tentant d'écraser **l'exécutable en
cours d'exécution**, que Windows verrouille. Remplacer le binaire est le
travail du script différé, qui attend l'arrêt de l'application — pas celui du
téléchargement.

- ✅ Le téléchargement se fait dans un **sous-dossier dédié**, jamais à côté de
  l'exécutable
- ✅ Un **garde-fou** refuse toute destination qui désignerait l'exécutable en
  cours, quel que soit son nom
- ✅ Les fichiers `.part` abandonnés par les versions précédentes à côté de
  l'exécutable sont supprimés — jusqu'à 30 Mo laissés là après chaque échec

Ce défaut touchait **tout poste Windows ayant récupéré le binaire depuis la
page des versions**, c'est-à-dire le cas normal.

### 🧪 COUVERTURE
Le test reproduit la situation exacte : un exécutable en cours portant le nom
du binaire publié. Vérifié en retirant le correctif — le téléchargement vise
alors bien l'exécutable en cours, et le test échoue. Au passage, ce changement
a invalidé un test antérieur qui plaçait le fichier partiel à l'ancien
emplacement ; il a été corrigé plutôt que contourné.

### ⚠️ CE QUI RESTE SANS EXPLICATION
La **lenteur** signalée auparavant (5 % en dix minutes) est un phénomène
distinct, toujours sans cause identifiée. Le journal `_telechargement.log`
ajouté en 2.8.3 reste en place pour la documenter si elle se reproduit.

---

## [2.8.3] - 2026-08-10 ⬇

### ⬇ UN TÉLÉCHARGEMENT QUI SURVIT À UN RÉSEAU CAPRICIEUX
- 🐛 Un téléchargement interrompu **repartait de zéro** : le fichier partiel
  était effacé à chaque échec. Sur une liaison qui trébuche, la mise à jour ne
  pouvait donc jamais aboutir
- ✅ Le fichier partiel est **conservé**, et la reprise se fait par en-tête
  `Range`. Repli propre si le serveur ne sait pas reprendre : on repart de zéro
  plutôt que de coller la suite sur un début déjà là
- ✅ **Quatre tentatives** au lieu d'une, chacune reprenant la précédente
- ✅ **Détection des connexions qui traînent.** Le délai réseau ne se déclenche
  que s'il n'arrive *plus rien* — jamais si les données arrivent trop lentement.
  Sous 20 Ko/s pendant 45 secondes, la tentative est coupée et relancée sur une
  connexion neuve, au lieu de ramper pendant des heures
- ✅ Le **débit s'affiche** pendant le téléchargement, et
  **`_telechargement.log`** conserve le détail de chaque tentative : proxy
  détecté, octets reçus, durée, motif d'interruption

### 🎨 FICHE SYSTÈME — ALIGNEMENTS ET COULEURS
- 🐛 Les badges partaient **en escalier**, chacun démarrant à la fin de son
  libellé. Mesuré : les badges du pare-feu commençaient à 402, 399 et 393 px ;
  ils sont désormais tous à la même abscisse
- 🐛 Les intitulés *Antivirus*, *Pare-feu*, *Chiffrement BitLocker* et
  *Plateforme* utilisaient une couleur **plus terne** que le reste de la fiche.
  Ils reprennent exactement la couleur des autres intitulés — vérifié en
  comparant les valeurs calculées
- ✅ Les mentions courtes restent sur la ligne du badge, les explications
  longues passent à la ligne : mélangées, elles cassaient l'alignement

### ⚠️ CE QUE JE N'AI PAS PU REPRODUIRE
La lenteur signalée (5 % en 10 minutes) **ne se reproduit pas hors de
l'application** : sur la même machine, `urllib` télécharge le même fichier à
**21,9 Mo/s**, l'écriture atteint **33 Mo/s** dans le même dossier et sous le
même nom, et aucun proxy n'est configuré. La cause reste donc inconnue. Les
mesures et le journal ajoutés ici servent à l'identifier au prochain essai —
et la reprise fait qu'entre-temps, la mise à jour aboutit malgré tout.

---

## [2.8.2] - 2026-08-10 🔐

### 🐛 « D:: 0 (Protection: 0) »
Le chiffrement des volumes s'affichait ainsi, ce qui ne veut rien dire. Windows
renvoie deux **énumérations** — état du volume et état de la protection — que la
sérialisation JSON réduisait à des entiers bruts. Elles sont maintenant
converties en chaînes côté PowerShell, puis traduites. Les **deux formes**
renvoyées selon la version de Windows sont reconnues : entiers sur les
anciennes, libellés (`FullyEncrypted`, `On`) sur les récentes.

### 🔐 UN ÉTAT BITLOCKER QU'ON PEUT LIRE
- ✅ Badge **Activé / Désactivé** en tête du bloc, puis l'état de chaque volume
  et la méthode de chiffrement quand il y en a une
- ✅ Quand aucun volume n'est protégé, la fiche le dit franchement : *les
  données du disque sont lisibles si la machine est volée*
- ✅ Sans donnée du tout, le bloc distingue « aucun volume BitLocker sur cette
  machine » d'une **collecte sans privilèges administrateur** — deux situations
  très différentes qui produisaient le même vide

### 🧱 SECTION SÉCURITÉ RÉORGANISÉE
- ✅ Quatre blocs autonomes — Antivirus, Pare-feu, BitLocker, Plateforme —
  répartis en colonnes selon la largeur disponible (quatre sur un écran large)
- ✅ Le badge d'état **suit le nom de l'antivirus** au lieu de passer à la
  ligne. La cause : la grille étiquette/valeur réservait 170 px à l'étiquette,
  et la valeur n'avait plus la place d'accueillir un badge à côté du nom

### 🔑 L'EMPLACEMENT DES CLÉS, MÊME VIDE
Le bloc des clés de récupération reste visible dans la fiche appareil quand il
n'y en a aucune, et énonce les trois explications possibles : BitLocker inactif,
collecte lancée sans privilèges administrateur, ou collecte antérieure à la
2.8.0. Son absence pouvait laisser croire que ParcInfo ne sait pas conserver ces
clés.

### 🧪 AU PASSAGE
L'alerte sur les volumes non chiffrés cherchait « non chiffré » **dans une
phrase que nous produisons nous-mêmes** — et laissait donc passer les libellés
renvoyés en anglais par Windows. Elle s'appuie désormais sur l'état structuré.
Le décodage est couvert par un test sur les deux formes, y compris un état
inconnu, qui doit ressortir tel quel plutôt qu'être inventé.

---

## [2.8.1] - 2026-08-10 🩹

### 🐛 ÉDITER UN APPAREIL RENVOYAIT UNE ERREUR 500
Régression introduite en 2.8.0 : la requête des clés BitLocker avait été placée
**après la fermeture de la connexion** à la base. Toute édition d'appareil
échouait, avec ou sans volume chiffré. La requête est remontée avant la
fermeture.

### 🔓 LA MISE À JOUR N'EST PLUS RÉSERVÉE AUX ADMINISTRATEURS
- ✅ Tout compte connecté peut lancer une mise à jour. Sur un poste de travail,
  celui qui utilise l'application est rarement celui qui porte le rôle
  d'administrateur dans ParcInfo, et la réserve empêchait purement et
  simplement les mises à jour
- ✅ L'auteur de la demande est inscrit au journal : l'opération redémarre
  l'application pour tout le monde, autant savoir qui l'a déclenchée

### 🧪 DES TESTS QUI PASSAIENT POUR LA MAUVAISE RAISON
- ✅ Trois suites créaient leurs comptes avec `INSERT OR IGNORE` **sans les
  colonnes obligatoires** : la contrainte était avalée en silence, le compte
  n'existait pas, et les assertions décrivaient en réalité une session sans
  utilisateur. Les comptes sont désormais créés explicitement
- ✅ Deux tests de non-régression couvrent exactement les symptômes signalés :
  l'affichage de la fiche appareil avec et sans clé BitLocker, et le lancement
  d'une mise à jour par un compte non administrateur

### 🔎 LE CONTRÔLE DE VERSION, RESSERRÉ
`verifier_version.py` exigeait que **tous** les numéros cités dans le README
soient la version courante. Il refusait donc les rappels historiques légitimes
(« avant 2.7.1, la mise à jour… ») alors que le risque réel est ailleurs : un
lien de téléchargement pointant vers une version disparue. Le contrôle ne porte
plus que sur les liens, la version déclarée et la commande Docker — vérifié en
introduisant un lien périmé, qu'il rejette toujours.

### 📝 README
Mentions périmées corrigées : le bouton d'installation n'est plus décrit comme
réservé aux administrateurs, et l'avertissement sur le mécanisme de mise à jour
vise désormais les versions antérieures à 2.7.1, pas la version courante.

---

## [2.8.0] - 2026-08-10 🔎

### 🔎 CE QUE LE COLLECTEUR VOIT EN PLUS

**Temps de démarrage.** Windows chronomètre lui-même chaque démarrage : autant
lire sa mesure plutôt que de deviner. « Le poste est long à démarrer » devient
une donnée, à rapprocher des programmes lancés au démarrage déjà collectés.

**Journal de sécurité.** Échecs d'ouverture de session et verrouillages de
compte sur 30 jours, avec le compte concerné et l'origine. Un compte verrouillé
en boucle trahit le plus souvent un service resté sur un ancien mot de passe ;
une rafale d'échecs depuis une même source, autre chose. Les comptes machine
sont écartés — ils font un bruit permanent sans rapport avec une personne.

**Certificats machine expirant sous 90 jours.** Panne silencieuse typique : VPN,
bureau à distance ou 802.1X tombent un matin sans qu'aucune modification n'ait
eu lieu la veille.

**De quoi le disque se remplit.** Taille déclarée de chaque logiciel — lue dans
le registre, sans aucun parcours de fichiers — et taille des profils
utilisateurs. C'est le complément direct de la tendance de saturation ajoutée en
2.7.0 : savoir qu'un disque se remplit sans savoir de quoi n'aide qu'à moitié.

**Fin de support de Windows**, déduite du build. La table des échéances doit
être tenue à jour — c'est son coût, et il est réel. Un build inconnu ne produit
aucune conclusion plutôt qu'une date inventée.

Tout cela alimente les points de vigilance, la fiche système et le PDF.

### 🔐 CLÉS DE RÉCUPÉRATION BITLOCKER
- ✅ Relevées par le collecteur, **chiffrées au repos** comme les mots de passe
  des identifiants, et affichées à la demande dans la fiche appareil par un clic
  sur l'icône — le même geste que pour un mot de passe
- ✅ **Retirées du rapport avant tout stockage** : le rapport est conservé tel
  quel en base et repris dans le PDF joint à l'appareil. Une clé qui déverrouille
  un disque n'a rien à faire dans une pièce jointe
- ✅ **Chaque consultation est inscrite à l'historique** : savoir qui a lu une
  clé et quand ne coûte rien ici, l'historique existait déjà
- ✅ Cloisonnement multi-client vérifié : la clé d'un appareil reste invisible
  depuis un autre client

### 🐛 UN DÉFAUT QUI SERAIT PASSÉ INAPERÇU
L'extraction des champs nommés d'un événement Windows était fausse : `$d.Champ`
ne renvoie rien, il faut filtrer sur le nom du champ. La mesure du temps de
démarrage serait restée **invisible en permanence**, et j'aurais conclu « aucun
événement sur cette machine ». Trouvé en comparant les deux écritures sur un
événement réel plutôt qu'en relisant le code.

### ⏱ À SAVOIR
La collecte passe d'environ **60 à 90 secondes** : mesurer la taille d'un profil
impose de parcourir son arborescence. L'opération est bornée dans le temps, et
une mesure interrompue est **signalée comme telle** — c'est alors un minimum,
jamais un total. Deux écritures naturelles ont dû être écartées : `foreach` sur
`Get-ChildItem -Recurse` matérialise toute l'arborescence avant la première
itération, et `break` dans un `ForEach-Object` interrompt le script entier.

---

## [2.7.1] - 2026-08-09 🔧

### 🐛 LA MISE À JOUR WINDOWS N'ABOUTISSAIT JAMAIS
Le bouton « Installer maintenant » téléchargeait bien la nouvelle version, mais
rien ne changeait ensuite.

- **Cause** : le script de remplacement était programmé, puis **rien n'arrêtait
  l'application**. Windows garde un verrou sur l'exécutable en cours ; le
  déplacement échouait, et l'ancienne version continuait de tourner comme si de
  rien n'était. Régression que j'ai introduite en **2.6.43**, en déplaçant
  l'installation du lanceur vers l'interface sans reporter la sortie du
  processus que le lanceur faisait juste après
- ✅ L'application rend maintenant la main deux secondes après avoir programmé
  le remplacement, le temps que la bannière affiche la confirmation
- ✅ Elle ne s'arrête **que si le remplacement a été programmé** : en cas
  d'échec, il serait absurde de fermer l'application sans rien pour la remplacer

### 🔁 UN SCRIPT DE REMPLACEMENT QUI PARDONNE
- ✅ Il **réessaie une trentaine de secondes** au lieu d'attendre une durée fixe
  puis d'abandonner en silence — un arrêt un peu lent suffisait à perdre la
  mise à jour
- ✅ Il remet l'ancienne version si la copie échoue, et **relance l'application
  dans tous les cas**
- ✅ Il écrit **`_apply_update.log`** : un échec laisse désormais une trace
  lisible au lieu de disparaître
- ✅ Écrit dans l'encodage attendu par `cmd.exe` : un chemin accentué le rendait
  illisible

### 👁 LES REFUS DU SERVEUR S'AFFICHENT
- ✅ Droits insuffisants ou mise à jour indisponible renvoyaient bien un motif,
  mais l'interface l'ignorait : le bouton restait sur « Démarrage… »
  indéfiniment. **Sans message, un refus ressemblait exactement à la panne
  ci-dessus** — la bannière affiche maintenant la raison

### 🧪 COUVERTURE
- ✅ `test_maj.py` vérifie que l'arrêt est demandé après un remplacement réussi,
  qu'il ne l'est **pas** après un échec, et que le script réessaie, relance et
  journalise. Vérifié en retirant le correctif : le test échoue bien sans lui
- ⚠️ Au passage, le test contenait un arrêt du serveur local resté d'une version
  précédente, qui coupait les sections ajoutées ensuite

---

## [2.7.0] - 2026-08-09 📈

### 🧪 LES TESTS TOURNENT ENFIN TOUT SEULS
- ✅ Un job **exécute les sept suites avant toute construction**. Elles
  existaient déjà mais n'étaient lancées qu'à la main : rien n'empêchait de
  publier une version sans qu'aucune ne tourne
- ✅ **`verifier_version.py`** contrôle la concordance des cinq sources de
  version, le tuple, les numéros cités dans le README, et **la correspondance
  du tag** — le contrôle qui manquait le jour où `v2.6.33` s'est retrouvé posé
  sur du code en 2.6.32
- ✅ Trois suites ajoutées : journal des mises à jour, durcissement, historique
- ✅ La CI a été éprouvée dans un conteneur Python 3.11 propre avant d'être
  poussée. Elle y a immédiatement révélé un test faux : la détection du mode
  d'exécution supposait qu'aucun conteneur n'était en jeu

### 📈 HISTORIQUE DES COLLECTES
Chaque collecte écrasait la précédente : on avait une photo, jamais une
trajectoire.
- ✅ Un **relevé horodaté par passage du collecteur** — espace disque, mémoire,
  système, numéro de série, empreinte des logiciels
- ✅ **Date de saturation du disque** projetée par régression linéaire. Elle ne
  conclut qu'à partir de trois relevés couvrant une semaine : sur deux points
  rapprochés, la moindre variation donnerait une date absurde
- ✅ **Logiciels ajoutés et retirés** entre les deux dernières collectes, et
  **changements matériels** — une barrette, un numéro de série ou un processeur
  qui change se voit alors tout seul
- ✅ Seules les grandeurs comparables sont conservées, pas le rapport entier :
  l'historique reste léger et se synchronise entre postes. 60 relevés par
  appareil, les plus anciens partent

### 🔒 DÉPÔTS DE FICHIERS ET API COLLECTEUR
- ✅ **Les dépôts acceptaient tout**, sans limite de taille. Désormais : liste
  blanche par usage (documents / images), **vérification de la signature du
  fichier contre son extension** — un exécutable renommé en `.pdf` est refusé —
  et 64 Mo maximum par requête
- ✅ **L'API du collecteur n'était pas authentifiée** : un POST suffisait à
  créer ou modifier des appareils et à déposer des fichiers. Un **jeton
  partagé**, configurable dans l'interface et saisi une fois dans les
  collecteurs, devient obligatoire dès qu'il est renseigné. Tant qu'il reste
  vide, rien ne change pour les collecteurs déjà déployés
- ✅ La liste des clients est protégée par le même jeton : les noms de vos
  clients n'ont pas à être lisibles par quiconque atteint le serveur
- 🐛 **La comparaison du jeton échouait sur un jeton accentué** et répondait 500
  au lieu de 401 — la fonction censée protéger l'API la cassait. Trouvé par le
  test, pas par la relecture

### 💾 RESTAURATION D'UNE SAUVEGARDE
- ✅ Restauration **depuis l'interface**, avec sauvegarde de sécurité prise
  juste avant et double confirmation. Seul le nom d'une sauvegarde existante est
  accepté — un chemin relatif ne peut pas désigner autre chose
- ✅ La copie passe par l'API `backup` de SQLite plutôt que par un remplacement
  de fichier : les connexions ouvertes resteraient sinon sur l'ancien fichier
- 🐛 **Deux sauvegardes prises dans la même seconde portaient le même nom**, et
  la seconde écrasait la première. C'était grave au moment de restaurer : le
  filet de sécurité remplaçait la sauvegarde qu'on s'apprêtait à recharger. Le
  test l'a montré du premier coup

### 🔌 PÉRIPHÉRIQUES EN ERREUR
- ✅ Les codes du Gestionnaire de périphériques sont remontés, traduits, et
  affichés dans la fiche, le PDF et les points de vigilance. Le reste de
  l'inventaire décrit ce qui est présent ; cette rubrique, ce qui ne marche pas
- ✅ « Désactivé » et « déconnecté » sont listés mais ne comptent pas comme
  pannes : ce sont des états voulus la plupart du temps

### 🧹 DIVERS
- ✅ Huit fichiers `.pyc` étaient versionnés alors que `.gitignore` les exclut

---

## [2.6.45] - 2026-08-09 🔢

### 🐛 BANNIÈRE MASQUÉE PAR LA BARRE LATÉRALE
- ✅ Avec la navigation en colonne, la bannière de mise à jour commençait
  **sous** la barre latérale — celle-ci est en position fixe, et seuls la barre
  client et le contenu principal étaient décalés de ses 220 px
- Mesuré avant correction : la bannière démarrait à **36 px**, la barre
  s'arrêtant à **220**. Après : **256 px**, entièrement visible

### 🔢 METTRE À JOUR DEPUIS LE NUMÉRO DE VERSION
- ✅ **Un clic sur le numéro de version** lance la recherche d'une mise à jour.
  S'il y en a une, la bannière s'affiche avec son bouton d'installation ; sinon
  le numéro confirme brièvement « à jour ✓ », puis reprend sa place
- ✅ **Pastille orange** sur le numéro dès qu'une version attend — y compris
  après avoir écarté la bannière. Sans elle, « Plus tard » effaçait toute trace
  de la mise à jour disponible
- ✅ Si l'annonce a été écartée, le clic **la redemande** (route
  `/api/updates/undismiss`), et ce choix survit au redémarrage
- ✅ Le numéro est désormais un **bouton**, plus un fragment du lien d'accueil :
  imbriqué dans celui-ci, un clic aurait ramené à l'accueil au lieu d'agir
- ✅ Placé à droite de la marque en navigation horizontale, sur sa propre ligne
  sous la marque en navigation verticale — vérifié dans les deux dispositions

---

## [2.6.44] - 2026-08-09 📓

### 📓 JOURNAL DES MISES À JOUR — PARTAGÉ ENTRE TOUS LES POSTES
- ✅ Chaque mise à jour est consignée avec le **nom de l'appareil**, la version
  d'origine, celle installée, le mode d'exécution et le résultat
- ✅ La table est **synchronisée** : un poste voit ce qui a été installé sur les
  autres installations, sans avoir à s'y connecter
- ✅ **Clé textuelle et non un identifiant auto-incrémenté** : chaque instance
  écrit dans sa propre base, et deux machines mises à jour le même jour
  auraient produit le même numéro — la synchronisation aurait écrasé l'une par
  l'autre
- ✅ Les **échecs** y figurent avec leur motif : c'est la seule trace
  consultable depuis un autre poste
- ✅ La page Journal présente les mises à jour d'abord, les cycles de
  synchronisation ensuite. Ces derniers restent locaux — ils décrivent ce que
  *cette* instance a échangé et n'auraient pas de sens répliqués. La machine
  consultée est mise en avant dans la liste

### 🖥 COLLECTEUR — UN APERÇU QU'ON PEUT LIRE
- ✅ **Onglets par rubrique** au lieu d'un bloc de texte brut : libellés en
  gris, valeurs en gras, listes titrées et comptées
- ✅ **Remplissage au fil de la collecte.** Elle dure une bonne minute ; un
  panneau vide pendant tout ce temps donne l'impression que rien ne se passe.
  Les onglets apparaissent dès que leurs données arrivent, et la position de
  défilement est conservée à chaque rafraîchissement — sans quoi l'utilisateur
  serait renvoyé en haut de la rubrique qu'il est en train de lire
- ✅ **Aperçu complet** : il couvre maintenant ce que contient le rapport —
  USB détaillés (marque, modèle, identifiant, date), diagnostic, tâches
  planifiées, configuration réseau, environnement, hygiène, mises à jour
  disponibles, points de vigilance
- ✅ **Bouton « Ouvrir le rapport PDF »**, qui rouvre le rapport déjà produit
  lors de l'envoi plutôt que d'en générer un second, identique
- ✅ Le résumé texte du collecteur en ligne de commande **dérive des mêmes
  rubriques** : deux constructions séparées auraient dérivé l'une de l'autre au
  fil des ajouts

#### Deux défauts d'affichage corrigés au passage
- **Onglets rognés.** Une quinzaine de rubriques sur une barre horizontale se
  réduisaient à « Points c », « Ident », « Sé ». Ils sont désormais **empilés à
  gauche** : leurs noms tiennent en entier quel qu'en soit le nombre
- **Boutons hors de la fenêtre.** Un conteneur extensible posé en premier prend
  toute la place restante et repousse ce qui vient après ; les barres du bas
  réservent maintenant leur place avant lui

### 🧹 CE QUI AVAIT ÉTÉ LAISSÉ DE CÔTÉ
- ✅ `journal_synchronisation` était créée **à l'identique dans `app.py` et
  `database.py`** — deux copies libres de diverger sans que rien ne le signale.
  Point unique désormais, dans `database.py`
- ✅ En-tête « v2.5.0 » périmé dans `docker-compose.synology.yml`. Le numéro est
  retiré : ce fichier ne change pas d'une version à l'autre, et le figer
  revenait à afficher un numéro faux à chaque release
- ✅ `backups/` ajouté au `.gitignore` — la sauvegarde automatique déposait des
  copies de la base dans le dépôt en mode développement

---

## [2.6.43] - 2026-08-09 🔄

### 🔄 MÉCANISME DE MISE À JOUR RECONSTRUIT

Trois défauts se cumulaient : rien ne s'affichait, macOS ne pouvait pas aboutir,
et le binaire téléchargé n'était jamais vérifié.

#### La bannière ne pouvait jamais apparaître
- `update_notifier.js` se terminait par une garde
  `document.readyState !== 'loading'`, alors que le script est chargé en fin de
  `<body>` : à cet instant le document est **toujours** en cours d'analyse. La
  classe était chargée, l'instance jamais créée — reproduit dans le navigateur
- ✅ Initialisation sur `DOMContentLoaded`, avec repli immédiat
- ✅ Vérification ramenée de **30 jours à 6 heures**. Même réparée, l'ancienne
  cadence aurait annoncé les versions après coup

#### macOS ne pouvait pas se mettre à jour
- La version publiée est une **archive ZIP**, le code tentait un
  `hdiutil attach` — réservé aux images DMG. Le montage échouait à chaque fois
- ✅ Archive décompressée, bundle remplacé, quarantaine Gatekeeper retirée,
  ancienne version conservée le temps de l'échange et remise en place en cas
  d'échec

#### Aucun contrôle d'intégrité n'avait jamais lieu
- Le code lisait la clé `windows_installer`, le fichier contient
  `windows_installer_sha256` : la clé ne correspondait pas et la vérification
  était **sautée en silence**. Les valeurs valaient de toute façon
  `PENDING_BUILD`, et rien ne les calculait
- ✅ Chaque version publie un **`SHA256SUMS.txt`**, produit par le workflow
- ✅ L'empreinte est réclamée **avant** le téléchargement : inutile de tirer
  30 Mo pour découvrir ensuite qu'on ne pourra pas les valider
- ✅ Sans empreinte vérifiable, l'installation est **refusée** et le
  téléchargement manuel proposé. Exécuter un binaire non vérifié offrirait à
  quiconque détourne la connexion un chemin direct vers la machine
- ✅ Un téléchargement interrompu ne prend son nom définitif qu'une fois complet

#### Windows n'installe plus rien sans le dire
- Le lanceur téléchargeait et remplaçait l'exécutable **à chaque démarrage**,
  sans message avant, pendant, ni après — d'où des redémarrages inexpliqués et
  aucune trace en cas d'échec
- ✅ La version disponible est annoncée dans l'interface ; l'installation part
  sur clic, **réservée aux administrateurs** — le remplacement redémarre
  l'application pour tout le monde
- ✅ Barre de progression pendant le téléchargement, puis **confirmation de la
  version installée** au redémarrage suivant

#### Docker
- ✅ Mode conteneur reconnu : la bannière affiche la **commande à lancer sur
  l'hôte**, avec bouton de copie. Un conteneur ne peut pas se remplacer lui-même
- ✅ Service **Watchtower** commenté dans `docker-compose.yml` pour ceux qui
  veulent l'automatiser. Il ne surveille que les conteneurs portant le label
  déclaré — sans quoi il toucherait à tout ce qui tourne sur la machine

#### Confort
- ✅ Écarter une annonce la masque **jusqu'à la version suivante**, et le choix
  survit au redémarrage
- ✅ `test_maj.py` : détection, refus d'un binaire altéré, refus sans empreinte,
  aucun fichier laissé derrière, persistance du choix, confirmation après
  installation, non-répétition, retour arrière non annoncé

### ⚠️ MISE À NIVEAU
Les versions **antérieures à 2.6.43** embarquent le mécanisme défaillant : le
passage à 2.6.43 doit se faire **manuellement, une fois**. Les suivantes se
feront depuis l'interface.

---

## [2.6.42] - 2026-08-09 🗄️

### 🗄️ SAUVEGARDE AUTOMATIQUE DE LA BASE
- ✅ Sauvegarde quotidienne, **ne conservant que les trois dernières** ; les plus
  anciennes sont supprimées à chaque nouvelle sauvegarde
- ✅ La copie passe par l'**API `backup` de SQLite**, pas par un copier-coller de
  fichier. La base tourne en mode WAL : une copie brute prise pendant une
  écriture donne un fichier tronqué, qui se révèle inutilisable au moment précis
  où l'on en aurait besoin
- ✅ Au démarrage, une sauvegarde n'est faite que si la dernière date de plus
  d'un intervalle — sans quoi chaque redémarrage en créerait une et ferait
  tourner la rotation jusqu'à ne plus garder que des copies de la même minute
- ✅ Deux sauvegardes simultanées ne se marchent pas dessus (verrou non bloquant)
- ✅ Route **`/api/db/sauvegarde`** : liste les sauvegardes, ou en déclenche une
  (réservé aux administrateurs). Désactivable par `PARCINFO_BACKUP=0`
- ✅ Couvert par `test_sauvegarde.py` : intégrité SQLite de la copie, accents
  préservés, rotation à trois, concurrence, base d'origine intacte

### 🔤 AUDIT ENCODAGE — CE QUI CASSAIT VRAIMENT
- ✅ **Téléchargement RDP corrompu par un accent.** L'en-tête HTTP était
  construit à la main, or un en-tête ne transporte que de l'ASCII : une machine
  nommée « Bureau-Réception » produisait un fichier « Bureau-R?ception.rdp ».
  L'en-tête suit maintenant la **RFC 6266** — repli ASCII plus version UTF-8
  percent-encodée, que les navigateurs préfèrent
- ✅ **Fichiers lus sans encodage explicite** : métadonnées de mise à jour,
  configuration du collecteur graphique, clé secrète. Sur un Windows français,
  `open()` utilise cp1252 et échoue sur tout contenu accentué — c'est le même
  défaut que celui corrigé dans le collecteur en 2.6.37
- ✅ **`netsh` du lanceur** décodait sa sortie avec l'encodage local, alors qu'un
  Windows français renvoie des messages accentués
- ✅ **Logiciels installés** stockés en base avec les accents échappés,
  contrairement au reste des données JSON de l'application

### ✅ CE QUE L'AUDIT A VALIDÉ
- **Synchronisation** : les 35 tables suivies correspondent exactement au schéma.
  Les trois seules tables non synchronisées sont celles de la machinerie de
  synchronisation elle-même — les inclure créerait une boucle
- **Écritures SQL** : aucune colonne écrite n'est absente du schéma
- **Requêtes dynamiques** : toutes construites à partir de listes blanches, avec
  les valeurs passées en paramètres. L'annulation d'une modification, qui bâtit
  une clause `SET` à la volée, filtre bien ses colonnes contre une liste figée
- **Rendus** : le test de parité fiche ↔ PDF passe toujours

### 📝 À NOTER
`journal_synchronisation` est créée à deux endroits (`app.py` et `database.py`)
avec des définitions aujourd'hui **identiques**. Rien n'est cassé, mais les deux
peuvent diverger : à unifier lors d'un prochain passage sur la synchronisation.

---

## [2.6.41] - 2026-08-09 📄

### 📄 MISE EN PAGE DU RAPPORT PDF
- ✅ **Plus aucune rubrique coupée en deux** : un saut conditionnel réserve la
  hauteur du titre et des premières lignes ; si la place manque, la rubrique
  entière commence à la page suivante. Vérifié sur le rapport de référence :
  **zéro titre orphelin sur 42 pages**
- ✅ La correction se fait **en un seul point**, juste avant le rendu : le récit
  est parcouru une fois et chaque titre reçoit son saut conditionnel. Aucun des
  vingt-sept endroits qui produisent un titre n'a eu à être modifié
- ✅ **Sommaire paginé** en tête de rapport — quarante pages sans index, cela se
  parcourt au jugé
- ✅ **Pied de page sur chaque page** : machine, client et numéro de page. Une
  page imprimée isolée reste identifiable
- ✅ **Correction** : « Virtualisation matérielle : False » — un booléen Python
  arrivait tel quel dans le rapport, au lieu d'« Activée » / « Désactivée »

### 🔎 RECONNAISSANCE SIGNALÉE DANS LE COLLECTEUR
- ✅ Quand la machine est reconnue d'après son adresse MAC, le collecteur
  graphique affiche un **bandeau vert** : « Machine déjà connue : client X
  présélectionné, modifiable si nécessaire ». L'avertissement générique sur le
  risque de mélange de données s'efface, puisqu'il ne s'applique plus
- ✅ L'information n'était jusqu'ici que dans la barre d'état, en bas de
  fenêtre : l'utilisateur ne la voyait pas et refaisait la sélection à la main

### 🔧 CORRECTION DE FOND DANS LE COLLECTEUR GRAPHIQUE
- ✅ Les threads de travail écrivaient **directement dans les widgets Tkinter**,
  ce qui n'est pas autorisé — y compris la lecture de la case de test de débit
  depuis le thread de collecte. Toutes les écritures passent désormais par la
  boucle principale via `after()`
- ✅ Le défaut était latent depuis l'origine et ne se manifestait pas
  systématiquement ; il a été révélé en écrivant un test automatisé de
  l'interface

---

## [2.6.40] - 2026-08-09 ⚖️

### ⚖️ PARITÉ FICHE SYSTÈME ↔ RAPPORT PDF
- ✅ **Le PDF avait pris un retard considérable** : 60 données étaient affichées
  dans la fiche et absentes du rapport — configuration et qualité réseau,
  incidents système, tâches planifiées, hygiène, comptes détaillés, mises à
  jour, batterie, détail matériel, cartes graphiques, correctifs installés. Le
  générateur PDF n'avait pas suivi les ajouts successifs
- ✅ Les deux rendus couvrent désormais **les mêmes données** : 41 pages sur la
  machine de test, contre 5 auparavant
- ✅ **Un test de parité empêche la dérive de recommencer**
  (`test_parite_rapports.py`) : il compare les clés consommées par chaque rendu
  et échoue dès qu'une donnée n'est affichée que d'un seul côté. Les
  divergences volontaires y sont inscrites **avec leur raison**, pas
  silencieusement ignorées

### 📶 DÉBIT : ABSENCE RENDUE EXPLICITE
- ✅ Quand la mesure n'a pas été demandée, la ligne affichait… rien. L'absence
  se confondait avec un défaut d'affichage
- ✅ La ligne est maintenant toujours présente, avec **« Non mesuré »** et la
  manière de l'activer (`--test-debit`, ou la case du collecteur graphique)

### 🔎 RECONNAISSANCE DU CLIENT PAR ADRESSE MAC
- ✅ Le collecteur demande au serveur s'il connaît déjà la machine. Si oui, le
  collecteur graphique **présélectionne son client** et l'annonce ; le
  collecteur en ligne de commande l'**utilise automatiquement** en l'absence de
  `--client-id`
- ✅ Évite qu'une machine déjà inventoriée reparte dans « Découverte réseau »
  parce que l'utilisateur s'est trompé de client dans une liste qui en compte
  parfois des dizaines
- ✅ L'adresse MAC est lue directement, pas prise dans la collecte : celle-ci
  dure une minute et tourne en parallèle, la reconnaissance serait arrivée trop
  tard
- ⚠️ `/api/clients-public` accepte un paramètre `mac` facultatif. Il ne renvoie
  que l'identifiant et le nom du client — **déjà publics sur cet endpoint** — et
  ne révèle rien lorsque la machine est inconnue. Sans suggestion la réponse
  reste une liste simple, de sorte que les collecteurs des versions
  précédentes continuent de fonctionner

---

## [2.6.39] - 2026-08-09 🔧

### 🔧 CORRECTION D'UNE ANNONCE ERRONÉE
- ✅ La case **« Mesurer aussi le débit descendant »** du collecteur graphique
  était annoncée dans les notes de la 2.6.38, mais le correctif avait échoué
  silencieusement avant d'atteindre ce fichier : la case n'existait pas. Elle
  est bien présente ici
- ℹ️ L'option `--test-debit` du collecteur en ligne de commande, elle,
  fonctionnait déjà en 2.6.38 — seule l'interface graphique était concernée

---

## [2.6.38] - 2026-08-09 🌐

### 🌐 CONFIGURATION ET QUALITÉ RÉSEAU
- ✅ **Serveurs DNS et passerelle par carte**, DHCP ou adressage manuel,
  **suffixes DNS**, et **proxy configuré** — les quatre réglages qui produisent
  tous le même symptôme (« il n'a plus Internet ») et ne se distinguent qu'ici
- ✅ **Environnement réseau détecté** : catégorie Windows (Domaine / Privé /
  Public), qui détermine quel profil de pare-feu s'applique, et connectivité
  IPv4 réellement constatée par Windows
- ✅ **Wi-Fi** : SSID, signal, bande et canal quand une interface existe
- ✅ **Qualité du lien mesurée à chaque collecte** : latence moyenne, pic et
  perte de paquets vers la passerelle, le serveur DNS et Internet. C'est ce qui
  distingue un problème de lien local d'un problème de connexion — un poste à
  500 Mb/s avec 200 ms vers sa **propre passerelle** a un souci de câble ou de
  Wi-Fi que le débit seul ne montre pas
- ✅ **Test de débit descendant, désactivé par défaut** : `--test-debit` en ligne
  de commande, case à cocher dans l'interface. Il consomme de la bande passante
  sur un poste de production et sollicite un service tiers — il reste un choix
  explicite, pas un comportement imposé à chaque collecte

### 🏢 ENVIRONNEMENT D'ENTREPRISE
- ✅ Serveur **WSUS** configuré et groupe cible — explique « pourquoi ce poste
  n'a pas les mises à jour »
- ✅ Rattachement au domaine, contrôleur utilisé, source de temps et **décalage
  d'horloge** (un décalage casse l'authentification Kerberos)

### 🧹 HYGIÈNE SYSTÈME
- ✅ **Points de restauration** — présence et date. Aucun point signifie qu'un
  retour arrière après une mise à jour ratée est impossible
- ✅ État de l'**UAC**, **Bureau à distance et NLA**. Sur la machine de test, RDP
  est actif **sans NLA** : le service est exposé avant toute authentification
- ✅ Espace récupérable en fichiers temporaires — 2,6 Go sur la machine de test,
  ce qui complète les barres d'occupation par volume

### 🔄 MISES À JOUR : RECHERCHE EN LIGNE
- ✅ La recherche interroge désormais **Microsoft Update ou le WSUS configuré**,
  et non plus le seul cache local. Elle voit donc les correctifs *applicables*,
  pas seulement ceux que Windows a déjà décidé d'installer
- ✅ La différence n'est pas théorique : sur la machine de référence le cache
  annonçait **zéro** alors qu'une mise à jour de **1,5 Go** était disponible
- ✅ Numéro de KB et taille de téléchargement affichés ; repli automatique sur le
  cache si la recherche échoue, **avec mention explicite de la source** pour
  qu'une liste incomplète ne passe pas pour exhaustive

### 📌 NOUVEAUX POINTS D'ATTENTION
Perte de paquets, latence anormale vers la passerelle, proxy résiduel, RDP sans
NLA, UAC désactivé, absence de point de restauration, temporaires volumineux.

---

## [2.6.37] - 2026-08-09 🔗

### 🔗 INCIDENTS DISQUE RAPPROCHÉS DU MATÉRIEL
- ✅ Le journal Système désigne les disques par `\Device\Harddisk6\DR6`, ce qui
  ne dit rien à personne. Le numéro est désormais rapproché du disque physique :
  **« Brother DCP-195C — USB »**, ou **« C: — KINGSTON SNV2S1000G — NVMe »**
- ✅ Un support amovible **débranché depuis l'incident** ne figure plus dans la
  table des disques : il est signalé comme absent plutôt qu'attribué au hasard à
  un disque encore présent. Sur la machine de test, les 35 blocs défectueux
  visaient un `Harddisk7` qui n'est plus connecté — l'information reste juste
- ✅ La table des disques n'est construite que si un incident disque a
  effectivement été relevé

### 🗓️ TÂCHES PLANIFIÉES
- ✅ Tâches hors dossier `\Microsoft\` — celles de Windows se comptent par
  centaines et n'apprennent rien. État, dernière exécution, résultat et
  exécutable
- ✅ Les tâches **en échec** remontent en tête et alimentent les points
  d'attention. Sur la machine de test : 13 échecs sur 29 tâches
- ✅ Le code retour 267011 (« jamais exécutée ») n'est pas compté comme un échec

### ⏳ PROGRESSION DU COLLECTEUR
- ✅ Étape en cours et barre de progression, pilotées par un rappel unique
  partagé : les deux collecteurs affichent les mêmes étapes sans dupliquer la
  liste
- ✅ En console interactive la barre se réécrit sur une seule ligne ; redirigée
  vers un fichier ou un journal, elle retombe sur une ligne par étape plutôt que
  de produire un fichier illisible de retours chariot
- ✅ Le collecteur GUI dispose d'une barre équivalente, alimentée depuis le
  thread de collecte via `after()` — seule la boucle Tk touche aux widgets
- ✅ Un rappel qui échoue n'interrompt jamais une collecte d'une minute

### 🔧 CORRECTION
- ✅ **Le collecteur CLI plantait immédiatement sur une console Windows
  française.** Le premier caractère non-ASCII affiché — le « ⚠ » de
  l'avertissement sur les privilèges, ou le « ✓ » de confirmation — levait un
  `UnicodeEncodeError` en cp1252 et interrompait le programme **avant même le
  début de la collecte**. Les deux collecteurs forcent maintenant leur sortie en
  UTF-8 avec repli

---

## [2.6.36] - 2026-08-09 🩺

### 🩺 DE L'INVENTAIRE AU DIAGNOSTIC
La fiche décrivait la configuration d'une machine. Elle décrit maintenant aussi
son **comportement** — ce qui permet de répondre à « ça rame » ou « ça redémarre
tout seul » sans se déplacer.

#### Incidents système (30 jours)
- ✅ Arrêts inattendus, écrans bleus, erreurs disque et corruptions NTFS, relevés
  dans le journal Système et **regroupés par occurrence**
- ✅ Le filtrage se fait sur les paires (fournisseur, identifiant) et non sur
  l'identifiant seul : l'ID 7 vaut « bloc défectueux » chez `disk` et tout autre
  chose chez Hyper-V. Un filtre sur l'ID seul ramenait 200 événements dont
  l'écrasante majorité n'était que du bruit
- ✅ Sur la machine de test : **35 signalements de bloc défectueux** sur un
  disque et un arrêt inattendu, tous deux invisibles jusqu'ici

#### Mises à jour en attente
- ✅ Liste des correctifs non installés, **mises à jour de sécurité distinguées**
- ✅ Recherche dans le cache local (`Online=$false`) : une recherche en ligne
  dépend du réseau et du serveur WSUS et peut dépasser la minute

#### Démarrage, services et partages
- ✅ Programmes lancés au démarrage — la réponse à « le poste est lent »
- ✅ Services en démarrage automatique mais **arrêtés** — la réponse à « le
  service métier ne se lance plus »
- ✅ Partages réseau, en distinguant les partages d'administration (`C$`,
  `ADMIN$`, présents partout) des partages créés à la main

#### Hygiène des comptes
- ✅ Mot de passe sans expiration et date de dernière connexion, en badges
- ✅ L'alerte ne se déclenche que sur les **comptes administrateurs actifs**
  concernés : la signaler compte par compte noierait la liste

#### Matériel
- ✅ **Âge estimé depuis la date du BIOS** — repère de renouvellement, pas une
  date d'achat. Ne demande aucune collecte supplémentaire
- ✅ **Diagonale des écrans en pouces**, calculée depuis les dimensions physiques
  de l'EDID déjà relevées

### 📌 POINTS D'ATTENTION ENRICHIS
Le bandeau intègre désormais : arrêts inattendus, erreurs disque, correctifs de
sécurité en attente, comptes administrateurs à mot de passe sans expiration,
partages exposés et matériel de plus de six ans.

Coût mesuré de l'ensemble : ~4 s pour les diagnostics, chaque bloc isolé de
sorte qu'un échec (droits, service absent) n'empêche pas les autres de remonter.

---

## [2.6.35] - 2026-08-08 🖨️

### 🖨️ IMPRIMANTES TYPÉES
- ✅ Distinction **physique / virtuelle** et **USB / Réseau / Local**, en badges,
  les physiques remontées en tête
- ✅ Le raccordement est déduit du **nom du port**, pas du drapeau `Network` de
  `Win32_Printer` : sur la machine de test, deux imprimantes WSD parfaitement
  réseau y sont rapportées comme non-réseau. `USB001` et `ESDPRT001` (port Epson)
  valent USB, `WSD-…`, `IP_…`, une IP nue ou un chemin UNC valent Réseau
- ✅ 4 imprimantes physiques sur 11 sur la machine de test — les 7 virtuelles
  (PDF, XPS, fax, OneNote, AnyDesk) restent listées mais jamais inventoriées

### 🔌 PÉRIPHÉRIQUES USB DÉTAILLÉS
- ✅ Marque, **modèle**, date de première connexion, version et date du pilote
- ✅ Le modèle vient de `BusReportedDeviceDesc`, le nom que le périphérique
  déclare lui-même : « DCP-195C » au lieu de « Dispositif de stockage de masse
  USB », « CP2102 USB to UART Bridge Controller » au lieu du libellé du pilote
- ✅ Ce nom déclaré **améliore aussi la classification** : une souris Logitech
  M500s qui remontait en « Autre » sous le libellé « Périphérique d'entrée USB »
  est désormais rangée en « Souris » — Windows ne disait « Mouse » que là
- ✅ Coût mesuré : ~14 s pour 40 nœuds, en un seul appel de propriétés par nœud

### 🛡️ ÉTAT RÉEL DE L'ANTIVIRUS
- ✅ Badge **Actif / Inactif / Actif, signatures obsolètes**, par décodage du
  `productState` du Centre de sécurité Windows (protection temps réel et
  fraîcheur des signatures)
- ✅ Jusqu'ici seul le nom du produit était affiché : un antivirus installé mais
  **désactivé** se présentait exactement comme un antivirus opérationnel

### 🎨 LISIBILITÉ
- ✅ Le badge de service des ports TCP passe **à droite du numéro** : carte plus
  compacte, port et service se lisent d'un bloc
- ✅ Les mises à jour Windows ordinaires prennent un **jaune sourd** qui ne
  concurrence plus le rouge des correctifs de sécurité
- ✅ Les tableaux larges (imprimantes, USB) défilent dans leur propre conteneur
  au lieu de pousser la page entière en débordement horizontal

---

## [2.6.34] - 2026-08-08 🏷️

### 🏷️ BADGES ET MISE EN ÉVIDENCE DANS LA FICHE SYSTÈME

#### Pare-feu
- ✅ Chaque profil (Domaine, Privé, Public) affiche un badge **Activé** / **Désactivé**
- ✅ La forme structurée `firewall_profiles` évite de réanalyser la chaîne
  « Domain: Activé » à l'affichage pour décider de la couleur

#### Comptes utilisateurs
- ✅ Tableau avec **type d'utilisateur** (Administrateur / Utilisateur standard)
  et **type de compte** (Local, Microsoft, Microsoft Entra, Domaine), en badges
- ✅ La description du compte est reprise quand Windows en fournit une
- ✅ **Correction importante** : aucun compte n'était jamais signalé comme
  administrateur sur un Windows non anglophone. Le groupe était interrogé par
  son nom, « Administrators », qui n'existe pas sur un Windows français où il
  s'appelle « Administrateurs » — l'appel levait une `GroupNotFoundException`
  avalée silencieusement, et la liste des administrateurs restait vide. Le
  groupe est désormais résolu par son **SID connu `S-1-5-32-544`**, indépendant
  de la langue. Sur la machine de test, 3 comptes administrateurs qui
  n'apparaissaient pas comme tels remontent correctement

#### Correctifs Windows
- ✅ Code couleur par type : les **mises à jour de sécurité** en rouge se
  distinguent des mises à jour ordinaires, des correctifs à chaud et des
  service packs. Utile sur une liste qui dépasse souvent la centaine de lignes

#### Réseau
- ✅ Les **cartes physiques sont mises en évidence** et remontées en tête de
  tableau. Sur un poste avec Hyper-V, WSL, Docker ou un VPN, les interfaces
  virtuelles sont largement majoritaires et noyaient le matériel réel — ici,
  1 carte physique pour 7 virtuelles
- ✅ Chaque carte affiche son **adresse IPv4 et la plage réseau** correspondante
  (192.168.1.101/24 → 192.168.1.0/24). La plage est calculée à la collecte, pas
  à l'affichage, pour que la page et le rapport concordent

---

## [2.6.33] - 2026-08-08 💾

### 💾 UNE BARRE PAR VOLUME
- ✅ Chaque volume logique a désormais sa propre barre d'occupation dans la
  fiche système : pourcentage, espace libre mis en avant, et code couleur
  (vert, orange au-delà de 75 %, rouge au-delà de 90 %)
- ✅ La capacité totale ne donnait qu'une moyenne — sur une machine à plusieurs
  disques, un volume saturé restait invisible derrière un total confortable
- ✅ Les lignes dont le format n'est pas reconnu (macOS, Linux) restent
  affichées telles quelles plutôt que de se voir attribuer un pourcentage
  inventé. Le découpage réutilise `_parse_drive` de `collector_core`, comme le
  PDF

### 🔧 CORRECTION CI
- ✅ **`docker-build-push.yml` supprimé** : réactivé par erreur à la version
  précédente, il faisait double emploi avec le job `docker` de
  `build-release.yml`. Les deux workflows publiaient la même image sur le même
  tag en même temps, l'un en multi-architecture (amd64 + arm64), l'autre en
  amd64 seul — le résultat dépendait de qui finissait en dernier. C'est
  précisément pour cela qu'il avait été désactivé en 9fcc288.
  `build-release.yml` redevient l'unique publieur
- ✅ Toutes les actions GitHub passent aux versions majeures courantes :
  `checkout` v7, `setup-python` v7, `upload-artifact` v7,
  `download-artifact` v8, `docker/setup-buildx-action` v4,
  `docker/login-action` v4, `docker/build-push-action` v7,
  `action-gh-release` v3. Les précédentes ciblaient Node.js 20, déprécié et
  déjà forcé en Node 24 par les runners

---

## [2.6.32] - 2026-08-08 🔌

### 🖱️ INVENTAIRE USB AUTOMATIQUE
- ✅ Les périphériques USB connectés sont détectés et créés dans la section
  Périphériques, aux côtés des écrans et imprimantes déjà gérés en 2.6.31
- ✅ **Regroupement des nœuds PnP** : Windows expose un périphérique physique
  sous plusieurs nœuds (une imprimante multifonction remonte comme
  « composite » + « stockage de masse » + « prise en charge d'impression »).
  Sans regroupement, une seule imprimante aurait créé quatre fiches
- ✅ Classification vers les catégories existantes, avec règle impression +
  numérisation → multifonction. Hubs racine, contrôleurs et nœuds composites
  sont listés dans le rapport mais jamais inventoriés
- ✅ Identité `source_usb_id` (VID:PID + série) : les collectes répétées ne
  créent pas de doublon. Avec numéro de série l'identité vaut pour le client
  entier (le périphérique suit la machine s'il est déplacé) ; sans série elle
  est limitée à la machine, pour que deux souris identiques sur deux postes ne
  fusionnent pas

### 👤 REPORT DE L'UTILISATEUR SUR LES PÉRIPHÉRIQUES
- ✅ L'utilisateur affecté à la fiche appareil est reporté sur **toutes** les
  fiches périphériques rattachées — écrans, imprimantes et USB
- ✅ `appareils.utilisateur` étant du texte libre et `peripheriques.utilisateur_id`
  une clé étrangère, le rapprochement se fait sur le nom dans les deux ordres
  (« Jean Dupont » comme « Dupont Jean »), sans jamais créer d'utilisateur
  fantôme si rien ne correspond
- ✅ Propagation également lors d'une réaffectation ultérieure, sans jamais
  écraser une affectation saisie à la main sur un périphérique précis

### 🔑 CLÉS DE LICENCE COMPLÈTES ET VÉRIFIÉES
- ✅ Les clés récupérées alimentent la section « Licences logiciels » de la
  fiche appareil, sans doublon et sans toucher aux lignes existantes
- ✅ Balayage de toutes les sources : `BackupProductKeyDefault` (clé installée,
  en clair), clé OEM du firmware, décodage du `DigitalProductId`, registre Office
- ✅ **Contrôle de correction** : Windows n'expose que les 5 derniers caractères
  de la clé en service, ils servent de somme de contrôle. Une clé dont la fin
  correspond est certifiée être celle installée ; une clé dont la fin ne
  correspond pas est signalée « non appairée » plutôt que présentée comme la
  licence active (machine OEM réinstallée avec une autre licence)
- ✅ **Correction** : la validation de format rejetait les clés contenant un `N`,
  que l'algorithme Windows 8+ insère pourtant par construction
- ⚠️ Limite assumée : Windows en licence numérique, Office Click-to-Run/365 et
  l'activation KMS ne stockent **aucune** clé sur la machine. Le rapport
  l'indique explicitement au lieu d'afficher une clé factice

### 📊 FICHE SYSTÈME GRAPHIQUE
Le rapport HTML était un listing monospace, le PDF une suite de tableaux :
- ✅ Bandeau **« Points d'attention »** : disque saturé, antivirus absent,
  pare-feu désactivé, TPM, Secure Boot, volume non chiffré, batterie critique
  ou usée, ports sensibles en écoute, licence non activée
- ✅ Vignettes chiffrées à bargraphs — la mémoire affiche l'occupation réelle
  grâce à `ram_free_gb` relevé depuis la 2.6.31
- ✅ Pastilles de sécurité colorées, barres par disque logique
- ✅ **Ports en écoute sous forme de cartes**, colorées par sensibilité. Les
  ports de la plage dynamique (49152+) sont comptés mais pas détaillés
- ✅ Sections barrettes mémoire, fiabilité disques, écrans et imprimantes,
  rendues à partir des données collectées en 2.6.31

#### La page `/appareil/<id>/fiche-systeme` reçoit le même traitement
- ✅ Bandeau **« Points d'attention »** et vignettes à bargraphs en tête de page
- ✅ Ports TCP en écoute affichés **en cartes** colorées par sensibilité, au lieu
  d'une liste inline `8080 (svchost) · 3389 (svchost) · …`
- ✅ Clés de licence **en entier** avec mention de vérification, là où seuls les
  5 derniers caractères étaient montrés
- ✅ Nouvelle section **Périphériques USB**, distinguant ce qui est inventorié de
  la plomberie interne
- ✅ L'analyse (seuils, alertes, criticité des ports) est importée de
  `collector_core` plutôt que réécrite en Jinja : la page et le PDF portent ainsi
  exactement le même jugement sur une machine donnée

### 🔧 CORRECTIONS
- ✅ **Encodage** : la sortie PowerShell était décodée avec l'encodage local
  (cp1252 sur un Windows français) au lieu d'UTF-8. Tout libellé accentué
  remonté par la collecte était silencieusement corrompu — noms de
  périphériques, comptes utilisateurs, adaptateurs réseau, fabricants
- ✅ `.claude/launch.json` déclarait le port 5000 alors que `app.py` écoute
  sur 3456

---

## [2.6.31] - 2026-08-08 🔒 🖥️

### 🖥️ COLLECTEUR SYSTÈME — PARITÉ BELARC ADVISOR

#### Le collecteur ramenait beaucoup et n'en conservait presque rien
L'API ne persistait que 13 colonnes ; tout le reste n'existait que dans le PDF
joint. Par ailleurs, les collecteurs CLI et GUI étaient deux copies des mêmes
~700 lignes, et elles avaient **déjà divergé** : la version GUI avait
silencieusement perdu la détection logicielle `pkgutil` (macOS) et `pacman` (Arch).

- ✅ **`collector_core.py`** porte désormais toute la collecte, la génération de
  rapports, le payload API et les appels réseau. Les deux scripts d'entrée ne
  gardent que leur interface (argparse / tkinter).
- ⚠️ Conséquence : le collecteur n'est plus un fichier autonome, donc
  `/download/system-info-collector[-gui]` sert une **archive ZIP** (script +
  `collector_core.py` + LISEZMOI) au lieu d'un `.py` qui échouerait à l'import.

#### Nouvelles données collectées
- ✅ **Barrettes mémoire par slot** : capacité, type, fréquence réelle, fabricant,
  référence, n° série — plus slots occupés/libres et capacité maximale. Répond à
  « peut-on upgrader cette machine ? » sans ouvrir le boîtier.
- ✅ **Carte mère**, type de châssis, asset tag, **CPU détaillé** (cœurs physiques
  et logiques, cache, sockets, virtualisation matérielle)
- ✅ **GPU avec VRAM lue dans le registre** — `Win32_VideoController.AdapterRAM`
  est un int32 signé qui déborde au-delà de 4 Go
- ✅ **Licences Windows/Office** : statut d'activation, canal, clé partielle, et la
  clé OEM inscrite dans le firmware. `SoftwareLicensingProduct` est interrogé avec
  un filtre WQL — sans filtre, l'énumération dépasse 30 s à elle seule.
- ✅ **Usure et fiabilité des disques** (heures de fonctionnement, usure SSD %,
  température, compteurs d'erreurs) — le statut SMART « Healthy » ne prévenait de rien
- ✅ **Santé réelle de la batterie** (capacité d'origine vs réelle, usure %, cycles)
  depuis les classes du pilote ACPI : `Win32_Battery.DesignCapacity` est presque
  toujours vide, seul le niveau de charge était remonté
- ✅ **Écrans** (modèle, n° série EDID, année), **imprimantes**, liste complète des
  correctifs, date d'installation de l'OS, propriétaire enregistré, fuseau horaire,
  session ouverte, détection d'hyperviseur
- ✅ **macOS et Linux** rapprochés de Windows : barrettes, écrans, imprimantes,
  ports en écoute, comptes locaux, type de disque, cycles batterie, distribution
- ✅ Le collecteur indique s'il a tourné **en administrateur** : un champ TPM,
  BitLocker ou SMART vide ne se confond plus avec un champ inaccessible

#### Exploitation côté serveur
- ✅ Nouvelle colonne **`rapport_systeme_json`** : snapshot complet (plafond 1 Mo)
- ✅ Nouvelle page **`/appareil/<id>/fiche-systeme`** : 11 sections, dont la liste
  logicielle qui était écrite en base et affichée nulle part
- ✅ **Écrans et imprimantes créés automatiquement** dans l'inventaire des
  périphériques et rattachés à la machine, en idempotent sur le n° série. Les
  imprimantes virtuelles (Print to PDF, XPS, fax, OneNote, AnyDesk) et les écrans
  sans EDID exploitable sont exclus — sans ce filtre, un poste Windows standard
  injectait 8 périphériques fantômes à chaque collecte.
- ✅ **`nom_dns`, `ports_ouverts` et `type_appareil`** enfin remplis ; le type est
  déduit du châssis SMBIOS et n'écrase jamais un type corrigé à la main

#### Vérification
Test bout-en-bout sur une machine Windows 10 réelle : 876 logiciels, snapshot de
12,8 Ko, deuxième passe sans aucun doublon (0 périphérique créé, 1 seul appareil),
fiche système rendue avec ses 11 sections, archive de téléchargement conforme.

---

### 🔒 CORRECTIF DE SÉCURITÉ CRITIQUE

#### 109 routes étaient accessibles sans authentification
Sur les 170 routes de l'application, **58 seulement** portaient `@login_required`.
Le point de départ était `/appareil/<id>/documents`, mais l'audit a montré que le
problème était généralisé à presque toute l'application.

Ce n'était pas qu'une fuite en lecture : `get_client_id()` (client_helpers.py)
retombe sur `SELECT id FROM clients ORDER BY id LIMIT 1` quand la session est vide.
Un visiteur anonyme se voyait donc attribuer le **premier client de la base** et
pouvait consulter *et modifier* ses données.

- ✅ Étaient exposés, entre autres :
  - **Documents joints** (rapports système, factures, contrats) — consultation,
    aperçu, téléchargement, upload et suppression, pour les appareils, les
    périphériques et les contrats
  - **`/api/identifiant/<id>/mdp`** — déchiffre et renvoie un mot de passe en clair
  - **CRUD complet** : appareils, périphériques, contrats, services, utilisateurs
    finaux, identifiants, clients, baie de brassage, plans
  - **`/export/global.zip` et `/export/global.json`** — export intégral de la base,
    et `/import/global` qui la réécrit
  - **Administration** : `/admin/utilisateurs`, création/édition/suppression de
    comptes, `/admin/email-config`, partage de client
  - **Import/export CSV** appareils et périphériques, scan réseau, base de
    connaissances, listes de configuration, `/api/config`
- ✅ **`/api/updates/install`** (app_update_routes.py) permettait à n'importe qui sur
  le réseau de déclencher l'installation d'une mise à jour — vecteur d'exécution de
  code à distance
- ✅ Fix : `@login_required` ajouté sur les 105 routes concernées de `app.py` et les
  4 routes de `app_update_routes.py`

#### Endpoints volontairement publics — inchangés
`/login`, `/logout`, `/api/clients-public`, `/api/device-info`,
`/api/device-info/upload-report`, `/download/system-info-collector` et
`/download/system-info-collector-gui` restent accessibles sans session : ce sont les
seuls endpoints appelés par les collecteurs système (vérifié dans
`system-info-collector.py` et `system-info-collector-gui.py`).

#### Vérification
- Anonyme : les 101 routes GET protégées répondent `302 → /login?next=…`, les
  POST/DELETE sensibles sont bloqués
- Connecté : 31 pages et endpoints AJAX testés (dont `/api/config`, `/api/ping/*`,
  `/api/listes/*`, `/api/baie/slots`, `/appareils/export.csv`, `/admin/utilisateurs`)
  répondent tous en 200 — aucune régression sur les appels AJAX des templates

---

## [2.6.21] - 2026-07-05 🔧

### 🔧 CORRECTION

#### 🕐 Décalage horaire sur l'affichage de la surveillance réseau (watchdog)
- ✅ `_watchdog_state['last_cycle']` avait exactement le même défaut que `last_sync`
  corrigé en 2.6.20 : heure locale serveur sans indicateur de fuseau, exposée via
  `/api/ping/summary` et `/api/ping/status`, affichée dans la barre du haut
  ("dernier : HH:MM:SS") avec un décalage correspondant à l'écart de fuseau entre
  serveur et navigateur
- ✅ Fix : heure transmise en UTC avec suffixe `Z` explicite, comme pour la sync Turso

---

## [2.6.20] - 2026-07-05 🔧

### 🔧 CORRECTION

#### 🕐 Décalage horaire sur l'affichage "dernière synchronisation"
- ✅ `last_sync` était stocké en heure locale du serveur, sans indicateur de fuseau —
  le JavaScript l'interprétait alors comme l'heure locale du **navigateur**, provoquant
  un décalage silencieux dès que serveur et navigateur ne sont pas dans le même fuseau
  (ex : serveur en UTC dans Docker, navigateur à Paris → écart de 2h affiché juste
  après une synchronisation qui vient de s'exécuter)
- ✅ Fix : l'heure est désormais transmise en UTC avec un suffixe `Z` explicite et
  non-ambigu ; l'affichage humain (tooltip, panneau paramètres) convertit correctement
  vers l'heure locale du navigateur au lieu d'un simple remplacement de texte

---

## [2.6.19] - 2026-07-05 🔧

### 🔧 CORRECTION CRITIQUE

#### 🔗 Fix sync Turso : la 2.6.18 ne synchronisait pas réellement entre instances
La sync par journal livrée en 2.6.18 ne poussait les changements que dans un seul sens
(local → Turso) et oubliait 9 tables, dont `auth_users` et `client_partages`. Concrètement,
avec plusieurs instances, les modifications faites sur l'une n'apparaissaient jamais sur
les autres — l'inverse de l'objectif recherché.

- ✅ Sync réellement bidirectionnelle : Turso tient son propre `_sync_journal` (mêmes
  triggers répliqués côté Turso), alimenté par les écritures de toutes les instances.
  Chaque instance retient un curseur et ne relit que les entrées plus récentes.
- ✅ Toutes les tables couvertes : ajout de `auth_users`, `client_partages`, `config`,
  `config_listes`, `user_preferences`, `documents_interventions`,
  `interventions_appareils`, `interventions_peripheriques`, `maintenance_notifications`.
- ✅ Plus de perte silencieuse : le journal local n'est purgé que par table et seulement
  après succès du push (retry automatique en cas d'erreur réseau transitoire).
- ✅ Correctif d'un effet de bord découvert en testant le point ci-dessus : appliquer les
  données reçues (pull) re-déclenchait les triggers locaux et pouvait écraser une
  modification distante avec une donnée périmée au cycle suivant — corrigé par une garde
  (`_sync_applying`) désactivant la journalisation pendant l'application du pull.

### 🧹 NETTOYAGE
- Correction des références au port 5000 (obsolète depuis le passage à 3456) dans
  `docker-compose.yml`, `docker-compose.synology.yml` et le template de release CI
- Correction des liens `darkmind64/parc_info` → `darkmind64/parc-info` (404)
- Archivage de 31 documents de session obsolètes (avril 2026) dans `docs/archive/`

---

## [2.6.18] - 2026-07-05 🚀

### 🚀 OPTIMISATION

#### 📉 Réduction drastique de l'utilisation Turso (reads/writes)
- ✅ Intervalle de sync par défaut : 30s → 600s (10 minutes)
- ✅ Change-tracking : nouvelle table `_sync_journal` alimentée par des triggers sur chaque
  table de données — la sync ne lit plus l'intégralité des tables à chaque cycle, seulement
  ce qui a changé depuis le dernier cycle
- ✅ Sync réellement bidirectionnelle : Turso tient son propre `_sync_journal` (répliqué via
  les mêmes triggers), chaque instance retient un curseur et ne relit que les entrées plus
  récentes — corrige un premier jet push-only qui ne propageait pas les changements distants
- ✅ Toutes les tables sont couvertes, y compris `auth_users`, `client_partages`, `config`
  (précédemment absentes de la sync entre instances)
- ✅ Aucune perte silencieuse : le journal local n'est purgé que par table et seulement après
  succès du push (retry automatique en cas d'erreur réseau transitoire)
- ✅ Garde anti-rebouclage (`_sync_applying`) : empêche que l'application des données reçues
  (pull) ne soit elle-même journalisée comme une modification locale

**Impact estimé** : -99% de reads/writes Turso pour un usage à faible fréquence de
modification (quelques dizaines de changements/jour), tout en gardant une synchronisation
complète et fiable entre plusieurs instances.

---

## [2.6.17] - 2026-06-14 🔧

### 🔧 CORRECTIONS

#### 🔗 Fix sync Turso : toutes les tables bloquées en "Request-sent"
- ✅ `TursoConnection.close()` avait un stub `pass` en bas de classe qui **écrasait** l'implémentation réelle (Python prend la dernière définition)
- ✅ Résultat : sur toute erreur réseau, la connexion restait en état "Request-sent", et toutes les tables suivantes levaient `CannotSendRequest` avec le message "Request-sent"
- ✅ Fix : suppression du stub redondant — la vraie méthode `close()` (reset de la socket HTTPS) est maintenant utilisée

---

## [2.6.16] - 2026-06-14 🔧

### 🔧 CORRECTIONS

#### 🌐 Fix perturbation réseau Docker sur Synology (Hyper Backup)
- ✅ Connexion Turso partagée dans `uploads_sync` : 1 seule résolution DNS par cycle (vs 6 avant)
- ✅ `TursoConnection` HTTP keep-alive : la socket TCP reste ouverte entre les requêtes
- ✅ `dns: [8.8.8.8, 1.1.1.1]` dans `docker-compose.yml` : contourne le resolver Docker embarqué qui saturait sur Synology
- ✅ Fix sync uploads : exclusion `contenu_blob` du sync DB (évite timeout 15s), pull blob par blob, reconnaissance de `date_upload`
- ✅ `version.json` inclus dans le build PyInstaller (fix numéro de version sous Windows)

### 📝 NOTES
- Sur Synology, le DNS Docker embarqué (`127.0.0.11`) pouvait saturer sous 6 requêtes DNS consécutives, faisant perdre la connexion à Hyper Backup. Cette version réduit à 1 résolution DNS par cycle de 60s.
- Si tu utilises un `docker-compose.yml` personnalisé, ajoute `dns: [8.8.8.8, 1.1.1.1]` dans le service `parcinfo`.

---

## [2.6.6] - 2026-06-13 🔄

### 🔧 CORRECTIONS

#### 🔗 Sync Turso réactivée par défaut
- ✅ `DISABLE_TURSO_SYNC` revient à `0` par défaut (était `1` depuis v2.6.3)
- ✅ La sync est active si Turso est configuré dans les réglages de l'app
- ✅ Pour désactiver : ajouter `DISABLE_TURSO_SYNC=1` à l'environnement Docker

### 📝 NOTES
- Nécessite configuration Turso dans **Outils → Base de données** pour que la sync fonctionne
- Sans Turso configuré, l'app fonctionne normalement en mode local

---

## [2.6.5] - 2026-06-13 🔧

### 🔧 CORRECTIONS

#### 🚀 Remplacement Gunicorn → Werkzeug (Docker)
- ✅ Suppression de Gunicorn — workers crashaient avec code 255 sur Synology
- ✅ Werkzeug `threaded=True, use_reloader=False` : stable et performant
- ✅ Démarrage plus rapide et fiable sur NAS ARM/x86
- ✅ Gunicorn reste dans `requirements.txt` mais n'est plus utilisé

---

## [2.6.4] - 2026-06-13 🔧

### 🔧 CORRECTIONS

#### ⚙️ Gunicorn worker class gthread
- ✅ Tentative de correction crash Gunicorn (code 255) avec `worker_class=gthread`
- ⚠️ Cette approche a été remplacée en v2.6.5 (Werkzeug)

---

## [2.6.3] - 2026-06-12 🔧

### 🔧 CORRECTIONS

#### 🌐 Résolution DNS au démarrage
- ✅ `DISABLE_TURSO_SYNC=1` par défaut pour éviter erreurs DNS si Turso non configuré
- ✅ Démarrage Docker propre sans tentatives de connexion Turso échouées
- ⚠️ Cette valeur a été inversée en v2.6.6

---

## [2.6.2] - 2026-06-12 🔧

### 🔧 CORRECTIONS

#### 🛠️ Ajout Gunicorn pour Synology
- ✅ Gunicorn ajouté comme serveur WSGI pour Docker
- ✅ Meilleure gestion multi-connexions
- ⚠️ Problèmes de crash workers sur Synology — remplacé par Werkzeug en v2.6.5

---

## [2.6.1] - 2026-06-12 🔧

### 🔄 CHANGEMENTS

#### 🔌 Port par défaut mis à jour
- ✅ Port changé de 5000 → 3456
- ✅ Meilleure compatibilité de déploiement
- ✅ Détection automatique de port libre en fallback (launcher.py)
- ✅ Application de développement (app.py)
- ✅ Container Docker (Dockerfile)
- ✅ Tests et validation (scripts de test)

### 📝 NOTES DE MIGRATION
- Mettre à jour les configurations Docker Compose/Kubernetes qui utilisent le port 5000
- Mettre à jour les firewall rules/reverse proxy pour le port 3456
- Les déploiements existants utilisant port 5000 seront affectés

---

## [2.6.0] - 2026-05-06 📋

### ✨ NOUVELLES FONCTIONNALITÉS

#### 🏷️ Générateur d'Étiquettes QR (AVERY J8159)
- ✅ Génération de codes QR avec données d'assets en texte brut
- ✅ Support format AVERY J8159 (63.5×33.9mm, grille 3×8 = 24 labels)
- ✅ Sélection d'appareil ou périphérique pour génération
- ✅ Choix multi-checkbox des paramètres à encoder
- ✅ Customisation complète : logo, texte (header/asset/footer), couleurs, polices
- ✅ Positionnement précis sur le sheet (positions 1-24)
- ✅ Génération de copies multiples per position
- ✅ Export PDF prêt pour impression
- ✅ Contrôles texte avancés : taille dynamique, couleur, police, justification
- ✅ Grille de positionnement visuelle interactive
- ✅ Aperçu en temps réel du label

#### 📱 QR Code Format Lisible
- ✅ Format texte brut (pas JSON) - compatible scanners téléphone
- ✅ Phone barcode scanner affiche le contenu directement
- ✅ Plus de message "rechercher un code barre"
- ✅ Données structurées avec libellés français
- Exemple:
  ```
  Nom: DESKTOP-ABC123
  IP: 192.168.1.50
  MAC: AA:BB:CC:DD:EE:FF
  User: admin
  Password: SecurePass123
  ...
  ```

#### 🎨 Interface & Design
- ✅ Page complète avec formulaire intuitif
- ✅ Responsive design + Dark/Light mode support
- ✅ Variables CSS pour cohérence avec l'app
- ✅ Navigation intégrée ("Étiquettes QR" en sidebar Inventaire)
- ✅ Grille étiquettes avec fond blanc persistant

#### 🔐 Sécurité
- ✅ Vérification ACL avant génération
- ✅ Isolation client_id stricte
- ✅ Déchiffrement des credentials depuis BD
- ✅ Audit logging dans historique
- ✅ Validation input complète

### 📦 NOUVELLES DÉPENDANCES

```
qrcode[pil]>=8.0            # QR code generation with PIL
```

### 📋 FICHIERS NOUVEAUX

```
qrcode_helper.py             # Utilities for QR generation, label & PDF creation (489 lines)
  - generate_qr()            # Generate QR from asset data (plain text format)
  - create_label_image()     # Create label image with logo, QR, text
  - create_pdf_sheet()       # Create AVERY J8159 PDF sheet

templates/qrcode_generator.html  # Form UI for label generation (1572 lines)
  - Asset selection (appareil/périphérique)
  - Parameter checkboxes (grouped by category)
  - Logo upload & customization
  - Text controls (Header, Asset, Footer - separated)
  - Position grid (3×8 interactive)
  - PDF generation & download
```

### 📋 FICHIERS MODIFIÉS

```
app.py                       # +354 lines
  - @app.route('/qrcode-labels')              # Display form
  - @app.route('/qrcode-labels/fields')       # AJAX: get available fields
  - @app.route('/qrcode-labels/preview')      # AJAX: generate preview PNG
  - @app.route('/qrcode-labels/generate')     # Generate PDF download

templates/base.html          # +3 lines
  - Navigation link: "📋 Étiquettes QR"

requirements.txt             # +1 line
  - qrcode[pil]>=8.0
```

### 🧪 TESTS & VALIDATION

- ✅ QR code generation with full asset data
- ✅ Plain text format verification
- ✅ Empty/sparse field handling
- ✅ Flask app imports successfully
- ✅ Syntax check passed
- ✅ Manual testing: Phone barcode scanner displays text correctly

### 🔍 NOTES

- QR content format changed from JSON to plain text for phone compatibility
- User responsibility: Physical security of labels (credentials in cleartext)
- Maximum QR data size: ~300 bytes (auto-scaled QR version)
- Logo handling: PNG/JPG, 0-30mm, auto-positioning
- Position grid: Interactive selection, multi-copy support (1-10 copies per position)

### 📊 IMPACT UTILISATEUR

| Aspect | Impact |
|--------|--------|
| Asset tracking | Meilleure avec QR codes imprimables |
| Mobile scanning | Plus d'erreurs "rechercher un code barre" |
| Customization | Logos, textes, couleurs personnalisables |
| Workflow | Étiquettes AVERY standard, prêtes à imprimer |

---

## [2.5.0] - 2026-04-23 🚀

### ✨ NOUVELLES FONCTIONNALITÉS

#### 🔐 Chiffrement des Identifiants
- ✅ Implémentation Fernet (AES-128)
- ✅ Chiffrement automatique des credentials
- ✅ Migration transparente des données existantes
- ✅ Déchiffrement sécurisé à l'affichage

#### 🔍 Recherche Full-Text Globale
- ✅ Barre de recherche dans la navbar
- ✅ Recherche multi-entités en temps réel
- ✅ Résultats groupés par type (appareils, contrats, services, etc.)
- ✅ Navigation directe vers entités trouvées
- ✅ Performance: 5ms (ultra-rapide)

#### ⚡ Autocomplete Dynamique
- ✅ Suggestions en temps réel dans les formulaires
- ✅ Intégration TomSelect
- ✅ Support: appareils, contrats, services, périphériques, utilisateurs
- ✅ Performance: 6ms
- ✅ API `/api/autocomplete/<type>`

### ⚙️ OPTIMISATIONS DE PERFORMANCE

#### 1. Indexation Base de Données
- ✅ 66 indexes SQLite créés
- ✅ Couverture: client_id, clés étrangères, colonnes fréquentes
- ✅ Amélioration: ~60% d'accélération requêtes
- ✅ Impact: Toutes les listes maintenant < 50ms

#### 2. Compression Réseau
- ✅ Flask-Compress activé
- ✅ Brotli (meilleur que Gzip)
- ✅ Réduction bande passante: ~70%
- ✅ Impact: Assets plus légers

#### 3. Cache en Mémoire (TTL)
- ✅ CacheManager avec expiration temporelle
- ✅ TTL configurable par type (5-15 min)
- ✅ Décorateur `@cache_result` pour fonctions
- ✅ Invalidation par pattern
- ✅ Impact: Réduction requêtes DB ~40%

#### 4. Audit Trail Complet
- ✅ Table `historique` pour tous changements
- ✅ 209+ entrées déjà loggées
- ✅ Catégories: Création, Modification, Suppression, Confirmation, Erreur
- ✅ Traçabilité complète

### 📦 NOUVELLES DÉPENDANCES

```
cryptography>=41.0.0        # Fernet encryption
flask-compress>=1.14.0      # Gzip/Brotli compression
```

### 📊 MÉTRIQUES DE PERFORMANCE

| Métrique | Avant | Après | Amélioration |
|----------|-------|-------|--------------|
| Temps accueil | ~80ms | 15ms | **-81%** |
| Temps appareils | ~150ms | 6ms | **-96%** |
| Temps recherche | N/A | 5ms | **Nouveau** |
| Temps autocomplete | N/A | 6ms | **Nouveau** |
| Bande passante | 100% | ~30% | **-70%** |
| Requêtes DB | Baseline | -40% | **Cache TTL** |

### 🔒 SÉCURITÉ

- ✅ 100% credentials chiffrés (Fernet AES-128)
- ✅ CSRF token sur tous formulaires
- ✅ ACL multi-client stricte
- ✅ Rate-limiting authentification (10/5min)
- ✅ Validation input systématique
- ✅ Audit trail complet

### 📋 FICHIERS NOUVEAUX

```
search_utils.py              # Recherche full-text + autocomplete
cache_utils.py               # Cache manager avec TTL
crypto_utils.py              # Chiffrement Fernet
validate_optimizations.py    # Script de validation (85% succès)
VALIDATION_REPORT.md         # Rapport détaillé
```

### 📋 FICHIERS MODIFIÉS

```
app.py                       # +3 imports, +2 endpoints API, +66 indexes
base.html                    # +Barre recherche, +TomSelect, +JS search
form_maintenance.html        # +IDs pour autocomplete
requirements.txt             # +cryptography, +flask-compress
```

### 🧪 TESTS & VALIDATION

- ✅ 17/20 tests passés (85%)
- ✅ Indexation DB: 100%
- ✅ Chiffrement: 100%
- ✅ Compression: 100%
- ✅ Recherche: 100%
- ✅ Autocomplete: 100%
- ✅ Audit Trail: 100%
- ✅ Performance: 100%

### 📚 DOCUMENTATION

- ✅ VALIDATION_REPORT.md - Rapport complet
- ✅ VERSION - Numéro et metadata
- ✅ CHANGELOG.md - Ce fichier
- ✅ CLAUDE.md - Guide technique (existant)
- ✅ README.md - Installation (existant)

### 🚀 DÉPLOIEMENT

**Prérequis:**
```bash
pip install -r requirements.txt
```

**Migration:**
```bash
# Automatique au démarrage
# - Création indexes
# - Chiffrement credentials existants
# - Création table historique
```

**Vérification:**
```bash
python validate_optimizations.py
```

### ⚠️ NOTES DE COMPATIBILITÉ

- ✅ Compatible Python 3.8+
- ✅ SQLite 3.8+ (auto-création indexes)
- ✅ Rétro-compatible (pas de breaking changes)
- ✅ Migration transparente credentials

### 🎯 PROCHAINES ÉTAPES (v2.6.0)

- 📊 Dashboard analytics (hits cache, requêtes slow, etc.)
- 🔔 Notifications pré-maintenance
- 📈 Graphiques historiques
- ⚙️ Configuration UI avancée
- 🗄️ Support PostgreSQL optionnel

---

## [2.4.0] - 2026-04-20

### ✨ Maintenance Module
- Planification maintenances
- Récurrence (hebdo/mensuel/annuel)
- Rapport d'historique
- Multi-client support

---

## [2.3.0] - 2026-04-15

### ✨ Intervention Tracking
- Gestion interventions
- Timesheets techniciens
- Rapport intervention

---

*Pour l'historique complet, voir le fichier historique en base de données.*
