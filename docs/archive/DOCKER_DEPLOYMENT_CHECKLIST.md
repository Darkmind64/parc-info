# ✅ ParcInfo v2.5.0 — Docker Deployment Checklist

**Date** : 23 avril 2026  
**Version** : 2.5.0 - Production Ready  
**Plateforme** : Synology NAS

---

## 📋 Pré-Déploiement (À faire sur PC)

### 1. Fichiers Projet
- [ ] Vérifier que tous fichiers Python présents
  - [ ] app.py
  - [ ] database.py
  - [ ] auth_utils.py
  - [ ] config_helpers.py
  - [ ] client_helpers.py
  - [ ] models.py
  - [ ] (autres modules .py)
- [ ] Vérifier dossier `templates/` complet (25+ fichiers .html)
- [ ] Vérifier dossier `static/` présent (JS, CSS, images)
- [ ] Vérifier fichiers de config Docker
  - [ ] Dockerfile
  - [ ] docker-compose.yml
  - [ ] requirements.txt
- [ ] Vérifier fichiers de documentation
  - [ ] QUICKSTART.md
  - [ ] DEPLOYMENT.md
  - [ ] SYNOLOGY_DEPLOYMENT.md
  - [ ] RELEASE_NOTES.md
  - [ ] FINAL_SUMMARY.txt

### 2. Validations
- [ ] Vérifier version Python ≥ 3.8
- [ ] Vérifier Docker installé sur PC (test : `docker --version`)
- [ ] Vérifier accès SSH fonctionnel
  - [ ] `ssh -V` fonctionnel
  - [ ] Clé SSH configurée (optionnel mais recommandé)
- [ ] Vérifier accès au NAS
  - [ ] Adresse IP NAS connue
  - [ ] Admin credentials disponibles

---

## 🚀 Déploiement — Étapes Principales

### Étape 1️⃣ : Préparation Fichiers

- [ ] **Copier fichiers sur NAS** (via File Station ou SCP)
  - [ ] Dossier destination : `/volume1/docker/parcinfo/`
  - [ ] Vérifier structure : Dockerfile, docker-compose.yml, app.py, etc.

```bash
# Via SCP (alternatif)
scp -r C:\parc_info admin@192.168.1.100:/volume1/docker/
```

### Étape 2️⃣ : Installation Docker

- [ ] **Docker installé sur NAS?**
  - [ ] Si OUI : continuer
  - [ ] Si NON : Package Center → Docker → Installer

```bash
# Vérifier (SSH):
docker --version
# Output: Docker version X.Y.Z
```

### Étape 3️⃣ : Construction Image

- [ ] **Accéder NAS via SSH**

```bash
ssh admin@192.168.1.100
# Entrer mot de passe
```

- [ ] **Construire image Docker**

```bash
cd /volume1/docker/parcinfo
docker build -t parcinfo:2.5.0 .
# Attendre 2-5 minutes...
```

- [ ] **Vérifier image créée**

```bash
docker images | grep parcinfo
# Output: parcinfo  2.5.0  ...  850MB
```

### Étape 4️⃣ : Créer Conteneur

- [ ] **Via Docker Compose (recommandé)**

```bash
cd /volume1/docker/parcinfo
docker-compose up -d
# Attendre 1 minute...
```

- [ ] **Ou : Via Interface Docker NAS**
  - [ ] Docker → Conteneur → Créer
  - [ ] Remplir champs (voir SYNOLOGY_DEPLOYMENT.md)

- [ ] **Vérifier conteneur actif**

```bash
docker ps | grep parcinfo
# Output: parcinfo-app ... Up X minutes
```

### Étape 5️⃣ : Vérification Connexion

- [ ] **Accéder ParcInfo** : `http://<NAS-IP>:8000`
  - [ ] Remplacer `<NAS-IP>` par IP NAS (ex: 192.168.1.100)
  - [ ] URL complète : `http://192.168.1.100:8000`

- [ ] **Page de login visible**
  - [ ] Logo ParcInfo présent
  - [ ] Champs login/password visibles
  - [ ] Pas d'erreur 404/500

### Étape 6️⃣ : Configuration Initiale

- [ ] **Login avec credentials par défaut**
  - [ ] Login: `admin`
  - [ ] Mot de passe: `admin`

- [ ] **⚠️ Changer mot de passe admin** (IMMÉDIATEMENT!)
  - [ ] Menu ⚙️ (haut-droit)
  - [ ] Profil → Changer mot de passe
  - [ ] Entrer nouveau mot de passe sécurisé

- [ ] **Créer premier client**
  - [ ] Bouton ➕ Nouveau
  - [ ] Remplir : Nom client, contact, etc.
  - [ ] Valider

### Étape 7️⃣ : Validation Optimisations (v2.5.0)

- [ ] **Tester Recherche Globale** (5ms)
  - [ ] Barre recherche navbar
  - [ ] Tapez nom d'appareil existant
  - [ ] Résultats apparaissent
  - [ ] Clic résultat → navigation OK

- [ ] **Tester Autocomplete** (6ms)
  - [ ] Menu ⚙️ → Maintenance → Nouvelle
  - [ ] Cliquez champ "Appareil"
  - [ ] Tapez quelques caractères
  - [ ] Suggestions dynamiques

- [ ] **Tester Chiffrement** 
  - [ ] Menu 🔐 Identifiants
  - [ ] Ajouter credential : login/password
  - [ ] Sauvegarder
  - [ ] Réouvrir → password ✓ caché

- [ ] **Performance**
  - [ ] Pages se chargent rapidement (< 1s)
  - [ ] Listes paginées instantanées
  - [ ] F12 DevTools → Network → voir "br" (Brotli) en Content-Encoding

---

## 💾 Données & Persistence

### Backup

- [ ] **Créer backup initial**

**Option 1 : Via Script (recommandé)**
```bash
ssh admin@192.168.1.100
cd /volume1/docker/parcinfo
chmod +x backup-parcinfo.sh
./backup-parcinfo.sh
```

**Option 2 : Manuellement**
```bash
tar -czf parcinfo-backup-$(date +%Y%m%d).tar.gz \
  /volume1/docker/parcinfo/data \
  /volume1/docker/parcinfo/uploads
```

- [ ] **Télécharger backup sur PC**

```bash
scp admin@192.168.1.100:/volume1/docker/parcinfo/parcinfo-backup-*.tar.gz \
  C:\Backups\
```

- [ ] **Archiver en sécurité** (disque externe, cloud, etc.)

### Restore (Procédure Sinistre)

- [ ] **Procédure documentée** : voir restore-parcinfo.sh
- [ ] **Tester restauration** une fois (avant incident!)
- [ ] **Garder backups** à jour (quotidien/hebdo)

---

## 🔧 Configuration & Optimisation

### Dimensionnement Ressources

**Choisir profil selon NAS :**

| NAS | Profil | CPU | RAM |
|-----|--------|-----|-----|
| Petit (DS218, DS419) | Light | 1 core | 256M |
| Moyen (DS720, DS920) | Standard | 2 cores | 512M |
| Puissant (DS1621, RS1221) | Heavy | 4 cores | 1G |

```bash
# Appliquer profil Light (exemple)
docker-compose -f docker-compose.yml \
  -f docker-compose.synology.yml up -d
```

- [ ] **Adapter resources** dans docker-compose.yml si besoin
- [ ] **Redémarrer** : `docker-compose restart`

### Configuration .env (Optionnel)

- [ ] **Copier .env.example → .env**

```bash
cp .env.example .env
```

- [ ] **Adapter paramètres** (cache TTL, compression, etc.)
- [ ] **Appliquer** : `docker-compose restart`

---

## 🔐 Sécurité — Post-Déploiement

- [ ] **Mot de passe admin changé** ✅ (fait en Étape 6)
- [ ] **Utilisateurs supplémentaires créés**
  - [ ] Menu ⚙️ → Utilisateurs
  - [ ] Ajouter users avec rôles appropriés
- [ ] **ACL multi-client configurée** (si applicable)
  - [ ] Menu 👥 → Partage clients
  - [ ] Définir accès lecture/écriture
- [ ] **Audit trail activé** ✅ (défaut v2.5.0)
  - [ ] Voir historique : Menu ⚙️ → Historique
- [ ] **Chiffrement credentials** ✅ (défaut v2.5.0)
  - [ ] Credentials automatiquement chiffrés

### Firewall & Réseau

- [ ] **Port 8000 accessible** depuis réseau interne
  - [ ] Si besoin accès externe : configurer reverse proxy HTTPS
  - [ ] NE PAS exposer port 8000 direct sur internet sans HTTPS

```bash
# Exemple nginx reverse proxy (avancé)
# À faire sur reverse proxy externe
server {
    listen 443 ssl;
    server_name parcinfo.example.com;
    
    location / {
        proxy_pass http://192.168.1.100:8000;
        proxy_set_header Host $host;
    }
}
```

---

## 📊 Monitoring & Maintenance

### Health Check

- [ ] **Vérifier santé conteneur**

```bash
docker inspect parcinfo-app | grep -A 5 '"Health"'
# Output: "Status": "healthy"
```

- [ ] **Consulter logs** s'il y a problèmes

```bash
docker logs --tail 50 parcinfo-app
```

### Maintenance Récurrente

- [ ] **Backups automatisés** (Task Scheduler NAS)
  - [ ] Control Panel → Task Scheduler
  - [ ] Create → Scheduled Task → Custom Script
  - [ ] Voir SYNOLOGY_DEPLOYMENT.md pour script complet
  - [ ] Fréquence : quotidienne (2h du matin)

- [ ] **Logs archivés** (garder disque libre)
  - [ ] Docker logs auto-rotatent (voir docker-compose.yml)
  - [ ] Pas d'action manuelle requise

- [ ] **Mise à jour future** (v2.6+)
  - [ ] Lire RELEASE_NOTES.md nouvelle version
  - [ ] Faire backup avant mise à jour
  - [ ] Reconstruire image : `docker build ...`
  - [ ] Relancer : `docker-compose restart`

---

## 🧪 Tests de Validation

### Test Fonctionnalités Critiques

- [ ] **CRUD Appareils**
  - [ ] Ajouter appareil
  - [ ] Modifier appareil
  - [ ] Supprimer appareil
  - [ ] Afficher liste

- [ ] **Recherche Globale**
  - [ ] Barre search navbar
  - [ ] Résultats groupés par type
  - [ ] Navigation vers entités

- [ ] **Autocomplete**
  - [ ] Formulaires avec suggestions
  - [ ] Sélection d'entités rapide

- [ ] **Authentification**
  - [ ] Login/Logout
  - [ ] Session timeout (8h)
  - [ ] Rate-limiting (10 tentatives)

- [ ] **ACL & Multi-client**
  - [ ] Utilisateur voir ses clients seulement
  - [ ] Propriétaire peut modifier client
  - [ ] Utilisateur lecture-only ne peut pas modifier

- [ ] **Persistence**
  - [ ] Arrêter conteneur : `docker stop parcinfo-app`
  - [ ] Vérifier données persistent
  - [ ] Redémarrer : `docker start parcinfo-app`
  - [ ] Vérifier données intactes

### Test Performance

- [ ] **Charge** (optionnel)
  - [ ] Ajouter 1000 appareils
  - [ ] Vérifier vitesse acceptable
  - [ ] Vérifier indexation fonctionne

- [ ] **Bande passante**
  - [ ] DevTools Network
  - [ ] Voir "br" (Brotli) en Content-Encoding
  - [ ] Taille requests compressée (~30% original)

---

## 🚨 Dépannage Rapide

### Problème : Pas d'accès à http://nas-ip:8000

**Checklist:**
1. [ ] Conteneur actif? `docker ps | grep parcinfo`
2. [ ] Port 8000 correct? `docker port parcinfo-app`
3. [ ] Firewall bloque port 8000? (tester ping NAS)
4. [ ] Logs erreur? `docker logs parcinfo-app`

**Solutions:**
```bash
# Redémarrer conteneur
docker restart parcinfo-app

# Vérifier port libre
netstat -an | grep 8000

# Voir logs complets
docker logs -f parcinfo-app
```

### Problème : Conteneur crash

**Diagnostic:**
```bash
docker logs parcinfo-app  # Voir erreur
docker inspect parcinfo-app | grep ExitCode
```

**Solutions courantes:**
- [ ] requirements.txt incomplet → `docker build -t parcinfo:2.5.0 .`
- [ ] Permissions fichiers → `docker exec parcinfo-app chmod -R 755 /app`
- [ ] BD corrompue → supprimer `/volume1/docker/parcinfo/data/parc_info.db`

### Problème : Base de données pleine

```bash
# Vérifier espace
df /volume1/docker/parcinfo

# Nettoyer anciens logs
docker logs --tail 1000 parcinfo-app > /dev/null
```

---

## 📝 Post-Déploiement

### Documentation à Lire

- [ ] QUICKSTART.md — utilisation quotidienne
- [ ] CLAUDE.md — développement & architecture
- [ ] SYNOLOGY_DEPLOYMENT.md — ce que vous avez suivi
- [ ] RELEASE_NOTES.md — nouvelles features v2.5.0

### Notification Équipe

- [ ] URL d'accès : `http://<NAS-IP>:8000`
- [ ] Credentials de départ : admin/admin (changé en Étape 6)
- [ ] Lien documentation pour utilisateurs

### Training Utilisateurs (Optionnel)

- [ ] Créer utilisateurs finaux
- [ ] Montrer interface de base
- [ ] Montrer recherche globale
- [ ] Montrer fonctionnalités clés

---

## ✅ Déploiement Réussi!

Une fois TOUS les checklist complétés, ParcInfo v2.5.0 est :

✅ **Installé** sur Synology NAS  
✅ **Accessible** via `http://<NAS-IP>:8000`  
✅ **Configuré** avec mot de passe sécurisé  
✅ **Optimisé** avec indexation DB + recherche + compression  
✅ **Sécurisé** avec ACL + chiffrement credentials  
✅ **Validé** avec tests fonctionnels  
✅ **Sauvegardé** avec backups persistants  
✅ **Documenté** pour utilisation & maintenance  

---

## 📞 Support & Escalade

**Si problème rencontré :**

1. [ ] Consulter SYNOLOGY_DEPLOYMENT.md → section "Dépannage"
2. [ ] Vérifier logs : `docker logs parcinfo-app`
3. [ ] Essayer restart : `docker restart parcinfo-app`
4. [ ] Supprimer/reconstruire conteneur si persistent
5. [ ] Consulter CLAUDE.md pour architecture détaillée

---

**Checklist Version** : 1.0  
**Dernière mise à jour** : 23 avril 2026  
**Statut** : ✅ PRODUCTION READY
