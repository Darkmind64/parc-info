"""
Module de recherche full-text pour ParcInfo.
Recherche rapide sur appareils, contrats, utilisateurs, services, etc.
"""

import logging
from database import get_db, row_to_dict
from typing import Dict, List, Any

logger = logging.getLogger('parcinfo')

def search_global(query: str, client_id: int, limit: int = 20) -> Dict[str, List[Dict]]:
    """
    Effectue une recherche globale sur toutes les entités.

    Args:
        query: Texte de recherche (minimum 2 caractères)
        client_id: ID du client actif
        limit: Nombre de résultats par type

    Returns:
        Dict avec résultats par catégorie
    """
    if not query or len(query) < 2:
        return {
            'appareils': [],
            'contrats': [],
            'utilisateurs': [],
            'services': [],
            'peripheriques': [],
            'identifiants': [],
            'total': 0
        }

    query_pattern = f'%{query}%'
    results = {
        'appareils': [],
        'contrats': [],
        'utilisateurs': [],
        'services': [],
        'peripheriques': [],
        'identifiants': [],
        'total': 0
    }

    try:
        conn = get_db()

        # ── APPAREILS ──────────────────────────────────────────────────────
        appareils = conn.execute('''
            SELECT id, nom_machine as nom, type_appareil, adresse_ip
            FROM appareils
            WHERE client_id=? AND (
                nom_machine LIKE ? OR
                adresse_ip LIKE ? OR
                numero_serie LIKE ? OR
                marque LIKE ?
            )
            LIMIT ?
        ''', (client_id, query_pattern, query_pattern, query_pattern, query_pattern, limit)).fetchall()

        results['appareils'] = [{
            'id': row[0],
            'nom': row[1],
            'type': row[2],
            'detail': row[3],
            'url': f'/appareil/{row[0]}/editer'
        } for row in appareils]

        # ── CONTRATS ───────────────────────────────────────────────────────
        contrats = conn.execute('''
            SELECT id, titre, fournisseur, type_contrat
            FROM contrats
            WHERE client_id=? AND (
                titre LIKE ? OR
                fournisseur LIKE ? OR
                numero_contrat LIKE ?
            )
            LIMIT ?
        ''', (client_id, query_pattern, query_pattern, query_pattern, limit)).fetchall()

        results['contrats'] = [{
            'id': row[0],
            'nom': row[1],
            'type': row[3],
            'detail': row[2],
            'url': f'/contrat/{row[0]}'
        } for row in contrats]

        # ── UTILISATEURS ───────────────────────────────────────────────────
        utilisateurs = conn.execute('''
            SELECT id, prenom, nom, email, poste
            FROM utilisateurs
            WHERE client_id=? AND (
                prenom LIKE ? OR
                nom LIKE ? OR
                email LIKE ?
            )
            LIMIT ?
        ''', (client_id, query_pattern, query_pattern, query_pattern, limit)).fetchall()

        results['utilisateurs'] = [{
            'id': row[0],
            'nom': f"{row[1]} {row[2]}".strip(),
            'detail': row[4] or row[3],
            'url': f'/utilisateur/{row[0]}/editer'
        } for row in utilisateurs]

        # ── SERVICES ───────────────────────────────────────────────────────
        services = conn.execute('''
            SELECT id, nom, description
            FROM services
            WHERE client_id=? AND (
                nom LIKE ? OR
                description LIKE ?
            )
            LIMIT ?
        ''', (client_id, query_pattern, query_pattern, limit)).fetchall()

        results['services'] = [{
            'id': row[0],
            'nom': row[1],
            'type': 'Service',
            'detail': row[2],
            'url': f'/service/{row[0]}/editer'
        } for row in services]

        # ── PÉRIPHÉRIQUES ──────────────────────────────────────────────────
        peripheriques = conn.execute('''
            SELECT id, categorie, marque, modele, numero_serie
            FROM peripheriques
            WHERE client_id=? AND (
                marque LIKE ? OR
                modele LIKE ? OR
                numero_serie LIKE ? OR
                categorie LIKE ?
            )
            LIMIT ?
        ''', (client_id, query_pattern, query_pattern, query_pattern, query_pattern, limit)).fetchall()

        results['peripheriques'] = [{
            'id': row[0],
            'nom': f"{row[1]}: {row[2]} {row[3]}",
            'detail': row[4],
            'url': f'/peripherique/{row[0]}/editer'
        } for row in peripheriques]

        # ── IDENTIFIANTS ───────────────────────────────────────────────────
        identifiants = conn.execute('''
            SELECT id, nom, categorie, login, url
            FROM identifiants
            WHERE client_id=? AND (
                nom LIKE ? OR
                categorie LIKE ? OR
                login LIKE ?
            )
            LIMIT ?
        ''', (client_id, query_pattern, query_pattern, query_pattern, limit)).fetchall()

        results['identifiants'] = [{
            'id': row[0],
            'nom': row[1],
            'type': row[2],
            'detail': row[3],
            'url': f'/identifiant/{row[0]}/popup'
        } for row in identifiants]

        conn.close()

        # Compter le total
        results['total'] = sum(len(v) for k, v in results.items() if k != 'total')

        return results

    except Exception as e:
        logger.error(f"Erreur recherche globale: {e}")
        return results


def search_autocomplete(query: str, client_id: int, entity_type: str, limit: int = 10) -> List[Dict]:
    """
    Autocomplete pour un type d'entité spécifique.

    Args:
        query: Texte de recherche
        client_id: ID du client
        entity_type: Type d'entité (appareil, contrat, utilisateur, service, etc.)
        limit: Nombre de résultats

    Returns:
        Liste des résultats formatés pour autocomplete
    """
    if not query or len(query) < 2:
        return []

    query_pattern = f'%{query}%'
    results = []

    try:
        conn = get_db()

        if entity_type == 'appareil':
            rows = conn.execute('''
                SELECT id, nom_machine, adresse_ip
                FROM appareils
                WHERE client_id=? AND nom_machine LIKE ?
                LIMIT ?
            ''', (client_id, query_pattern, limit)).fetchall()

            results = [{
                'id': row[0],
                'text': f"{row[1]} ({row[2]})" if row[2] else row[1],
                'value': row[0]
            } for row in rows]

        elif entity_type == 'contrat':
            rows = conn.execute('''
                SELECT id, titre, fournisseur
                FROM contrats
                WHERE client_id=? AND titre LIKE ?
                LIMIT ?
            ''', (client_id, query_pattern, limit)).fetchall()

            results = [{
                'id': row[0],
                'text': f"{row[1]} - {row[2]}" if row[2] else row[1],
                'value': row[0]
            } for row in rows]

        elif entity_type == 'utilisateur':
            rows = conn.execute('''
                SELECT id, prenom, nom, email
                FROM utilisateurs
                WHERE client_id=? AND (prenom LIKE ? OR nom LIKE ? OR email LIKE ?)
                LIMIT ?
            ''', (client_id, query_pattern, query_pattern, query_pattern, limit)).fetchall()

            results = [{
                'id': row[0],
                'text': f"{row[1]} {row[2]} ({row[3]})" if row[3] else f"{row[1]} {row[2]}",
                'value': row[0]
            } for row in rows]

        elif entity_type == 'service':
            rows = conn.execute('''
                SELECT id, nom
                FROM services
                WHERE client_id=? AND nom LIKE ?
                LIMIT ?
            ''', (client_id, query_pattern, limit)).fetchall()

            results = [{
                'id': row[0],
                'text': row[1],
                'value': row[0]
            } for row in rows]

        elif entity_type == 'peripherique':
            rows = conn.execute('''
                SELECT id, categorie, marque, modele
                FROM peripheriques
                WHERE client_id=? AND (marque LIKE ? OR modele LIKE ?)
                LIMIT ?
            ''', (client_id, query_pattern, query_pattern, limit)).fetchall()

            results = [{
                'id': row[0],
                'text': f"{row[2]} {row[3]} ({row[1]})" if row[3] else f"{row[2]} ({row[1]})",
                'value': row[0]
            } for row in rows]

        conn.close()

    except Exception as e:
        logger.error(f"Erreur autocomplete {entity_type}: {e}")

    return results
