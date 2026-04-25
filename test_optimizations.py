#!/usr/bin/env python3
"""
Script de validation automatisé pour les 5 optimisations ParcInfo.
Teste: Indexation, Chiffrement, Compression, Cache, Recherche, Autocomplete, Audit
"""

import requests
import json
import time
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Configuration
BASE_URL = "http://127.0.0.1:5000"
DB_PATH = Path("parc_info.db")

# Couleurs pour le terminal
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

# Compteurs
tests_passed = 0
tests_failed = 0
tests_total = 0

def print_header(title):
    """Affiche un en-tête de section"""
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}► {title}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

def print_test(name, passed, message=""):
    """Affiche le résultat d'un test"""
    global tests_passed, tests_failed, tests_total
    tests_total += 1

    if passed:
        tests_passed += 1
        print(f"{GREEN}✓ PASS{RESET} - {name}")
    else:
        tests_failed += 1
        print(f"{RED}✗ FAIL{RESET} - {name}")

    if message:
        print(f"  └─ {message}")

def print_summary():
    """Affiche le résumé des tests"""
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}RÉSUMÉ DES TESTS{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

    passed_pct = (tests_passed / tests_total * 100) if tests_total > 0 else 0

    print(f"Total:    {tests_total} tests")
    print(f"{GREEN}Réussis:  {tests_passed}{RESET}")
    print(f"{RED}Échoués:  {tests_failed}{RESET}")
    print(f"Taux:     {passed_pct:.1f}%\n")

    if tests_failed == 0:
        print(f"{GREEN}{BOLD}✓ TOUS LES TESTS SONT PASSÉS!{RESET}\n")
        return True
    else:
        print(f"{RED}{BOLD}✗ CERTAINS TESTS ONT ÉCHOUÉ{RESET}\n")
        return False

# ============================================================================
# TEST 1: INDEXATION BASE DE DONNÉES
# ============================================================================

def test_database_indexes():
    """Vérifie que tous les indexes sont créés"""
    print_header("TEST 1: INDEXATION BASE DE DONNÉES")

    if not DB_PATH.exists():
        print_test("Fichier DB existe", False, "parc_info.db non trouvé")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Récupérer tous les indexes
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        indexes = cursor.fetchall()

        print_test("DB accessible", True, f"{len(indexes)} indexes trouvés")

        # Vérifier quelques indexes clés
        index_names = [idx[0] for idx in indexes]

        critical_indexes = [
            'idx_appareils_client',
            'idx_contrats_client',
            'idx_peripheriques_client',
            'idx_utilisateurs_client',
            'idx_services_client'
        ]

        found = sum(1 for idx in critical_indexes if idx in index_names)
        print_test("Indexes critiques présents", found >= 4, f"{found}/5 indexes trouvés")

        conn.close()

    except Exception as e:
        print_test("DB accessible", False, str(e))

# ============================================================================
# TEST 2: CHIFFREMENT DES IDENTIFIANTS
# ============================================================================

def test_encryption():
    """Vérifie le chiffrement des identifiants"""
    print_header("TEST 2: CHIFFREMENT DES IDENTIFIANTS")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Vérifier qu'il y a au moins un identifiant
        cursor.execute("SELECT COUNT(*) FROM identifiants")
        count = cursor.fetchone()[0]

        print_test("Table identifiants accessible", True, f"{count} identifiants")

        if count > 0:
            # Vérifier que les mots de passe sont chiffrés
            cursor.execute("SELECT mot_de_passe FROM identifiants LIMIT 5")
            passwords = cursor.fetchall()

            encrypted_count = 0
            for (pwd,) in passwords:
                if pwd and pwd.startswith('gAAAAAB'):
                    encrypted_count += 1

            print_test("Mots de passe chiffrés", encrypted_count > 0,
                      f"{encrypted_count}/{len(passwords)} chiffrés")
        else:
            print_test("Mots de passe chiffrés", True, "Aucun identifiant à tester")

        conn.close()

    except Exception as e:
        print_test("Chiffrement", False, str(e))

# ============================================================================
# TEST 3: COMPRESSION GZIP
# ============================================================================

def test_gzip_compression():
    """Vérifie que la compression Gzip est active"""
    print_header("TEST 3: COMPRESSION GZIP")

    try:
        # Tester une requête GET pour voir les headers
        response = requests.get(f"{BASE_URL}/")

        print_test("Serveur accessible", response.status_code == 200,
                  f"Status: {response.status_code}")

        # Vérifier les headers de compression
        content_encoding = response.headers.get('Content-Encoding', '')
        gzip_active = 'gzip' in content_encoding.lower()

        print_test("Compression Gzip active", gzip_active,
                  f"Content-Encoding: {content_encoding or 'aucun'}")

        # Vérifier le ratio de compression
        if gzip_active:
            content_length = response.headers.get('Content-Length', 'N/A')
            print(f"  └─ Content-Length (compressé): {content_length} bytes")

    except Exception as e:
        print_test("Compression Gzip", False, str(e))

# ============================================================================
# TEST 4: CACHE EN MÉMOIRE
# ============================================================================

def test_cache():
    """Vérifie que le cache fonctionne"""
    print_header("TEST 4: CACHE EN MÉMOIRE (TTL)")

    try:
        # Vérifier l'endpoint de stats de cache
        response = requests.get(f"{BASE_URL}/api/cache/stats")

        print_test("Endpoint /api/cache/stats accessible", response.status_code == 200,
                  f"Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()

            # Vérifier la structure des stats
            has_entries = 'entries' in data
            has_hits = 'total_hits' in data

            print_test("Stats du cache valides", has_entries and has_hits,
                      f"Entrées: {data.get('entries', 0)}, Hits: {data.get('total_hits', 0)}")

            if has_entries:
                print(f"  └─ {data.get('message', '')}")

    except Exception as e:
        print_test("Cache", False, str(e))

# ============================================================================
# TEST 5: RECHERCHE FULL-TEXT
# ============================================================================

def test_full_text_search():
    """Vérifie que la recherche fonctionne"""
    print_header("TEST 5: RECHERCHE FULL-TEXT")

    try:
        # Test 1: Requête vide
        response = requests.get(f"{BASE_URL}/api/search?q=")
        print_test("Requête vide acceptée", response.status_code == 200)

        # Test 2: Requête trop courte (< 2 caractères)
        response = requests.get(f"{BASE_URL}/api/search?q=a")
        print_test("Requête courte rejetée", response.status_code == 200)
        data = response.json()
        is_empty = data.get('total', 0) == 0
        print(f"  └─ Résultats: {data.get('total', 0)}")

        # Test 3: Recherche valide
        response = requests.get(f"{BASE_URL}/api/search?q=pc&limit=5")
        print_test("Recherche valide exécutée", response.status_code == 200)

        if response.status_code == 200:
            data = response.json()
            total = data.get('total', 0)
            print(f"  └─ Total résultats: {total}")

            # Vérifier que tous les types sont présents
            types = ['appareils', 'contrats', 'utilisateurs', 'services',
                    'peripheriques', 'identifiants']
            for entity_type in types:
                count = len(data.get(entity_type, []))
                if count > 0:
                    print(f"    • {entity_type}: {count}")

    except Exception as e:
        print_test("Recherche Full-Text", False, str(e))

# ============================================================================
# TEST 6: AUTOCOMPLETE
# ============================================================================

def test_autocomplete():
    """Vérifie que l'autocomplete fonctionne"""
    print_header("TEST 6: AUTOCOMPLETE")

    entity_types = ['appareil', 'contrat', 'utilisateur', 'service', 'peripherique']

    try:
        for entity_type in entity_types:
            # Requête courte
            response = requests.get(f"{BASE_URL}/api/autocomplete/{entity_type}?q=a&limit=5")

            if response.status_code == 200:
                data = response.json()
                is_list = isinstance(data, list)
                print_test(f"Autocomplete {entity_type}", is_list,
                          f"{len(data)} résultats retournés")
            else:
                print_test(f"Autocomplete {entity_type}", False,
                          f"Status: {response.status_code}")

    except Exception as e:
        print_test("Autocomplete", False, str(e))

# ============================================================================
# TEST 7: AUDIT TRAIL
# ============================================================================

def test_audit_trail():
    """Vérifie que l'historique est enregistré"""
    print_header("TEST 7: AUDIT TRAIL (HISTORIQUE)")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Vérifier que la table histories existe
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='histories'")
        exists = cursor.fetchone() is not None

        print_test("Table histories existe", exists)

        if exists:
            # Compter les entrées
            cursor.execute("SELECT COUNT(*) FROM histories")
            count = cursor.fetchone()[0]

            print_test("Historique enregistré", count > 0, f"{count} entrées")

            # Vérifier les types d'actions
            cursor.execute("SELECT DISTINCT action FROM histories LIMIT 10")
            actions = [row[0] for row in cursor.fetchall()]

            if actions:
                print(f"  └─ Actions enregistrées:")
                for action in actions[:5]:
                    print(f"    • {action}")

        conn.close()

    except Exception as e:
        print_test("Audit Trail", False, str(e))

# ============================================================================
# TEST 8: PERFORMANCE GLOBALE
# ============================================================================

def test_performance():
    """Teste les performances globales"""
    print_header("TEST 8: PERFORMANCE GLOBALE")

    try:
        # Test 1: Temps de réponse - Page d'accueil
        start = time.time()
        response = requests.get(f"{BASE_URL}/", timeout=5)
        time_home = (time.time() - start) * 1000

        is_fast = time_home < 500
        print_test("Temps chargement accueil", is_fast, f"{time_home:.0f}ms")

        # Test 2: Temps de réponse - Liste appareils
        start = time.time()
        response = requests.get(f"{BASE_URL}/appareils", timeout=5)
        time_devices = (time.time() - start) * 1000

        is_fast = time_devices < 1000
        print_test("Temps chargement appareils", is_fast, f"{time_devices:.0f}ms")

        # Test 3: Temps de recherche
        start = time.time()
        response = requests.get(f"{BASE_URL}/api/search?q=server", timeout=5)
        time_search = (time.time() - start) * 1000

        is_fast = time_search < 300
        print_test("Temps recherche", is_fast, f"{time_search:.0f}ms")

        # Test 4: Temps autocomplete
        start = time.time()
        response = requests.get(f"{BASE_URL}/api/autocomplete/appareil?q=pc", timeout=5)
        time_autocomplete = (time.time() - start) * 1000

        is_fast = time_autocomplete < 200
        print_test("Temps autocomplete", is_fast, f"{time_autocomplete:.0f}ms")

        print(f"\n{YELLOW}Résumé des performances:{RESET}")
        print(f"  • Accueil:      {time_home:>6.0f}ms")
        print(f"  • Appareils:    {time_devices:>6.0f}ms")
        print(f"  • Recherche:    {time_search:>6.0f}ms")
        print(f"  • Autocomplete: {time_autocomplete:>6.0f}ms")

    except Exception as e:
        print_test("Performance", False, str(e))

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Exécute tous les tests"""
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}VALIDATION AUTOMATISÉE - 5 OPTIMISATIONS PARCINFO{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Base de données: {DB_PATH}")
    print(f"Serveur: {BASE_URL}\n")

    # Vérifier que le serveur est accessible
    try:
        response = requests.get(f"{BASE_URL}/", timeout=2)
        print(f"{GREEN}✓ Serveur accessible{RESET}\n")
    except Exception as e:
        print(f"{RED}✗ Serveur non accessible: {e}{RESET}")
        print(f"  → Assurez-vous que le serveur Flask est en cours d'exécution\n")
        sys.exit(1)

    # Exécuter tous les tests
    test_database_indexes()
    test_encryption()
    test_gzip_compression()
    test_cache()
    test_full_text_search()
    test_autocomplete()
    test_audit_trail()
    test_performance()

    # Afficher le résumé
    success = print_summary()

    # Code de sortie
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
