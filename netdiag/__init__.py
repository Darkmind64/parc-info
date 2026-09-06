"""netdiag — refonte progressive du diagnostic réseau de ParcInfo.

Package introduit par la refonte 2026-09 (voir ``DIAGNOSTIC_RESEAU.md`` et
``~/.claude/plans/structured-forging-rivest.md``). Les modules sont ajoutés
lot par lot ; ``network_diag.py`` reste le point d'entrée historique et
déléguera ici au fur et à mesure.

- Lot 1 : ``netdiag.collect`` — collecteur SNMP unifié (une passe parallèle
  par équipement, sous budget), qui remplace les balayages séquentiels
  indépendants du palier 3 et du palier 4.
- Lot 2 : ``netdiag.analyse`` — analyse SNMP par port (fonctions pures,
  classification en clair des erreurs de trafic) ; ``netdiag.events`` —
  auto-résolution des évènements de port.
"""

from . import analyse, collect, events  # noqa: F401

__all__ = ['analyse', 'collect', 'events']
