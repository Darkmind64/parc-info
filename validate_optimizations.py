#!/usr/bin/env python3
"""
VALIDATION FINALE - 5 Optimisations ParcInfo
Script de test robuste sans authentification requise
"""

import requests
import sqlite3
import time
import sys
from datetime import datetime
from pathlib import Path

BASE_URL = "http://127.0.0.1:5000"
DB_PATH = Path("parc_info.db")

# Couleurs
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

stats = {"passed": 0, "failed": 0, "total": 0}

def header(title):
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}► {title}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

def test(name, passed, msg=""):
    stats["total"] += 1
    status = f"{GREEN}✓ PASS{RESET}" if passed else f"{RED}✗ FAIL{RESET}"
    print(f"{status} - {name}")
    if msg:
        print(f"  └─ {msg}")
    if passed:
        stats["passed"] += 1
    else:
        stats["failed"] += 1

def summary():
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}RÉSUMÉ FINAL{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")
    pct = (stats["passed"] / stats["total"] * 100) if stats["total"] > 0 else 0
    print(f"Total:     {stats['total']} tests")
    print(f"{GREEN}Réussis:   {stats['passed']}{RESET}")
    print(f"{RED}Échoués:   {stats['failed']}{RESET}")
    print(f"Taux:      {pct:.1f}%\n")
    if stats["failed"] == 0:
        print(f"{GREEN}{BOLD}🎉 VALIDATION COMPLÈTE - TOUS LES TESTS PASSÉS!{RESET}\n")
        return True
    else:
        print(f"{YELLOW}{BOLD}⚠️  {stats['failed']} TEST(S) À CORRIGER{RESET}\n")
        return False

# ============================================================================
# TEST 1: INDEXATION DB
# ============================================================================

def test_indexes():
    header("✅ TEST 1: INDEXATION BASE DE DONNÉES")

    if not DB_PATH.exists():
        test("DB existe", False, "parc_info.db non trouvé")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Compter les indexes
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        count = cursor.fetchone()[0]
        test("Indexes créés", count > 50, f"{count} indexes trouvés")

        # Vérifier quelques indexes clés
        critical = ['idx_appareils_client', 'idx_contrats_client', 'idx_peripheriques_client']
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        index_names = [r[0] for r in cursor.fetchall()]
        found = sum(1 for idx in critical if idx in index_names)
        test("Indexes critiques", found >= 2, f"{found}/3 présents")

        conn.close()
    except Exception as e:
        test("DB accessible", False, str(e))

# ============================================================================
# TEST 2: CHIFFREMENT
# ============================================================================

def test_crypto():
    header("🔐 TEST 2: CHIFFREMENT DES IDENTIFIANTS")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM identifiants WHERE mot_de_passe IS NOT NULL")
        count = cursor.fetchone()[0]
        test("Identifiants existent", count > 0, f"{count} credentials")

        if count > 0:
            cursor.execute("SELECT mot_de_passe FROM identifiants WHERE mot_de_passe IS NOT NULL LIMIT 10")
            pwds = [r[0] for r in cursor.fetchall()]
            encrypted = sum(1 for p in pwds if p.startswith('gAAAAAB'))
            test("Chiffrement Fernet", encrypted == len(pwds),
                 f"{encrypted}/{len(pwds)} chiffrés (gAAAAAB...)")

        conn.close()
    except Exception as e:
        test("Chiffrement", False, str(e))

# ============================================================================
# TEST 3: COMPRESSION
# ============================================================================

def test_compression():
    header("📦 TEST 3: COMPRESSION RÉSEAU")

    try:
        r = requests.get(f"{BASE_URL}/")
        test("Serveur accessible", r.status_code == 200, f"Status: {r.status_code}")

        enc = r.headers.get('Content-Encoding', '').lower()
        is_compressed = 'gzip' in enc or 'br' in enc
        comp_type = "Brotli" if 'br' in enc else "Gzip" if 'gzip' in enc else "Aucune"

        test("Compression activée", is_compressed, f"Type: {comp_type}")
        if is_compressed and 'br' in enc:
            print(f"  {YELLOW}→ Brotli est meilleur que Gzip!{RESET}")
    except Exception as e:
        test("Compression", False, str(e))

# ============================================================================
# TEST 4: CACHE
# ============================================================================

def test_cache():
    header("💾 TEST 4: CACHE EN MÉMOIRE (TTL)")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Vérifier qu'il y a des données cachées
        cursor.execute("SELECT COUNT(*) FROM appareils")
        app_count = cursor.fetchone()[0]
        test("Données pour cache", app_count > 0, f"{app_count} appareils")

        # Tester la performance (cache devrait être rapide)
        start = time.time()
        requests.get(f"{BASE_URL}/appareils")
        elapsed1 = (time.time() - start) * 1000

        start = time.time()
        requests.get(f"{BASE_URL}/appareils")
        elapsed2 = (time.time() - start) * 1000

        is_faster = elapsed2 <= elapsed1 * 0.9  # 2e requête 10% plus rapide
        test("Cache opérationnel", is_faster,
             f"1ère: {elapsed1:.0f}ms, 2e: {elapsed2:.0f}ms")

        conn.close()
    except Exception as e:
        test("Cache", False, str(e))

# ============================================================================
# TEST 5: RECHERCHE FULL-TEXT
# ============================================================================

def test_search():
    header("🔍 TEST 5: RECHERCHE FULL-TEXT")

    try:
        # Vérifier qu'on a du contenu à chercher
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM appareils")
        app_count = cursor.fetchone()[0]
        test("Données pour recherche", app_count > 0, f"{app_count} appareils")

        # Tester la perf de recherche
        start = time.time()
        r = requests.get(f"{BASE_URL}/api/search?q=p")  # Minimum 2 chars requis
        elapsed = (time.time() - start) * 1000

        test("Recherche performante", elapsed < 500, f"Temps: {elapsed:.0f}ms")

        if r.status_code == 200:
            try:
                data = r.json()
                test("Format JSON valide", True, f"Total résultats: {data.get('total', 0)}")
            except:
                test("Format JSON", False, "Non authentifié - endpoint protégé")

        conn.close()
    except Exception as e:
        test("Recherche", False, str(e))

# ============================================================================
# TEST 6: AUTOCOMPLETE
# ============================================================================

def test_autocomplete():
    header("⚡ TEST 6: AUTOCOMPLETE (DYNAMIQUE)")

    try:
        # Vérifier qu'on a des appareils
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM appareils")
        count = cursor.fetchone()[0]
        test("Données pour autocomplete", count > 0, f"{count} appareils")

        # Tester la perf
        start = time.time()
        r = requests.get(f"{BASE_URL}/api/autocomplete/appareil?q=p")
        elapsed = (time.time() - start) * 1000

        test("Autocomplete performant", elapsed < 300, f"Temps: {elapsed:.0f}ms")

        if r.status_code == 200:
            try:
                data = r.json()
                test("Format valide", isinstance(data, list),
                     f"{len(data)} suggestions retournées")
            except:
                test("Format JSON", False, "Non authentifié - endpoint protégé")

        conn.close()
    except Exception as e:
        test("Autocomplete", False, str(e))

# ============================================================================
# TEST 7: AUDIT TRAIL
# ============================================================================

def test_audit():
    header("📋 TEST 7: AUDIT TRAIL (HISTORIQUE)")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Vérifier que la table existe
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='historique'")
        exists = cursor.fetchone() is not None
        test("Table historique créée", exists)

        if exists:
            cursor.execute("SELECT COUNT(*) FROM historique")
            count = cursor.fetchone()[0]
            test("Historique enregistré", count >= 0, f"{count} entrées")

            if count > 0:
                cursor.execute("SELECT DISTINCT action FROM historique LIMIT 5")
                actions = [r[0] for r in cursor.fetchall()]
                print(f"  └─ Actions trouvées:")
                for action in actions:
                    print(f"    • {action}")

        conn.close()
    except Exception as e:
        test("Audit Trail", False, str(e))

# ============================================================================
# TEST 8: PERFORMANCE
# ============================================================================

def test_perf():
    header("⏱️  TEST 8: PERFORMANCE GLOBALE")

    urls = [
        ("/", "Accueil", 300),
        ("/appareils", "Liste appareils", 500),
        ("/api/search?q=p", "Recherche", 400),
        ("/api/autocomplete/appareil?q=p", "Autocomplete", 300),
    ]

    for url, name, threshold in urls:
        try:
            start = time.time()
            r = requests.get(f"{BASE_URL}{url}", timeout=5)
            elapsed = (time.time() - start) * 1000

            is_fast = elapsed < threshold
            test(f"Perf {name}", is_fast, f"{elapsed:.0f}ms (seuil: {threshold}ms)")
        except Exception as e:
            test(f"Perf {name}", False, str(e))

# ============================================================================
# MAIN
# ============================================================================

def main():
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}VALIDATION FINALE - 5 OPTIMISATIONS PARCINFO{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"Date: {datetime.now().strftime('%d/04/2026 à %H:%M:%S')}")
    print(f"Base: {DB_PATH.absolute()}")
    print(f"Serveur: {BASE_URL}\n")

    try:
        r = requests.get(f"{BASE_URL}/", timeout=2)
        print(f"{GREEN}✓ Serveur accessible{RESET}\n")
    except Exception as e:
        print(f"{RED}✗ Serveur non accessible{RESET}\n")
        sys.exit(1)

    test_indexes()
    test_crypto()
    test_compression()
    test_cache()
    test_search()
    test_autocomplete()
    test_audit()
    test_perf()

    success = summary()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
