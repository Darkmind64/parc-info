"""
Performance du ping — _tcp_probe_rapide (fallback TCP de _ping()/_ping_once()).

Le fallback TCP d'un hôte injoignable essayait autrefois un port après
l'autre, chacun jusqu'à son propre timeout : pire cas = N × timeout, plusieurs
secondes par hôte pour la plupart des adresses inutilisées d'un scan réseau,
ou pour tout appareil dont l'ICMP est filtré (pare-feu Windows par défaut).
Sondés en parallèle, le pire cas doit rester borné à ~1 timeout quel que soit
le nombre de ports, et retourner sans attendre les ports encore en cours dès
qu'un port a déjà répondu.

Sockets mockés (aucun accès réseau réel) : le comportement testé est le
parallélisme et le retour anticipé, pas la couche TCP elle-même.
"""
import time
from unittest.mock import MagicMock, patch

import app as A


def _fabrique_sockets_lents(delai, reussit_sur=None):
    """Fabrique de sockets factices : connect_ex() attend `delai` secondes
    puis échoue (retourne 1), sauf pour le port `reussit_sur` (succès
    immédiat, sans attendre) si fourni."""
    def fabrique(*a, **kw):
        s = MagicMock()

        def connect_ex(addr):
            _ip, port = addr
            if reussit_sur is not None and port == reussit_sur:
                return 0
            time.sleep(delai)
            return 1
        s.connect_ex.side_effect = connect_ex
        return s
    return fabrique


def test_tcp_probe_parallele_borne_au_pire_timeout_pas_a_la_somme():
    ports = [10001, 10002, 10003, 10004, 10005, 10006]
    delai = 0.3
    with patch('app.socket.socket', side_effect=_fabrique_sockets_lents(delai)):
        t0 = time.monotonic()
        ok = A._tcp_probe_rapide('192.0.2.1', ports, timeout=delai)
        elapsed = time.monotonic() - t0
    assert ok is False
    # Séquentiel aurait pris len(ports)*delai = 1.8s ; en parallèle, borné à
    # ~1 délai + petite marge, quel que soit le nombre de ports.
    seuil = delai * len(ports) * 0.6
    assert elapsed < seuil, (
        f"trop lent ({elapsed:.2f}s, seuil {seuil:.2f}s) : "
        "le fallback TCP ne tourne pas en parallèle"
    )


def test_tcp_probe_retourne_des_le_premier_succes_sans_attendre_les_autres():
    ports = [10001, 10002, 10003, 10004, 10005]
    with patch('app.socket.socket', side_effect=_fabrique_sockets_lents(0.6, reussit_sur=10003)):
        t0 = time.monotonic()
        ok = A._tcp_probe_rapide('192.0.2.1', ports, timeout=1.0)
        elapsed = time.monotonic() - t0
    assert ok is True
    # Les 4 autres ports sont encore à mi-parcours de leur délai de 0.6s à ce
    # stade : si la fonction attendait leur fin (bug corrigé — Executor
    # utilisé sans `with`, shutdown(wait=False)), ce test dépasserait 0.5s.
    assert elapsed < 0.4, f"n'a pas retourné dès le succès ({elapsed:.2f}s)"


def test_tcp_probe_tous_les_ports_ferment_rapidement():
    """Ports qui répondent vite (RST immédiat, port fermé) : pas de délai
    artificiel à attendre, la fonction doit revenir quasi instantanément."""
    ports = [10001, 10002, 10003]
    with patch('app.socket.socket', side_effect=_fabrique_sockets_lents(0)):
        t0 = time.monotonic()
        ok = A._tcp_probe_rapide('192.0.2.1', ports, timeout=0.3)
        elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 0.2
