"""
/api/instance-info — identification publique d'une instance pour le
balayage réseau du collecteur (scan_network_for_parcinfo, collector_core.py).
"""
import socket


def test_instance_info_ok_sans_auth(client):
    """Pas de session, pas de jeton : la route reste ouverte (comme le TXT
    record mDNS qu'elle complète pour les instances injoignables en mDNS)."""
    r = client.get('/api/instance-info')
    assert r.status_code == 200
    data = r.get_json()
    assert data['hostname'] == socket.gethostname()
    assert 'version' in data
    assert isinstance(data['docker'], bool)


def test_instance_info_docker_flag(client, monkeypatch):
    # conftest.py force RUNNING_IN_DOCKER=1 pour toute la suite — on vérifie
    # les deux sens du drapeau plutôt que de supposer une valeur par défaut.
    monkeypatch.setenv('RUNNING_IN_DOCKER', '1')
    assert client.get('/api/instance-info').get_json()['docker'] is True
    monkeypatch.delenv('RUNNING_IN_DOCKER', raising=False)
    assert client.get('/api/instance-info').get_json()['docker'] is False
