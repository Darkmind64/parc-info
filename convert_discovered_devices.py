"""
convert_discovered_devices.py
Convertit un CSV "Discovered Devices" (Atera, NinjaRMM, etc.)
au format d'import ParcInfo (CSV ';', UTF-8 BOM).

Usage :
    python convert_discovered_devices.py <fichier_source.csv> [fichier_sortie.csv]

Si le fichier de sortie n'est pas précisé, le script génère
  <fichier_source>_parcinfo.csv  dans le même dossier.
"""

import csv
import io
import os
import re
import sys
from datetime import datetime


# ─── COLONNES CIBLES (ordre exact attendu par ParcInfo) ──────────────────────

COLS_PARCINFO = [
    'nom_machine', 'type_appareil', 'marque', 'modele', 'numero_serie',
    'adresse_ip', 'adresse_mac', 'nom_dns', 'utilisateur', 'service', 'localisation',
    'date_achat', 'duree_garantie', 'date_fin_garantie', 'fournisseur', 'prix_achat',
    'numero_commande', 'os', 'version_os', 'ram', 'cpu', 'stockage', 'statut',
    'ports_ouverts', 'notes', 'user_login', 'user_password',
    'admin_login', 'admin_password', 'anydesk_id', 'anydesk_password',
    'date_creation', 'date_maj',
]

# ─── CORRESPONDANCES DE TYPES ─────────────────────────────────────────────────
# Source (Device type) → ParcInfo (type_appareil)

TYPE_MAP = {
    'workstation':  'PC',
    'server':       'Serveur',
    'laptop':       'PC Portable',
    'printer':      'Imprimante',
    'router':       'Routeur',
    'switch':       'Switch',
    'firewall':     'Routeur',
    'nas':          'NAS',
    'access point': 'Borne Wi-Fi',
    'phone':        'Téléphone IP',
    'mobile':       'Téléphone IP',
    'camera':       'Caméra IP',
    'unknown':      '',
    '':             '',
}


# ─── UTILITAIRES ─────────────────────────────────────────────────────────────

def clean_mac(mac: str) -> str:
    """Normalise une adresse MAC : retire les espaces, met en majuscules."""
    mac = mac.strip()
    if not mac:
        return ''
    # Garder seulement les caractères hex et les séparateurs
    mac = re.sub(r'[^0-9a-fA-F:\-]', '', mac)
    # Normaliser le séparateur en ':'
    mac = mac.replace('-', ':').upper()
    return mac


def parse_date(date_str: str) -> str:
    """
    Convertit une date source en date ISO YYYY-MM-DD.
    Formats reconnus :
        2026/01/19 11:49:25 AM   (format Atera)
        2026-01-19 11:49:25
        2026-01-19
    Retourne '' si non reconnu.
    """
    date_str = date_str.strip()
    if not date_str:
        return ''
    for fmt in (
        '%Y/%m/%d %I:%M:%S %p',   # 2026/01/19 11:49:25 AM
        '%Y/%m/%d %H:%M:%S',      # 2026/01/19 11:49:25
        '%Y-%m-%d %H:%M:%S',      # 2026-01-19 11:49:25
        '%Y-%m-%d',               # 2026-01-19
        '%d/%m/%Y',               # 19/01/2026
    ):
        try:
            return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return ''


def map_type(device_type: str) -> str:
    """Traduit le type source vers un type_appareil ParcInfo."""
    return TYPE_MAP.get(device_type.strip().lower(), device_type.strip())


def clean_name(name: str) -> str:
    """Nettoie un nom de machine : retire les espaces superflus."""
    return name.strip()


def is_valid_name(name: str) -> bool:
    """
    Un nom valide n'est pas vide, pas 'Unknown', et ne commence pas
    par '_' (artefacts de découverte comme _dosvc, _http).
    """
    n = name.strip()
    return bool(n) and n.lower() != 'unknown' and not n.startswith('_')


def clean_manufacturer(mfr: str) -> str:
    """Retourne la marque ou '' si 'null'."""
    mfr = mfr.strip()
    return '' if mfr.lower() in ('null', 'none', '') else mfr


# ─── CONVERSION ──────────────────────────────────────────────────────────────

def convert(src_path: str, dst_path: str) -> tuple:
    """
    Lit src_path, convertit chaque ligne, écrit dst_path.
    Retourne (nb_ok, nb_skipped, warnings).
    """
    # Lecture source (UTF-8 ou Latin-1 en fallback)
    for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            with open(src_path, encoding=enc, newline='') as fh:
                content = fh.read()
            break
        except UnicodeDecodeError:
            continue

    # Détecter le séparateur (virgule ou point-virgule)
    first_line = content.splitlines()[0] if content.strip() else ''
    delimiter  = ';' if first_line.count(';') > first_line.count(',') else ','

    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

    if not reader.fieldnames:
        raise ValueError("Fichier CSV vide ou sans en-tête.")

    # Vérification souple des colonnes sources attendues
    fields_lower = {f.lower().strip(): f for f in reader.fieldnames}
    required = ['ip address', 'device name']
    missing  = [r for r in required if r not in fields_lower]
    if missing:
        raise ValueError(
            f"Colonnes sources manquantes : {missing}\n"
            f"Colonnes détectées : {list(reader.fieldnames)}"
        )

    def get(row, *candidates):
        """Récupère la première colonne trouvée parmi plusieurs noms possibles."""
        for c in candidates:
            for k, orig in fields_lower.items():
                if k == c.lower():
                    return row.get(orig, '')
        return ''

    out      = io.StringIO()
    writer   = csv.writer(out, delimiter=';', lineterminator='\r\n')
    writer.writerow(COLS_PARCINFO)

    nb_ok      = 0
    nb_skipped = 0
    warnings   = []

    for i, row in enumerate(reader, start=2):  # ligne 2 = première data
        ip      = get(row, 'ip address').strip()
        raw_name = get(row, 'device name', 'hostname', 'name')
        mac     = clean_mac(get(row, 'mac address', 'mac'))
        mfr     = clean_manufacturer(get(row, 'manufacture', 'manufacturer', 'vendor'))
        d_type  = get(row, 'device type', 'type')
        d_last  = parse_date(get(row, 'last discovery date', 'last seen', 'date'))
        d_first = parse_date(get(row, 'first discovery date', 'first seen'))

        # Nom de machine : utiliser l'IP si le nom n'est pas exploitable
        if is_valid_name(raw_name):
            nom = clean_name(raw_name)
        elif ip:
            nom = ip
            if raw_name.strip().lower() not in ('unknown', ''):
                # Préserver le nom d'origine en note
                warnings.append(
                    f"Ligne {i} : nom '{raw_name.strip()}' ignoré (artefact), IP utilisée : {ip}"
                )
        else:
            nb_skipped += 1
            warnings.append(f"Ligne {i} : ignorée (pas de nom ni d'IP valide).")
            continue

        # Statut
        raw_statut = get(row, 'status', 'statut')
        statut = 'actif'
        if raw_statut.strip().lower() in ('inactive', 'inactif', 'hors service', 'offline'):
            statut = 'inactif'

        # Notes : conserver le nom si on n'a pas pu l'utiliser
        notes_parts = []
        if not is_valid_name(raw_name) and raw_name.strip() and raw_name.strip().lower() != 'unknown':
            notes_parts.append(f"Nom découvert : {raw_name.strip()}")
        extra_notes = get(row, 'notes', 'description', 'comments').strip()
        if extra_notes:
            notes_parts.append(extra_notes)
        notes = ' | '.join(notes_parts)

        record = {k: '' for k in COLS_PARCINFO}
        record.update({
            'nom_machine':   nom,
            'type_appareil': map_type(d_type),
            'marque':        mfr,
            'adresse_ip':    ip,
            'adresse_mac':   mac,
            'statut':        statut,
            'notes':         notes,
            'date_creation': d_first or d_last,
            'date_maj':      d_last or d_first,
        })

        writer.writerow([record[c] for c in COLS_PARCINFO])
        nb_ok += 1

    # Écriture sortie avec BOM UTF-8 (pour Excel)
    with open(dst_path, 'w', encoding='utf-8-sig', newline='') as fh:
        fh.write(out.getvalue())

    return nb_ok, nb_skipped, warnings


# ─── POINT D'ENTRÉE ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = sys.argv[1]
    if not os.path.isfile(src):
        print(f"Erreur : fichier introuvable : {src}")
        sys.exit(1)

    if len(sys.argv) >= 3:
        dst = sys.argv[2]
    else:
        base, _ = os.path.splitext(src)
        dst = base + '_parcinfo.csv'

    print(f"Source  : {src}")
    print(f"Cible   : {dst}")
    print()

    try:
        nb_ok, nb_skipped, warnings = convert(src, dst)
    except ValueError as e:
        print(f"Erreur : {e}")
        sys.exit(1)

    print(f"OK  {nb_ok} appareil(s) converti(s)")
    if nb_skipped:
        print(f"/!\\ {nb_skipped} ligne(s) ignoree(s) (pas de nom ni d'IP)")
    if warnings:
        print()
        print("Avertissements :")
        for w in warnings:
            print(f"  • {w}")

    print()
    print(f"Fichier prêt à importer dans ParcInfo : {dst}")


if __name__ == '__main__':
    main()
