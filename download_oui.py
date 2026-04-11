"""
download_oui.py — Télécharge la base OUI IEEE pour la détection de fabricants
Usage : python download_oui.py

Télécharge le fichier oui.txt (~5 MB) depuis le registre IEEE officiel.
ParcInfo utilisera automatiquement ce fichier au démarrage.
"""
import urllib.request, os, sys

URL = "https://standards-oui.ieee.org/oui/oui.txt"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oui.txt')

print(f"Téléchargement de la base OUI IEEE...")
print(f"Source  : {URL}")
print(f"Dest    : {DEST}")

try:
    def progress(count, block_size, total_size):
        pct = int(count * block_size * 100 / total_size) if total_size > 0 else 0
        print(f"\r  {min(pct,100):3d}% ({count*block_size//1024} KB)", end='', flush=True)
    
    urllib.request.urlretrieve(URL, DEST, reporthook=progress)
    size = os.path.getsize(DEST)
    print(f"\n✅ Téléchargé : {size//1024} KB")
    
    # Compter les entrées
    count = sum(1 for line in open(DEST, encoding='utf-8', errors='ignore') if '(hex)' in line)
    print(f"   {count} préfixes OUI")
    print("\nRedémarrez ParcInfo pour activer la nouvelle base.")
    
except Exception as e:
    print(f"\n❌ Erreur : {e}")
    print("Vérifiez votre connexion internet et réessayez.")
    sys.exit(1)
