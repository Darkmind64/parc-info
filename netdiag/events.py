"""netdiag.events — cycle de vie des évènements de diagnostic (Lot 2).

Pour l'instant : **auto-résolution** des évènements d'erreur de port SNMP.
Avant la refonte, un évènement `port_crc` / `duplex_mismatch` / … restait
« actif » jusqu'à ce qu'un humain clique « résoudre », même une fois le câble
changé et la condition disparue depuis des jours.

`auto_resoudre_snmp` clôt un tel évènement quand **sa condition n'a pas été
revue depuis assez longtemps** ET que **l'équipement concerné est toujours
relevé** (sinon on résoudrait un problème simplement parce que le switch est
devenu injoignable). Le reste de la logique d'évènements (`_enregistrer_evenements`,
`_alerter_email`, `_appareil_pour_finding`) reste dans `network_diag` — Lot 6.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger('parcinfo')

# Catégories éligibles : un défaut de port qui se corrige silencieusement.
_CATEGORIES_PORT = ('duplex_mismatch', 'port_crc', 'port_erreurs', 'port_sature',
                    'port_flapping', 'vitesse_reduite')

# Délais par défaut (secondes) — un évènement de port se résout seul si sa
# condition n'a pas reparu depuis `_ABSENCE_S` sur un équipement vu il y a moins
# de `_EQUIP_FRAIS_S`.
_ABSENCE_S = 1800.0        # ~15 cycles SNMP à 120 s
_EQUIP_FRAIS_S = 600.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec='seconds').replace('+00:00', 'Z')


def auto_resoudre_snmp(client_id: int, absence_s: float | None = None,
                       equip_frais_s: float = _EQUIP_FRAIS_S) -> int:
    """Clôt les évènements de port SNMP dont la condition a disparu depuis
    `absence_s` sur un équipement relevé il y a moins de `equip_frais_s`.
    Retourne le nombre d'évènements résolus. `absence_s=None` → clé de config
    `diag_snmp_auto_resolution_s` (0 = auto-résolution désactivée)."""
    try:
        from database import get_db
    except Exception:
        return 0
    if absence_s is None:
        try:
            import network_diag
            absence_s = network_diag._cfg_int('diag_snmp_auto_resolution_s', int(_ABSENCE_S))
        except Exception:
            absence_s = _ABSENCE_S
    if not absence_s or absence_s <= 0:
        return 0
    now = time.time()
    now_z = _iso(now)
    limite_absence = _iso(now - absence_s)
    limite_frais = _iso(now - equip_frais_s)
    conn = get_db()
    try:
        ph = ','.join('?' * len(_CATEGORIES_PORT))
        # Équipements relevés récemment ET joignables (leur état est fiable).
        ips_frais = {r[0] for r in conn.execute(
            "SELECT equipement_ip FROM diag_etat_equipement "
            "WHERE client_id=? AND snmp_ok=1 AND derniere_maj >= ?",
            (client_id, limite_frais))}
        if not ips_frais:
            return 0
        cands = conn.execute(
            f"SELECT id, equipement_ip FROM diag_reseau_evenements "
            f"WHERE client_id=? AND resolu=0 AND categorie IN ({ph}) "
            f"AND derniere_occurrence < ?",
            (client_id, *_CATEGORIES_PORT, limite_absence)).fetchall()
        a_resoudre = [eid for eid, ip in cands if ip in ips_frais]
        if not a_resoudre:
            return 0
        qmarks = ','.join('?' * len(a_resoudre))
        conn.execute(
            f"UPDATE diag_reseau_evenements SET resolu=1, date_resolu=? "
            f"WHERE id IN ({qmarks})", (now_z, *a_resoudre))
        conn.commit()
        logger.info('netdiag.events: %d évènement(s) de port SNMP auto-résolu(s) '
                    '(client %s)', len(a_resoudre), client_id)
        return len(a_resoudre)
    except Exception:
        logger.debug('netdiag.events: auto_resoudre_snmp en échec', exc_info=True)
        return 0
    finally:
        conn.close()
