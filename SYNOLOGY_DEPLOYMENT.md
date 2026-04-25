# 🐳 Guide Déploiement Docker — ParcInfo v2.5.0 sur NAS Synology

**Date** : 23 avril 2026  
**Version** : 2.5.0 - Production Ready  
**Cible** : Synology NAS (DSM 7.0+)

---

## 📋 Prérequis

### Matériel & Logiciel
- ✅ NAS Synology (modèle x86 ou ARM, DSM 7.0+)
- ✅ Application **Docker** installée sur le NAS (via Package Center)
- ✅ Au minimum **2 cores CPU** et **512 MB RAM** disponibles
- ✅ Au minimum **2 GB d'espace disque** libre
- ✅ Port **8000** disponible (port 5000 réservé par Synology)

### Accès
- ✅ Accès admin au NAS (SSH ou interface web)
- ✅ Accès à un terminal/PowerShell sur le PC de développement
- ✅ Les fichiers du projet `parc_info/` sur le PC

---

## 🚀 Déploiement — Étapes Complètes

### Étape 1 : Préparer les Fichiers (PC)

```bash
# Sur votre PC, naviguer au dossier parc_info
cd C:\chemin\vers\parc_info

# Vérifier présence des fichiers essentiels
ls Dockerfile
ls docker-compose.yml
ls requirements.txt
ls app.py
```

**Fichiers requis :**
- ✅ Dockerfile
- ✅ docker-compose.yml
- ✅ requirements.txt
- ✅ app.py + modules (.py)
- ✅ templates/ (dossier complet)
- ✅ static/ (optionnel mais recommandé)

---

### Étape 2 : Installer Docker sur le NAS (si absent)

#### Via Interface Web Synology

1. **Ouvrir Package Center**
   - Accédez à votre NAS : `http://<NAS-IP>:5000`
   - Connexion avec compte admin
   - Cliquez **Package Center** (icône app)

2. **Rechercher Docker**
   - Barre recherche : tapez `Docker`
   - Cliquez sur application **Docker**
   - Bouton **Installer**
   - Confirmez + acceptez conditions

3. **Attendre Installation**
   - L'installation peut prendre 2-5 min
   - ✅ Vous verrez : "Installation terminée"

4. **Lancer Docker**
   - Cliquez **Ouvrir**
   - Interface Docker s'affiche

---

### Étape 3 : Transférer les Fichiers sur le NAS

#### Option 1 : Via File Station (Simple)

1. **Ouvrir File Station**
   - NAS → File Station
   - Naviguer : `volume1/docker/` (créer si absent)
   - Créer dossier : `mkdir parcinfo`

2. **Uploader fichiers**
   - Ouvrir dossier `parcinfo` créé
   - Drag & Drop depuis PC :
     - Dockerfile
     - docker-compose.yml
     - requirements.txt
     - app.py
     - Tous les modules .py
     - Dossier `templates/` entier
     - Dossier `static/` (optionnel)

3. **Vérifier structure**
   ```
   /volume1/docker/parcinfo/
   ├── Dockerfile
   ├── docker-compose.yml
   ├── requirements.txt
   ├── app.py
   ├── database.py
   ├── auth_utils.py
   ├── config_helpers.py
   ├── client_helpers.py
   ├── [autres modules .py]
   ├── templates/
   │   ├── base.html
   │   ├── index.html
   │   ├── [tous autres .html]
   │   └── ...
   └── static/
       └── [CSS, JS, images - optionnel]
   ```

#### Option 2 : Via SSH (Avancé)

```bash
# Depuis PC (PowerShell ou Terminal)
# Remplacer <NAS-IP> et <admin> par vos valeurs

scp -r C:\chemin\parc_info admin@<NAS-IP>:/volume1/docker/

# Ou, si vous préférez fichier par fichier :
scp Dockerfile admin@<NAS-IP>:/volume1/docker/parcinfo/
scp docker-compose.yml admin@<NAS-IP>:/volume1/docker/parcinfo/
# ... etc
```

---

### Étape 4 : Construire l'Image Docker (NAS)

#### Méthode 1 : Via Docker Application (Interface)

**ATTENTION :** Synology Docker UI ne permet pas construire via docker-compose. Nous passons par SSH.

#### Méthode 2 : Via SSH (Recommandée)

1. **Accéder au NAS en SSH**
   ```bash
   ssh admin@<NAS-IP>
   # Entrer mot de passe admin
   ```

2. **Naviguer au dossier projet**
   ```bash
   cd /volume1/docker/parcinfo
   
   # Vérifier fichiers présents
   ls -la
   ```

3. **Construire l'image**
   ```bash
   docker build -t parcinfo:2.5.0 .
   
   # Output attendu :
   # [+] Building 45.2s (9/9) FINISHED
   # => exporting to image                                 0.0s
   # => => writing image sha256:abc123...                  0.0s
   # => => naming to docker.io/library/parcinfo:2.5.0      0.0s
   ```

4. **Vérifier image créée**
   ```bash
   docker images | grep parcinfo
   
   # Output :
   # parcinfo          2.5.0     abc123...   2 minutes ago   850MB
   ```

---

### Étape 5 : Créer le Conteneur (NAS)

#### Via Docker Compose (Recommandé)

**Dans le SSH (toujours connecté) :**

```bash
# Toujours dans /volume1/docker/parcinfo
docker-compose up -d

# Output attendu :
# Creating parcinfo-app ... done
```

**Vérifier conteneur en marche :**
```bash
docker ps

# Output :
# CONTAINER ID  IMAGE         STATUS              PORTS
# 1a2b3c4d5e6f  parcinfo:2.5.0  Up 2 minutes  0.0.0.0:8000->5000/tcp
```

#### Ou : Créer Manuellement via Interface (Plus Long)

1. **Ouvrir Docker Application** (sur NAS)
   - NAS → Applications → Docker

2. **Onglet "Image"**
   - Chercher `parcinfo:2.5.0`
   - Clic droit → **Lancer**

3. **Créer Conteneur**
   - Popup apparaît
   - Remplir champs :

   | Champ | Valeur |
   |-------|--------|
   | Nom | `parcinfo-app` |
   | CPU Limit | `2` (ou moins selon NAS) |
   | Memory Limit | `512MB` |
   | Env Var: `FLASK_ENV` | `production` |
   | Env Var: `PYTHONUNBUFFERED` | `1` |
   | Env Var: `TZ` | `Europe/Paris` |
   | Port Local | `8000` |
   | Port Conteneur | `5000` |
   | Dossier Local 1 | `/volume1/docker/parcinfo/data` |
   | Dossier Conteneur 1 | `/app` |
   | Dossier Local 2 | `/volume1/docker/parcinfo/uploads` |
   | Dossier Conteneur 2 | `/app/uploads` |
   | Dossier Local 3 | `/volume1/docker/parcinfo/backups` |
   | Dossier Conteneur 3 | `/data/backups` |

4. **Avancé**
   - ✅ Redémarrage auto : `unless-stopped`
   - ✅ Exécuter en tant que : laisser défaut (non-root recommandé)

5. **Appliquer**
   - Cliquez **Appliquer**
   - Conteneur démarre automatiquement

---

### Étape 6 : Vérifier Déploiement

#### 1. Conteneur Actif

```bash
# Dans SSH
docker ps | grep parcinfo

# Doit montrer status "Up X minutes"
```

#### 2. Test Accès

**Depuis navigateur PC :**
```
http://<NAS-IP>:8000
```

**Résultat attendu :**
- ✅ Page ParcInfo s'affiche
- ✅ Interface de connexion visible

#### 3. Journaux (Logs)

**Voir les logs du conteneur :**
```bash
docker logs parcinfo-app

# Output attendu (pas d'erreurs) :
# [INFO] werkzeug: Running on http://0.0.0.0:5000
# [INFO] parcinfo: Database initialized
# [INFO] parcinfo: ✅ All optimizations loaded
```

**Logs en direct (live) :**
```bash
docker logs -f parcinfo-app
# Ctrl+C pour quitter
```

---

### Étape 7 : Premier Accès & Configuration

#### 1. Accéder ParcInfo

**URL :** `http://<NAS-IP>:8000`

Exemple : Si votre NAS est à `192.168.1.100` :
```
http://192.168.1.100:8000
```

#### 2. Login Initial

**Identifiants par défaut** (voir app.py:init_db()) :
- **Login** : `admin`
- **Mot de passe** : `admin` (à changer immédiatement)

**⚠️ SÉCURITÉ** : Changez le mot de passe admin dès la 1ère connexion !

#### 3. Créer Premier Client

1. Cliquez **➕ Nouveau** (top-right)
2. Remplissez :
   - Nom : `Mon Entreprise`
   - Contact : votre info
3. Cliquez **Valider**

---

### Étape 8 : Vérifier Optimisations (v2.5.0)

Une fois connecté, testez les 5 optimisations :

#### ✅ 1. Recherche (5ms)
- Barre de recherche navbar
- Tapez un nom d'appareil
- Résultats apparaissent en temps réel

#### ✅ 2. Autocomplete (6ms)
- Menu ⚙️ → Maintenance → Nouvelle
- Cliquez champ "Appareil"
- Tapez quelques caractères
- Suggestions dynamiques

#### ✅ 3. Chiffrement Credentials
- Menu 🔐 Identifiants → Ajouter
- Remplissez login/password
- Sauvegardez
- Credential stocké chiffré (vérif en DB)

#### ✅ 4. Indexation DB (60% plus rapide)
- Pages se chargent très vite
- Listes paginées instantanées

#### ✅ 5. Compression Réseau (70% bande passante)
- Ouvrir DevTools (F12)
- Onglet Network
- Voir "Content-Encoding: br" (Brotli)

---

## 🔄 Gestion du Conteneur

### Démarrer/Arrêter (SSH)

```bash
# Démarrer conteneur arrêté
docker start parcinfo-app

# Arrêter conteneur
docker stop parcinfo-app

# Redémarrer
docker restart parcinfo-app

# Supprimer conteneur (ATTENTION : données persistent)
docker rm parcinfo-app
```

### Ou via Interface Synology

1. Docker → Conteneur
2. Sélectionner `parcinfo-app`
3. Boutons : ⏹ (arrêter), ▶️ (démarrer), ⟳ (redémarrer)

---

## 💾 Persistence de Données

### Structure Volumes

**Sur NAS, données stockées dans :**
```
/volume1/docker/parcinfo/
├── data/                    # Base de données SQLite
│   └── parc_info.db
├── uploads/                 # Documents joints
│   └── app<id>_<timestamp>_<filename>
├── backups/                 # Backups (optionnel)
└── config/                  # Configuration persistée
    └── configurations.json
```

### Sauvegarder Données

#### Manuel (SSH)

```bash
# Créer backup
tar -czf parcinfo-backup-$(date +%Y%m%d).tar.gz \
  /volume1/docker/parcinfo/data \
  /volume1/docker/parcinfo/uploads

# Télécharger sur PC
scp admin@<NAS-IP>:/volume1/docker/parcinfo/parcinfo-backup-*.tar.gz ~/Backups/
```

#### Automatisé (via Task Scheduler Synology)

1. NAS → **Control Panel** → **Task Scheduler**
2. **Create** → **Scheduled Task** → **Custom Script**
3. Remplissez :
   - **Task name** : `ParcInfo Daily Backup`
   - **Schedule** : Daily, 2:00 AM
   - **Script** :
   ```bash
   tar -czf /volume1/backups/parcinfo-backup-$(date +%Y%m%d).tar.gz \
     /volume1/docker/parcinfo/data \
     /volume1/docker/parcinfo/uploads
   ```
4. **OK**

### Restaurer Données

```bash
# Arrêter conteneur
docker stop parcinfo-app

# Extraire backup
tar -xzf parcinfo-backup-20260423.tar.gz -C /volume1/docker/parcinfo/

# Redémarrer
docker start parcinfo-app
```

---

## 🐛 Dépannage

### Problème 1 : "Impossible d'accéder à http://nas-ip:8000"

**Causes possibles :**
- Port 8000 bloqué par pare-feu Synology
- Conteneur n'a pas démarré
- Mauvaise adresse IP

**Solutions :**

```bash
# 1. Vérifier conteneur actif
docker ps | grep parcinfo

# Résultat ? Continue à l'étape 2 : 3
# Pas de résultat ? Conteneur arrêté : docker start parcinfo-app

# 2. Vérifier port 8000 ouvert
docker port parcinfo-app

# Output attendu :
# 5000/tcp -> 0.0.0.0:8000

# 3. Vérifier logs erreur
docker logs parcinfo-app

# Si erreur : voir "Problème 3" ci-dessous
```

### Problème 2 : Port 8000 Déjà Utilisé

**Error** : `bind: address already in use`

**Solution** : Changer port dans docker-compose.yml

```yaml
# Avant
ports:
  - "8000:5000"

# Après (utiliser 8001, 8002, etc)
ports:
  - "8001:5000"
```

Puis redéployer :
```bash
docker-compose down
docker-compose up -d
```

### Problème 3 : Conteneur Crash (Erreur au Démarrage)

**Diagnostic :**
```bash
docker logs parcinfo-app
# Lire les erreurs

# Ex output :
# ModuleNotFoundError: No module named 'flask'
# → requirements.txt pas correctement installé
```

**Solutions courantes :**

| Erreur | Cause | Fix |
|--------|-------|-----|
| `ModuleNotFoundError: flask` | requirements.txt incomplet | Vérifier Dockerfile ligne 23 : `pip install -r requirements.txt` |
| `FileNotFoundError: app.py` | app.py manquant du contexte build | Vérifier /volume1/docker/parcinfo/app.py existe |
| `Address already in use` | Port 5000 conflictuel (impossible sur 0.0.0.0:5000 dans conteneur) | Rare, vérifier Dockerfile CMD line |
| `PermissionError: /app` | Permissions fichiers incorrectes | `docker exec parcinfo-app chmod -R 755 /app` |

### Problème 4 : Base de Données Corrompue

```bash
# 1. Arrêter conteneur
docker stop parcinfo-app

# 2. Supprimer BD
rm /volume1/docker/parcinfo/data/parc_info.db

# 3. Redémarrer (BD sera recréée)
docker start parcinfo-app

# Vérifier logs
docker logs parcinfo-app
```

### Problème 5 : Performance Lente

**Causes :**
- Ressources CPU/RAM insuffisantes
- Beaucoup de clients/données
- Indexation incomplète

**Optimisations :**

```bash
# 1. Augmenter ressources
# Modifier docker-compose.yml :
deploy:
  resources:
    limits:
      cpus: '4'          # ↑ 4 cores au lieu de 2
      memory: 1024M      # ↑ 1 GB au lieu de 512MB

# 2. Redéployer
docker-compose down
docker-compose up -d

# 3. Vérifier indexation BD
docker exec parcinfo-app python -c "
from database import get_db
db = get_db()
indexes = db.execute('SELECT name FROM sqlite_master WHERE type=\"index\"').fetchall()
print(f'Indexes: {len(indexes)}')
db.close()
"

# Output attendu : Indexes: 66+
```

### Problème 6 : Session Expire Rapidement

**Config :** Sessions 8h par défaut

**Si souhaite changer :**
```bash
# Modifier app.py ligne ~40 :
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # ↑ 24h

# Reconstruire image
docker build -t parcinfo:2.5.0 .
docker-compose restart
```

---

## 📊 Monitoring & Métriques

### Vérifier Santé Conteneur

```bash
# Health status
docker inspect parcinfo-app | grep -A 10 '"Health"'

# Output attendu :
# "Health": {
#   "Status": "healthy",
#   "FailingStreak": 0,
#   "Log": [...]
# }
```

### Utilisation Ressources

```bash
# CPU & Mémoire en temps réel
docker stats parcinfo-app

# Output :
# CONTAINER     CPU %    MEM USAGE / LIMIT
# parcinfo-app  2.3%     120 MiB / 512 MiB
```

### Logs en Temps Réel

```bash
# Voir logs en direct
docker logs -f parcinfo-app

# Ctrl+C pour quitter
```

---

## 🔐 Sécurité — Checklist Production

- [ ] Modifier mot de passe admin (ne pas laisser admin/admin)
- [ ] Configurer utilisateurs avec ACL appropriée
- [ ] Activer HTTPS (via reverse proxy Synology)
- [ ] Sauvegardes régulières activées
- [ ] Monitoring erreurs activé
- [ ] Logs centralisés (optionnel : ELK stack)
- [ ] Limite de ressources respectée (CPU/RAM)
- [ ] Firewall Synology : port 8000 si nécessaire
- [ ] Credentials identifiants chiffrés ✅ (v2.5.0)
- [ ] Audit trail activé ✅ (v2.5.0)

---

## 🚀 Mise à Jour Vers v2.6 (Future)

Quand nouvelle version sortira :

```bash
# 1. Télécharger v2.6 sur PC
cd parc_info-v2.6

# 2. Transférer sur NAS (remplacer fichiers)
scp -r . admin@<NAS-IP>:/volume1/docker/parcinfo/

# 3. Reconstruire image
ssh admin@<NAS-IP>
cd /volume1/docker/parcinfo
docker build -t parcinfo:2.6.0 .

# 4. Redémarrer conteneur (données persistent)
docker-compose down
docker-compose up -d

# 5. Vérifier nouvelle version
docker ps | grep parcinfo
# Image doit afficher : parcinfo:2.6.0
```

---

## 📞 Support & Ressources

### Logs Essentiels
```bash
# Voir les 100 dernières lignes
docker logs --tail 100 parcinfo-app

# Voir les 10 dernières lignes avec timestamps
docker logs --timestamps --tail 10 parcinfo-app
```

### Commandes Utiles
```bash
# Exécuter commande dans conteneur
docker exec parcinfo-app python -c "import app; print('OK')"

# Accéder à shell du conteneur
docker exec -it parcinfo-app /bin/bash

# Copier fichier de/vers conteneur
docker cp parcinfo-app:/app/parc_info.db ./backup.db
docker cp ./config.json parcinfo-app:/app/config.json
```

### Documentation
- QUICKSTART.md - Démarrage rapide ParcInfo
- CLAUDE.md - Guide développement complet
- RELEASE_NOTES.md - Notes version v2.5.0
- DEPLOYMENT.md - Guide déploiement général

---

**Version** : 2.5.0 Production Ready  
**Date** : 23 avril 2026  
**Format** : Synology NAS Docker Deployment  
**Support** : Voir documentations liées
