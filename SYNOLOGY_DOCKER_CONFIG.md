# ⚙️ Configuration Synology Docker — Ressources & Limites

**Important :** Synology NAS UI ne supporte pas les sections `deploy.resources` et `healthcheck` avancées du docker-compose.yml v3.8.

Ce guide explique comment configurer les ressources **via l'interface web Synology**.

---

## 🐳 docker-compose.yml — Version Simplifiée

Le fichier a été **simplifié à v3.5** pour compatibilité complète Synology :

```yaml
version: '3.5'  # ← Changé de 3.8 (v3.8 features non supportées)

services:
  parcinfo:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: parcinfo-app
    restart: unless-stopped
    ports:
      - "8000:5000"
    environment:
      FLASK_ENV: production
      PYTHONUNBUFFERED: "1"
      TZ: Europe/Paris
    volumes:
      - parcinfo-db:/app
      - parcinfo-uploads:/app/uploads
      - parcinfo-backups:/data/backups
      - parcinfo-config:/data/config
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  parcinfo-db:
    driver: local
  parcinfo-uploads:
    driver: local
  parcinfo-backups:
    driver: local
  parcinfo-config:
    driver: local
```

**✅ Désormais compatible à 100% avec Synology Docker UI!**

---

## ⚡ Configurer Ressources via Interface Web Synology

Puisque `deploy.resources` n'est pas supporté en compose, **configurez les limites via la web UI** :

### Étape 1 : Créer/Lancer Conteneur

1. **Ouvrir Docker Application** (sur NAS)
   - NAS → Applications → Docker

2. **Onglet "Image"**
   - Chercher l'image `parcinfo:2.5.0` (après `docker build`)
   - Clic droit → **Lancer**

3. **Pop-up Créer Conteneur** → Remplir champs détails ci-dessous

### Étape 2 : Paramètres Basiques

| Champ | Valeur |
|-------|--------|
| **Nom du conteneur** | `parcinfo-app` |
| **Image** | `parcinfo:2.5.0` |
| **Ports** | Local: `8000` → Conteneur: `5000` |

### Étape 3 : Environnement (Onglet "Environnement")

```
FLASK_ENV=production
PYTHONUNBUFFERED=1
TZ=Europe/Paris
```

### Étape 4 : Volumes (Onglet "Volumes")

**Créer 4 mappings :**

| Local Path | Container Path | Utilité |
|-----------|---|---|
| `/volume1/docker/parcinfo/data` | `/app` | Base de données |
| `/volume1/docker/parcinfo/uploads` | `/app/uploads` | Documents joints |
| `/volume1/docker/parcinfo/backups` | `/data/backups` | Backups |
| `/volume1/docker/parcinfo/config` | `/data/config` | Configuration |

**💡 Conseil :** Utiliser dossiers locaux plutôt que volumes nommés → plus facile de gérer manuellement

### Étape 5 : Limites Ressources (Onglet "Avancé")

#### ⭐ **Configuration Standard** (Recommandée)

Pour NAS moyen (DS720, DS920) :

```
Limite CPU: 2 cores
Limite Mémoire: 512 MB
```

#### 📊 Profils selon NAS

**Petit NAS (DS218, DS419, DS419slim)**
```
Limite CPU: 1 core
Limite Mémoire: 256 MB
```

**NAS Moyen (DS720, DS920, DS720+)**
```
Limite CPU: 2 cores
Limite Mémoire: 512 MB  ← STANDARD
```

**NAS Puissant (DS1621, RS1221, RS822)**
```
Limite CPU: 4 cores
Limite Mémoire: 1024 MB
```

### Étape 6 : Redémarrage Auto (Onglet "Avancé")

✅ **Cocher :** "Redémarrer automatiquement"

**Stratégie :** `unless-stopped`
- Conteneur redémarre après crash NAS
- Se ferme proprement si arrêt manuel
- Recommandé pour production

### Étape 7 : Réseau

✅ **Réseau : Bridge** (défaut)

Pas besoin de config supplémentaire.

### Étape 8 : Appliquer & Démarrer

1. Cliquez **Appliquer**
2. Docker crée & lance conteneur
3. Attendez 1-2 minutes (démarrage première fois)
4. Vérifier status : "En exécution" ✅

---

## 🔧 Configuration Avancée

### Augmenter Ressources Plus Tard

Si application lente après déploiement :

1. **Docker** → **Conteneur** → sélectionner `parcinfo-app`
2. Clic droit → **Éditer**
3. Onglet "Avancé"
4. Augmenter **Limite CPU** ou **Limite Mémoire**
5. **Appliquer** → Conteneur redémarre

### Variables Personnalisées

Modifier via éditeur texte sur NAS :

```bash
ssh admin@<NAS-IP>
nano /volume1/docker/parcinfo/docker-compose.yml

# Modifier section environment:
environment:
  FLASK_ENV: production
  PYTHONUNBUFFERED: "1"
  TZ: Europe/Paris
  CACHE_TTL: "300"          # ← Ajouter si besoin
  COMPRESSION_LEVEL: "6"    # ← Ajouter si besoin
```

Puis redémarrer :
```bash
docker-compose restart
```

---

## ✅ Vérification Post-Configuration

### Test 1 : Conteneur Actif

```bash
ssh admin@<NAS-IP>
docker ps | grep parcinfo

# Output attendu:
# parcinfo-app  ... Up X minutes  0.0.0.0:8000->5000/tcp
```

### Test 2 : Accès Web

```
http://<NAS-IP>:8000
```

Vous devriez voir page login ParcInfo.

### Test 3 : Ressources Utilisées

```bash
docker stats parcinfo-app

# Exemple output:
# CONTAINER  CPU%   MEM USAGE / LIMIT
# parcinfo   1.2%   120 MiB / 512 MiB
```

Si `MEM USAGE` approche limite → augmenter ressources.

---

## 🐛 Dépannage Synology

### Problème 1 : "Format Invalid"

**Cause :** Ancienne version docker-compose.yml avec `deploy.resources`

**Solution :**
```bash
# Télécharger nouveau docker-compose.yml depuis GitHub
# Ou éditer directement et supprimer sections non compatibles:
# - deploy:
# - healthcheck:
# - networks: (optionnel)
```

### Problème 2 : Conteneur Ne Démarre Pas

```bash
docker logs parcinfo-app

# Si erreur "out of memory":
# → Augmenter limite mémoire via web UI
# → Ou réduire caches (CACHE_TTL=180)
```

### Problème 3 : Volumes Vides

**Cause :** Chemins locaux n'existent pas

**Solution :**
```bash
# Créer dossiers avant de lancer conteneur
mkdir -p /volume1/docker/parcinfo/{data,uploads,backups,config}

# Vérifier permissions
ls -la /volume1/docker/parcinfo/
```

---

## 📝 Comparaison Versions Compose

| Feature | v3.5 (Synology) | v3.8 (Docker Desktop) |
|---------|---|---|
| Services basiques | ✅ | ✅ |
| Volumes | ✅ | ✅ |
| Environnement | ✅ | ✅ |
| Logging | ✅ | ✅ |
| **deploy.resources** | ❌ | ✅ |
| **healthcheck** | ⚠️ (limité) | ✅ |
| **networks** | ⚠️ (basique) | ✅ |

---

## 💡 Best Practices Synology

1. **Chemins locaux > volumes nommés**
   - Plus facile de gérer en File Station
   - Backups plus simples

2. **Logs doivent être rotatés**
   - `max-size: 10m` (défaut)
   - Évite disque plein

3. **Redémarrage auto active**
   - `unless-stopped` (recommandé)
   - Conteneur survit crash/reboot NAS

4. **Monitorer ressources**
   - Vérifier `docker stats` régulièrement
   - Augmenter si approche limites

5. **Pas de networking complexe**
   - Utiliser `bridge` (défaut)
   - Éviter configurations Swarm

---

## 🚀 Résumé Configuration

```bash
# 1. Construire image
docker build -t parcinfo:2.5.0 .

# 2. Lancer via docker-compose simplifié
docker-compose up -d

# 3. (Ou créer via web UI avec limites)
# Docker → Image → Parcinfo:2.5.0 → Lancer
# → Appliquer limites CPU/RAM via "Avancé"

# 4. Vérifier status
docker ps | grep parcinfo

# 5. Accéder
# http://<NAS-IP>:8000
```

---

**Statut :** ✅ docker-compose.yml conforme Synology  
**Version Compose :** v3.5 (compatible Synology)  
**Date :** 23 avril 2026
