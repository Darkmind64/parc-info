#!/usr/bin/env python3
"""
Test du système de chiffrement des identifiants
"""
import os
from crypto_utils import get_crypto_manager

print("\n" + "="*80)
print("🔐 TEST CHIFFREMENT DES IDENTIFIANTS")
print("="*80 + "\n")

# Initialiser le gestionnaire crypto
secret_key_file = 'secret.key'
if not os.path.exists(secret_key_file):
    print("❌ Fichier secret.key non trouvé. Lance 'python app.py' d'abord.\n")
    exit(1)

crypto = get_crypto_manager(secret_key_file)
print(f"✅ Gestionnaire crypto initialisé")
print(f"📄 Clé secrète: {secret_key_file}\n")

# Tests
test_passwords = [
    "MonPassword123",
    "SuperSecret!@#",
    "admin@localhost",
    "WiFiPassword2024",
    ""  # Vide
]

print("📝 TEST 1: Chiffrement/Déchiffrement\n")
for pwd in test_passwords:
    if not pwd:
        print(f"  Input:      [VIDE]")
        encrypted = crypto.encrypt(pwd)
        print(f"  Chiffré:    {encrypted}")
        print()
        continue

    print(f"  Input:      {pwd}")
    encrypted = crypto.encrypt(pwd)
    print(f"  Chiffré:    {encrypted[:30]}..." if len(encrypted) > 30 else f"  Chiffré:    {encrypted}")

    decrypted = crypto.decrypt(encrypted)
    print(f"  Déchiffré:  {decrypted}")

    if decrypted == pwd:
        print(f"  ✅ OK - Identique!\n")
    else:
        print(f"  ❌ ERREUR - Différent!\n")

print("📝 TEST 2: Détection de texte chiffré\n")
test_strings = [
    ("gAAAAABl7x4K9xK2m4r8...", True),
    ("MonPassword123", False),
    ("", False),
]

for text, expected in test_strings:
    is_encrypted = crypto.is_encrypted(text)
    status = "✅" if is_encrypted == expected else "❌"
    print(f"  {status} '{text[:20]}...' → Chiffré={is_encrypted} (attendu: {expected})")

print("\n" + "="*80)
print("✅ TESTS TERMINÉS")
print("="*80 + "\n")
