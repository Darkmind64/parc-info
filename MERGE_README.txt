╔═══════════════════════════════════════════════════════════╗
║       Fusion Automatisée: uploads_sync + Widget System    ║
╚═══════════════════════════════════════════════════════════╝

📋 DÉMARRAGE RAPIDE
═══════════════════════════════════════════════════════════

WINDOWS
-------
  Option 1 (recommandée): Double-clic sur merge_uploads_sync.cmd
  Option 2: Lancer PowerShell et exécuter: .\merge_uploads_sync.ps1
  Option 3: Lancer CMD et exécuter: merge_uploads_sync.cmd

LINUX / macOS
-------------
  chmod +x merge_uploads_sync.sh
  ./merge_uploads_sync.sh


📊 CE QUE FAIT LE SCRIPT
═══════════════════════════════════════════════════════════

  ✓ Vérifie que vous êtes dans un repo git
  ✓ Détecte les branches disponibles
  ✓ Demande confirmation avant de fusionner
  ✓ Fusionne uploads_sync + widget system vers master
  ✓ Affiche un résumé avec les prochaines étapes


⚙️ PRÉREQUIS
═══════════════════════════════════════════════════════════

  ✓ Git installé et en PATH
  ✓ Pas de changements non-committés
    (ou le script vous demandera confirmation)
  ✓ Accès à la branche master


⚠️ SITUATIONS SPÉCIALES
═══════════════════════════════════════════════════════════

  Si vous voyez: "master is already used by worktree"
  → Sortir du worktree: cd "E:\Claude Code\parc_info - docker"
  → Relancer le script

  Si conflits de merge:
  → Le script affichera les fichiers en conflit
  → Les résoudre manuellement, puis git add . && git commit


📋 APRÈS LA FUSION
═══════════════════════════════════════════════════════════

  1. Migrer les uploads existants:
     python migrate_uploads.py

  2. Tester l'application:
     python app.py

  3. Vérifier la synchronisation:
     Attendre 60 secondes et vérifier les logs


📖 DOCUMENTATION COMPLÈTE
═══════════════════════════════════════════════════════════

  Voir MERGE_GUIDE.md pour plus de détails:
    - Troubleshooting
    - Vérifications post-merge
    - Configuration Turso
    - Résolution des conflits


🚀 COMMANDES RAPIDES
═══════════════════════════════════════════════════════════

  # Vérifier l'état
  git status
  git log --oneline -5

  # Vérifier les fichiers modifiés
  git show HEAD --name-status

  # Migrer les uploads
  python migrate_uploads.py

  # Tester l'application
  python app.py


❓ AIDE
═══════════════════════════════════════════════════════════

  Voir MERGE_GUIDE.md pour:
  - Instructions détaillées
  - Troubleshooting complet
  - Prochaines étapes
  - Questions fréquentes


═══════════════════════════════════════════════════════════
  Créé avec 🤖 Claude Code
  Dernière mise à jour: 2026-04-25
═══════════════════════════════════════════════════════════
