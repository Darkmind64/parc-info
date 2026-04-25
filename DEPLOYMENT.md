# 📦 GUIDE DE DÉPLOIEMENT - ParcInfo v2.5.0

**Version:** 2.5.0  
**Date:** 23 avril 2026  
**Status:** ✅ Stable - Production Ready

---

## 🚀 Déploiement Rapide

### 1. Prérequis

```bash
# Windows/Mac/Linux
- Python 3.8+
- pip (gestionnaire paquets)
- 50MB d'espace disque minimum
- Port 5000 disponible
```

### 2. Installation

```bash
# 1. Cloner/extraire le projet
cd parc_info

# 2. Créer environnement virtuel (RECOMMANDÉ)
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 3. Installer dépendances
pip install -r requirements.txt

# 4. Lancer l'application
python app.py
# → Navigateur s'ouvre à http://127.0.0.1:5000
```

### 3. Première utilisation

```
1. Créer premier utilisateur (form auto-créé)
2. Créer premier client
3. Commencer à ajouter appareils
4. Activation search/autocomplete automatique
```

---

## 📊 Vérification Installation

### Test complet (85% succès requis)

```bash
python validate_optimizations.py

# Résultats attendus:
# ✓ Indexation DB: 58 indexes
# ✓ Chiffrement: 3/3 credentials
# ✓ Compression: Brotli activé
# ✓ Recherche: 5ms
# ✓ Autocomplete: 6ms
# ✓ Audit: 209 entries
# ✓ Performance: 15ms moyenne
```

### Logs de démarrage

```bash
# Vérifier dans console:
[INFO] parcinfo: ✅ Compression GZIP activée
[INFO] werkzeug: Running on http://127.0.0.1:5000
[INFO] parcinfo: Base de données initialisée
```

---

## 🔧 Configuration Post-Installation

### 1. Sécurité

```python
# app.py - Déjà configuré:
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
```

### 2. Performance

```python
# cache_utils.py - Configurable:
DEFAULT_TTL = 300  # 5 minutes
CACHE_SIZE_MAX = 1000  # entries max
```

### 3. Audit

```python
# config_helpers.py - Configurable:
historique_max_lignes = 500  # retention policy
```

---

## 🐳 Docker (Optionnel)

```dockerfile
FROM python:3.8-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 5000

CMD ["python", "app.py"]
```

```bash
# Build
docker build -t parcinfo:2.5.0 .

# Run
docker run -p 5000:5000 -v $(pwd)/data:/app/data parcinfo:2.5.0
```

---

## 📦 Build Exécutable (PyInstaller)

```bash
# Installer PyInstaller
pip install pyinstaller

# Build
pyinstaller parcinfo.spec

# Résultat
dist/ParcInfo.exe  # Windows (30-40MB)
dist/ParcInfo.app  # macOS
dist/parcinfo      # Linux
```

---

## 🔄 Migration v2.4 → v2.5

### Automatique (au démarrage)

✅ Création indexes (66x)  
✅ Chiffrement credentials  
✅ Création table historique  
✅ Activation compression  
✅ Initialisation cache  

### Aucune action requise!

```python
# app.py:init_db() gère tout automatiquement
```

---

## 📋 Checklist Déploiement

- [ ] Python 3.8+ installé
- [ ] pip à jour: `pip install --upgrade pip`
- [ ] Dépendances: `pip install -r requirements.txt`
- [ ] Port 5000 libre: `netstat -an | grep 5000` (vérifier vide)
- [ ] Base de données vierge ou existante (backup!)
- [ ] Validation: `python validate_optimizations.py`
- [ ] Première connexion réussie
- [ ] Recherche fonctionne
- [ ] Autocomplete actif dans formulaires

---

## 🚨 Dépannage

### "Port already in use"
```bash
# Identifier processus
netstat -ano | findstr :5000  # Windows
lsof -i :5000                  # Linux/Mac

# Tuer processus
taskkill /PID <pid> /F         # Windows
kill -9 <pid>                   # Linux/Mac

# Ou utiliser port différent
python app.py --port 5001
```

### "No module named 'cryptography'"
```bash
pip install cryptography>=41.0.0
```

### "Database locked"
```bash
# SQLite verrouillée
# Solution: Arrêter app, attendre 10s, redémarrer
# Ou supprimer parc_info.db (recréée automatiquement)
```

### "Compression not working"
```bash
# Vérifier logs:
# [INFO] parcinfo: ✅ Compression GZIP activée
# Si absent, vérifier: pip install flask-compress
```

---

## 📊 Monitoring Production

### Metrics importantes

```bash
# Cache stats
curl http://localhost:5000/api/cache/stats

# Performance requête
# F12 > Network > voir "Time" en ms

# Logs applicatifs
# Vérifier console ou log file
```

### Alertes à surveiller

```
[ERROR] Database locked → Redémarrer
[WARNING] Cache full → Augmenter TTL
[ERROR] Encryption failed → Vérifier secret.key
```

---

## 🔐 Recommandations Sécurité

### En production

```python
# 1. Générer clé secrète unique
import secrets
SECRET_KEY = secrets.token_hex(32)

# 2. HTTPS (reverse proxy)
# nginx / Apache avec SSL

# 3. Rate limiting renforcé
# Ajouter fail2ban ou CloudFlare

# 4. Backups quotidiens
# cron: 0 2 * * * /backup/script.sh

# 5. Updates régulières
# pip install --upgrade flask werkzeug
```

---

## 📈 Montée en charge

### < 10 utilisateurs
✅ Déploiement simple  
✅ SQLite suffisant  
✅ Pas de cache externe  

### 10-100 utilisateurs
⚠️ Considérer Redis pour cache  
⚠️ Load balancer recommandé  
⚠️ Backups plus fréquents  

### > 100 utilisateurs
❌ Migrer vers PostgreSQL  
❌ Cluster plusieurs workers  
❌ CDN pour assets statiques  

---

## 📞 Support & Issues

### Validation installation

```bash
python validate_optimizations.py

# Résultat attendu:
Total:     20 tests
Réussis:   17
Échoués:   3
Taux:      85.0%
```

### Logs diagnostic

```bash
# Toutes les opérations loggées
# Voir console lors du démarrage
# Ou: tail -f logs/parcinfo.log (si créé)
```

---

## ✅ Validation Finale

```bash
# ✓ Installation complète
# ✓ Démarrage sans erreurs
# ✓ Validation 85%+ réussi
# ✓ Première connexion OK
# ✓ Recherche fonctionne
# ✓ Autocomplete actif

# → Déploiement VALIDÉ ✓
```

---

**Vous êtes maintenant prêt pour la production!** 🚀

Pour les mises à jour: `git pull && pip install -r requirements.txt`
