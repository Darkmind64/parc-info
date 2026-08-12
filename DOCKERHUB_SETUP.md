# 🐳 Configuration DockerHub + GitHub Actions

**Objectif** : Automatiser le build et push de l'image Docker vers DockerHub à chaque nouvelle version.

---

## 📋 État Actuel

✅ **Opérationnel** — chaque publication (`git tag vX.Y.Z` + `git push origin vX.Y.Z`)
construit et publie automatiquement l'image Docker, en service depuis
plusieurs dizaines de versions.

| Composant | Status |
|-----------|--------|
| GitHub Actions Workflow (`.github/workflows/build-release.yml`) | ✅ Actif |
| DockerHub Secrets | ✅ Configurés |
| Auto-build DockerHub | ✅ Opérationnel |

Le reste de ce document décrit **comment cette configuration a été mise en
place** — utile si les secrets doivent être régénérés (un token DockerHub
expire ou est révoqué), mais il n'y a rien à faire pour une utilisation
normale.

---

## 🔧 Configuration Requise (si les secrets sont à régénérer)

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

Le workflow (`build-release.yml`) se déclenche sur le **push d'un tag** de la
forme `vX.Y.Z` — pas sur un simple push vers `master`. Le tag est ce qui
distingue « du code committé » de « une version publiée » :

```bash
git tag -a v2.9.7 -m "..."
git push origin v2.9.7
```

Suites de tests → build des binaires (Windows/macOS) → build & push de
l'image Docker → création de la release GitHub avec les binaires et
`SHA256SUMS.txt` : tout dans le même run, rien n'est publié tant que les
tests ne passent pas.

**Durée du build** : ~10-15 minutes (tests + 4 binaires + image Docker)

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
docker pull darkmind64/parcinfo:v2.9.7
```

---

## 🔄 Workflow Automatique

```
1. Version bumpée (VERSION, version.json, __version__.py, Dockerfile,
   README) + CHANGELOG.md, vérifiés par verifier_version.py, committés
   ↓
2. git tag vX.Y.Z && git push origin vX.Y.Z
   ↓
3. GitHub Actions se déclenche (build-release.yml)
   ↓
4. Suites de tests (échec → rien n'est publié)
   ↓
5. Build des binaires (Windows ParcInfo/collecteurs, macOS ARM)
   ↓
6. Build & push de l'image Docker, tags :
   - darkmind64/parcinfo:latest
   - darkmind64/parcinfo:vX.Y.Z
   ↓
7. Release GitHub créée avec les binaires + SHA256SUMS.txt
   ↓
8. Utilisateurs : "docker pull darkmind64/parcinfo:latest", ou la bannière
   de mise à jour dans l'app pour les exécutables Windows/macOS
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

## 📝 Checklist (en cas de reconfiguration)

- [ ] Access Token DockerHub créé
- [ ] Secret `DOCKER_USERNAME` ajouté dans GitHub
- [ ] Secret `DOCKER_PASSWORD` ajouté dans GitHub
- [ ] Workflow `.github/workflows/build-release.yml` existe
- [ ] Publier un tag `vX.Y.Z` pour tester le workflow
- [ ] Vérifiez que DockerHub a la nouvelle image en 10-15 min

---

**Status** : Opérationnel depuis plusieurs dizaines de versions.
**Dernière mise à jour** : 2026-08-12 (v2.9.7)

