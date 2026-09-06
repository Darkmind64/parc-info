#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bench des sondes hote du diagnostic reseau (refonte, Lot 7 - performance).

Les paliers 1/2/7a (ARP, rafales de ping, DNS, DHCP, NetBIOS, Wi-Fi) tournaient
en SERIE dans `_run_snapshot`. Sous Windows, `ping -n N` envoie ~1 paquet/s :
3 cibles en serie = 3x la rafale. `_sondes_hote` (`_executer_sondes`) les lance
en parallele -> le palier ne dure que la plus lente.

`_ping_rafale` et les autres sondes sont remplaces par des bouchons qui dorment
`latence` secondes -- pas de vrai socket.

    python bench_sondes.py [nb_cibles] [latence_s]
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def main():
    nb_cibles = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    latence = float(sys.argv[2]) if len(sys.argv) > 2 else 1.2  # ~ ping -n 12 sous Windows

    import network_diag as N

    cibles = [{'ip': f'10.0.0.{i + 1}', 'libelle': f'C{i}', 'role': 'perso'}
              for i in range(nb_cibles)]
    N._ping_rafale = lambda ip, n=20: (time.sleep(latence) or {
        'ip': ip, 'envoyes': n, 'recus': n, 'perte_pct': 0.0,
        'min': 1.0, 'moy': 1.0, 'max': 1.0, 'gigue': 0.0})
    N._passerelle_defaut = lambda: '10.0.0.254'
    N._cibles_ping = lambda cid, p: cibles
    N._cfg = lambda k, d=None: '0'          # Wi-Fi off pour isoler la mesure
    N.detecter_conflits_ip = lambda *a, **k: (time.sleep(latence) or [])
    N.verifier_dns = lambda *a, **k: (time.sleep(latence) or [])
    N.detecter_dhcp_pirate = lambda *a, **k: (time.sleep(latence) or [])
    N.detecter_conflits_noms = lambda *a, **k: (time.sleep(latence) or [])

    # AVANT : enchainement serie des memes sondes.
    t0 = time.time()
    N.detecter_conflits_ip('10.0.0.254', releves=2)
    for c in cibles:
        N._ping_rafale(c['ip'], 12)
    N.verifier_dns('1.1.1.1')
    N.detecter_dhcp_pirate([])
    N.detecter_conflits_noms(1)
    serie = time.time() - t0

    # APRES : _sondes_hote (parallele).
    t0 = time.time()
    N._sondes_hote(1, n_ping=12, releves_arp=2)
    paralle = time.time() - t0

    print(f"cibles={nb_cibles}  latence/sonde={latence}s")
    print(f"  serie    : {serie:6.2f} s")
    print(f"  parallele: {paralle:6.2f} s   (x{serie / paralle:.1f})")


if __name__ == '__main__':
    main()
