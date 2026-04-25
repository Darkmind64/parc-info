#!/usr/bin/env python3
"""
Script de vérification des index de performance
"""
import sqlite3
import os

DB_PATH = 'parc_info.db'

if not os.path.exists(DB_PATH):
    print("❌ Base de données non trouvée. Lance 'python app.py' d'abord.")
    exit(1)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Récupérer tous les index
indexes = c.execute("""
    SELECT name, tbl_name, sql FROM sqlite_master
    WHERE type='index' AND name NOT LIKE 'sqlite_%'
    ORDER BY tbl_name, name
""").fetchall()

print("\n" + "="*80)
print("📊 VÉRIFICATION DES INDEX DE PERFORMANCE")
print("="*80 + "\n")

if not indexes:
    print("❌ Aucun index trouvé!\n")
    exit(1)

# Grouper par table
tables = {}
for name, tbl, sql in indexes:
    if tbl not in tables:
        tables[tbl] = []
    tables[tbl].append((name, sql))

# Afficher par table
for tbl in sorted(tables.keys()):
    idx_list = tables[tbl]
    print(f"📋 TABLE: {tbl.upper()} ({len(idx_list)} index)")
    print("   " + "-" * 76)
    for name, sql in sorted(idx_list):
        if sql:
            # Extraire la partie importante du SQL
            col_part = sql.split('ON ')[1] if 'ON ' in sql else ''
            print(f"   ✅ {name:45s} → {col_part}")
        else:
            print(f"   ✅ {name}")
    print()

# Statistiques
print("="*80)
print(f"✅ TOTAL: {len(indexes)} index créés")
print("="*80 + "\n")

# Analyser la taille de la DB
file_size = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"💾 Taille DB: {file_size:.2f} MB")
print(f"📈 Impact estimé: +{min(file_size * 0.3, 50):.1f}% (index = ~30% de la taille DB)")

conn.close()
