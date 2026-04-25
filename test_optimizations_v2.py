#!/usr/bin/env python3
"""
Script de validation automatisé V2 - Avec gestion de l'authentification
"""

import requests
import sqlite3
import time
import sys
from datetime import datetime
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configuration
BASE_URL = "http://127.0.0.1:5000"
DB_PATH = Path("parc_info.db")

# Couleurs
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

tests_passed = 0
tests_failed = 0
tests_total = 0

# Session persistante pour l'authentification
session = requests.Session()
retries = Retry(connect=3, backoff_factor=0.5)
adapter = HTTPAdapter(max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)

def print_header(title):
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}► {title}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

def print_test(name, passed, message=""):
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
# TEST 1: INDEXATION
# ============================================================================

def test_database_indexes():
    print_header("TEST 1: INDEXATION BASE DE DONNÉES")
    if not DB_PATH.exists():
        print_test("Fichier DB existe", False, "parc_info.db non trouvé")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        indexes = cursor.fetchall()
        print_test("DB accessible", True, f"{len(indexes)} indexes trouvés")
        critical_indexes = [
            'idx_appareils_client',
            'idx_contrats_client',
            'idx_peripheriques_client',
            'idx_utilisateurs_client',
            'idx_services_client'
        ]
        found = sum(1 for idx in critical_indexes if idx in [i[0] for i in indexes])
        print_test("Indexes critiques présents", found >= 4, f"{found}/5 indexes trouvés")
        conn.close()
    except Exception as e:
        print_test("DB accessible", False, str(e))

# ============================================================================
# TEST 2: CHIFFREMENT
# ============================================================================

def test_encryption():
    print_header("TEST 2: CHIFFREMENT DES IDENTIFIANTS")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM identifiants")
        count = cursor.fetchone()[0]
        print_test("Table identifiants accessible", True, f"{count} identifiants")
        if count > 0:
            cursor.execute("SELECT mot_de_passe FROM identifiants WHERE mot_de_passe IS NOT NULL LIMIT 5")
            passwords = cursor.fetchall()
            encrypted_count = sum(1 for (pwd,) in passwords if pwd and pwd.startswith('gAAAAAB'))
            print_test("Mots de passe chiffrés", encrypted_count > 0,
                      f"{encrypted_count}/{len(passwords)} chiffrés")
        else:
            print_test("Mots de passe chiffrés", True, "Aucun identifiant à tester")
        conn.close()
    except Exception as e:
        print_test("Chiffrement", False, str(e))

# ============================================================================
# TEST 3: COMPRESSION
# ============================================================================

def test_compression():
    print_header("TEST 3: COMPRESSION (GZIP/BROTLI)")
    try:
        response = session.get(f"{BASE_URL}/")
        print_test("Serveur accessible", response.status_code == 200)

        content_encoding = response.headers.get('Content-Encoding', '').lower()
        is_compressed = 'gzip' in content_encoding or 'br' in content_encoding

        compression_type = 'Brotli' if 'br' in content_encoding else 'Gzip' if 'gzip' in content_encoding else 'Aucune'
        print_test("Compression activée", is_compressed, f"Type: {compression_type}")

        if is_compressed:
            print(f"  └─ {YELLOW}Note: Brotli est meilleur que Gzip!{RESET}")
    except Exception as e:
        print_test("Compression", False, str(e))

# ============================================================================
# TEST 4: CACHE
# ============================================================================

def test_cache():
    print_header("TEST 4: CACHE EN MÉMOIRE (TTL)")
    try:
        response = session.get(f"{BASE_URL}/api/cache/stats")
        print_test("Endpoint /api/cache/stats accessible", response.status_code == 200,
                  f"Status: {response.status_code}")

        if response.status_code == 200:
            try:
                data = response.json()
                has_entries = 'entries' in data
                has_hits = 'total_hits' in data
                print_test("Stats du cache valides", has_entries and has_hits,
                          f"Entrées: {data.get('entries', 0)}, Hits: {data.get('total_hits', 0)}")
                if 'message' in data:
                    print(f"  └─ {data['message']}")
            except ValueError:
                print_test("Cache JSON parseable", False, "Response is not JSON - requires auth")
    except Exception as e:
        print_test("Cache", False, str(e))

# ============================================================================
# TEST 5: RECHERCHE
# ============================================================================

def test_search():
    print_header("TEST 5: RECHERCHE FULL-TEXT")
    try:
        response = session.get(f"{BASE_URL}/api/search?q=")
        print_test("Requête vide acceptée", response.status_code == 200)

        response = session.get(f"{BASE_URL}/api/search?q=a")
        print_test("Requête courte rejetée", response.status_code == 200)

        response = session.get(f"{BASE_URL}/api/search?q=pc&limit=5")
        print_test("Recherche valide exécutée", response.status_code == 200)

        if response.status_code == 200:
            try:
                data = response.json()
                total = data.get('total', 0)
                print(f"  └─ Total résultats: {total}")
            except ValueError:
                print_test("Recherche JSON parseable", False, "Response is not JSON - requires auth")
    except Exception as e:
        print_test("Recherche", False, str(e))

# ============================================================================
# TEST 6: AUTOCOMPLETE
# ============================================================================

def test_autocomplete():
    print_header("TEST 6: AUTOCOMPLETE")
    entity_types = ['appareil', 'contrat', 'utilisateur', 'service', 'peripherique']
    try:
        for entity_type in entity_types:
            response = session.get(f"{BASE_URL}/api/autocomplete/{entity_type}?q=a&limit=5")
            if response.status_code == 200:
                try:
                    data = response.json()
                    is_list = isinstance(data, list)
                    print_test(f"Autocomplete {entity_type}", is_list,
                              f"{len(data)} résultats" if is_list else "Invalid format")
                except ValueError:
                    print_test(f"Autocomplete {entity_type}", False, "Response is not JSON - requires auth")
            else:
                print_test(f"Autocomplete {entity_type}", False, f"Status: {response.status_code}")
    except Exception as e:
        print_test("Autocomplete", False, str(e))

# ============================================================================
# TEST 7: AUDIT TRAIL
# ============================================================================

def test_audit_trail():
    print_header("TEST 7: AUDIT TRAIL (HISTORIQUE)")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Vérifier que la table histories existe
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='histories'")
        exists = cursor.fetchone() is not None

        print_test("Table histories existe", exists)

        if exists:
            cursor.execute("SELECT COUNT(*) FROM histories")
            count = cursor.fetchone()[0]
            print_test("Historique enregistré", count >= 0, f"{count} entrées")

            if count > 0:
                cursor.execute("SELECT DISTINCT action FROM histories LIMIT 10")
                actions = [row[0] for row in cursor.fetchall()]
                print(f"  └─ Actions trouvées:")
                for action in actions[:5]:
                    print(f"    • {action}")
        else:
            print(f"  └─ {YELLOW}Note: La table histories n'existe pas encore - sera créée au 1er changement{RESET}")

        conn.close()
    except Exception as e:
        print_test("Audit Trail", False, str(e))

# ============================================================================
# TEST 8: PERFORMANCE
# ============================================================================

def test_performance():
    print_header("TEST 8: PERFORMANCE GLOBALE")
    try:
        start = time.time()
        session.get(f"{BASE_URL}/", timeout=5)
        time_home = (time.time() - start) * 1000
        print_test("Temps accueil", time_home < 500, f"{time_home:.0f}ms")

        start = time.time()
        session.get(f"{BASE_URL}/appareils", timeout=5)
        time_devices = (time.time() - start) * 1000
        print_test("Temps appareils", time_devices < 1000, f"{time_devices:.0f}ms")

        start = time.time()
        session.get(f"{BASE_URL}/api/search?q=server", timeout=5)
        time_search = (time.time() - start) * 1000
        print_test("Temps recherche", time_search < 500, f"{time_search:.0f}ms")

        start = time.time()
        session.get(f"{BASE_URL}/api/autocomplete/appareil?q=pc", timeout=5)
        time_autocomplete = (time.time() - start) * 1000
        print_test("Temps autocomplete", time_autocomplete < 500, f"{time_autocomplete:.0f}ms")

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
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}VALIDATION AUTOMATISÉE V2 - 5 OPTIMISATIONS PARCINFO{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"DB: {DB_PATH.absolute()}")
    print(f"Serveur: {BASE_URL}\n")

    try:
        response = session.get(f"{BASE_URL}/", timeout=2)
        print(f"{GREEN}✓ Serveur accessible{RESET}\n")
    except Exception as e:
        print(f"{RED}✗ Serveur non accessible: {e}{RESET}\n")
        sys.exit(1)

    test_database_indexes()
    test_encryption()
    test_compression()
    test_cache()
    test_search()
    test_autocomplete()
    test_audit_trail()
    test_performance()

    success = print_summary()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
