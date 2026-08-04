# 🐳 Configuration DockerHub + GitHub Actions

**Objectif** : Automatiser le build et push de l'image Docker vers DockerHub à chaque changement.

---

## 📋 État Actuel

| Composant | Status |
|-----------|--------|
| GitHub Code | ✅ À jour (v2.6.28) |
| GitHub Actions Workflow | ✅ Créé |
| DockerHub Secrets | ❌ À configurer |
| Auto-build DockerHub | ⏳ En attente de secrets |

---

## 🔧 Configuration Requise

### 1️⃣ Générer un Access Token DockerHub

1. Allez sur **https://hub.docker.com/settings/security**
2. Clic **New Access Token**
3. Donnez un nom : `GitHub Actions`
4. **Copiez le token** (vous ne le reverrez qu'une fois)

### 2️⃣ Ajouter les Secrets dans GitHub

1. Allez sur **https://github.com/darkmind64/parc-info/settings/secrets/actions**
2. Clic **New repository secret**
3. Créez deux secrets :

   **Secret 1** :
   - Name: `DOCKER_USERNAME`
   - Value: `darkmind64` (ou votre username DockerHub)

   **Secret 2** :
   - Name: `DOCKER_PASSWORD`
   - Value: `<le token que vous avez copié>` (PAS votre mot de passe!)

### 3️⃣ Vérifier le Workflow

1. Allez sur **https://github.com/darkmind64/parc-info/actions**
2. Cherchez `Build and Push Docker Image`
3. Si vous voyez une erreur, c'est probablement les secrets manquants

---

## 🚀 Déclenchement du Build

Le workflow se déclenche **automatiquement** quand on :
1. Pousse vers `master` ET
2. Change un de ces fichiers :
   - `Dockerfile`
   - `app.py`
   - `requirements.txt`
   - `version.json`
   - `__version__.py`

**Durée du build** : ~5-10 minutes

---

## 📊 Monitoring du Build

### Via GitHub
1. Allez sur **Actions** → **Build and Push Docker Image**
2. Cliquez le workflow le plus récent
3. Vérifiez les logs

### Via DockerHub
1. Allez sur **https://hub.docker.com/r/darkmind64/parcinfo**
2. Onglet **Tags**
3. Cherchez la version la plus récente avec un ✅ (built)

---

## ✅ Comment Vérifier que c'est à Jour

Après un push vers master avec les changements :

```bash
# 1. Attendez 5-10 minutes pour le build
# 2. Vérifiez DockerHub
docker pull darkmind64/parcinfo:latest
docker run --rm darkmind64/parcinfo:latest python3 -c "from __version__ import __version__; print(f'Version: {__version__}')"

# 3. Vérifiez le tag spécifique
docker pull darkmind64/parcinfo:v2.6.28
```

---

## 🔄 Workflow Automatique (Après Configuration)

```
1. Developer pousse code vers master
   ↓
2. GitHub détecte changement dans Dockerfile/app.py/version.json
   ↓
3. GitHub Actions se déclenche
   ↓
4. Build l'image Docker
   ↓
5. Pousse vers DockerHub avec tags :
   - darkmind64/parcinfo:latest
   - darkmind64/parcinfo:v2.6.28
   ↓
6. Utilisateurs peuvent faire "docker pull darkmind64/parcinfo:latest"
```

---

## 🆘 Troubleshooting

### Build Failed - "Authentication failed"
**Cause** : Les secrets ne sont pas configurés
**Solution** : Suivez les étapes 1-2 du "Configuration Requise"

### Build Successful mais DockerHub pas à jour
**Cause** : Peut prendre 5-10 minutes
**Solution** : Attendez et vérifiez DockerHub tags

### Token expiré
**Cause** : Les tokens DockerHub expirent
**Solution** : Créez un nouveau token et mettez à jour les secrets GitHub

---

## 📝 Checklist de Vérification

- [ ] Access Token DockerHub créé
- [ ] Secret `DOCKER_USERNAME` ajouté dans GitHub
- [ ] Secret `DOCKER_PASSWORD` ajouté dans GitHub  
- [ ] Workflow `.github/workflows/docker-build-push.yml` existe
- [ ] Push un changement mineur pour tester le workflow
- [ ] Vérifiez que DockerHub a la nouvelle image en 5-10 min

---

**Status** : En attente de configuration des secrets GitHub Actions
**Prochaine étape** : Suivre les étapes 1-2 du "Configuration Requise"

