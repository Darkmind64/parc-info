#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bench du collecteur SNMP unifie (refonte diagnostic reseau, Lot 1).

Compare le balayage SNMP du palier 3 AVANT (boucle sequentielle de
`interroger_equipement` : 3 GETBULK + 1 GET par equipement) et APRES
(`netdiag.collect.balayer` : 1 GETBULK multi-colonnes par equipement, en
parallele).

Les primitives SNMP d'`app` sont remplacees par des bouchons qui dorment
`latence` secondes pour simuler un aller-retour reseau -- pas de vrai socket.

    python bench_collecte.py [nb_equipements] [latence_s]
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

LAT = 0.12


def _installer_bouchons(latence):
    import app as A

    def _fake_bulk_cols(ip, bases, comm=('public',), timeout=1.5, **k):
        time.sleep(latence)
        out = {}
        for b in bases:
            if b.startswith('1.3.6.1.2.1.2.2.1.2'):
                out[b] = {str(i): f'Gi1/0/{i}' for i in range(1, 49)}
            elif b.startswith('1.3.6.1.2.1.2.2.1.3'):
                out[b] = {str(i): 6 for i in range(1, 49)}
            elif b.startswith('1.3.6.1.2.1.2.2.1.7') or b.startswith('1.3.6.1.2.1.2.2.1.8'):
                out[b] = {str(i): 1 for i in range(1, 49)}
            elif b.startswith('1.3.6.1.2.1.31.1.1.1.15'):
                out[b] = {str(i): 1000 for i in range(1, 49)}
            elif b.startswith('1.3.6.1.2.1.31.1.1.1.6') or b.startswith('1.3.6.1.2.1.31.1.1.1.10'):
                out[b] = {str(i): 1_000_000 * i for i in range(1, 49)}
            else:
                out[b] = {str(i): 0 for i in range(1, 49)}
        return out

    def _fake_presence(ip, communautes=('public',), port=161, timeout=1.2):
        time.sleep(latence)
        return True, True, 'v1/v2c (public)'

    def _fake_get(ip_str, oids, communaute='public', timeout=0.8, port=161):
        time.sleep(latence)
        return {A._OID_SYS_NAME: 'SW-BENCH'}

    def _fake_get_typed(ip_str, oids, communaute='public', timeout=1.0, port=161, **k):
        time.sleep(latence)
        return {A._OID_SYS_NAME: 'SW-BENCH', A._OID_SYS_DESCR: 'bench'}

    A._snmp_bulk_cols = _fake_bulk_cols
    A._snmp_presence = _fake_presence
    A._snmp_get = _fake_get
    A._snmp_get_typed = _fake_get_typed


def bench(n, latence):
    _installer_bouchons(latence)
    import network_diag as N
    from netdiag import collect

    equipements = [(1000 + i, f'10.9.{i // 254}.{i % 254 + 1}', 'Switch') for i in range(n)]

    t0 = time.time()
    avant_ports = 0
    for _aid, ip, _ta in equipements:
        eq = N.interroger_equipement(ip, ['public'])
        if eq:
            avant_ports += len(eq['ports'])
    d_avant = time.time() - t0

    collect.vider_cache()

    res = collect.balayer(0, besoins=('compteurs', 'dot3'), communautes=['public'],
                          equipements=equipements)
    d_apres = res.duree_s
    apres_ports = sum(len(rv.equipement['ports']) for rv in res.releves.values() if rv.equipement)

    ratio = d_avant / d_apres if d_apres else float('inf')
    print(f"  {n:>3} equipements x 48 ports, latence {latence*1000:.0f} ms/aller-retour")
    print(f"    AVANT (sequentiel)  : {d_avant:6.2f} s   ({avant_ports} ports)")
    print(f"    APRES (collecteur)  : {d_apres:6.2f} s   ({apres_ports} ports)")
    print(f"    gain                : x{ratio:.1f}\n")
    return d_apres, ratio


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    lat = float(sys.argv[2]) if len(sys.argv) > 2 else LAT
    print("=== Bench collecteur SNMP unifie (Lot 1) ===")
    tailles = (5, 20) if n == 20 else (n,)
    d20 = r20 = None
    for taille in tailles:
        d, r = bench(taille, lat)
        if taille == 20:
            d20, r20 = d, r
    if d20 is None:
        d20, r20 = bench(20, lat)
    ok = d20 < 8.0 and r20 >= 4.0
    print(f"Critere (20 equip. < 8 s ET au moins x4) : {'OK' if ok else 'ECHEC'} "
          f"({d20:.2f} s, x{r20:.1f})")
    sys.exit(0 if ok else 1)
