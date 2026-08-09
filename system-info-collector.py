#!/usr/bin/env python3
"""
ParcInfo System Information Collector

Petit script autonome qui collecte les informations système et les envoie à ParcInfo.
Fonctionne sur Windows, macOS et Linux.

Toute la logique de collecte vit dans `collector_core.py`, partagé avec le
collecteur GUI ; ce script ne contient que l'interface en ligne de commande.

Utilisation :
    python system-info-collector.py [--server http://parcinfo.local:3456] [--token ABC123]

Ou directement dans ParcInfo :
    1. Ouvrir l'interface web
    2. Cliquer sur le lien "Télécharger collecteur" depuis la page Inventaire
    3. Exécuter le script
    4. Les infos système s'ajoutent automatiquement à la machine correspondante
"""

import argparse
import sys

from collector_core import (
    COLLECTOR_VERSION,
    build_summary_lines,
    collect_system_info,
    console_progress,
    fetch_clients,
    generate_pdf_report,
    is_elevated,
    send_to_parcinfo,
    upload_report_to_parcinfo,
)


def _forcer_sortie_utf8():
    """Rend la console capable d'afficher les caractères non-ASCII.

    Une console Windows utilise cp1252 : le premier « ⚠ » ou « ✓ » affiché y
    lève un UnicodeEncodeError qui interrompt le collecteur avant même la
    collecte. Reconfigurer la sortie en UTF-8 avec repli évite d'avoir à
    appauvrir tous les messages.
    """
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            # Python < 3.7 ou flux déjà encapsulé : sans objet, on continue
            pass


def main():
    parser = argparse.ArgumentParser(
        description='Collecte les infos système et les envoie à ParcInfo'
    )
    parser.add_argument('--server', default='http://parcinfo.local:3456',
                        help='URL du serveur ParcInfo (défaut: http://parcinfo.local:3456)')
    parser.add_argument('--token', default=None,
                        help='Token d\'authentification (optionnel)')
    parser.add_argument('--client-id', type=int, default=None,
                        help='ID du client cible (ex: --client-id 5)')
    parser.add_argument('--client-name', default=None,
                        help='Nom du client cible (ex: --client-name "Mon Entreprise")')
    parser.add_argument('--no-send', action='store_true',
                        help='Collecte et génère le rapport sans rien envoyer au serveur')
    parser.add_argument('--quiet', action='store_true',
                        help='Mode silencieux (pas d\'affichage)')

    # Désactivé par défaut : le test consomme de la bande passante sur un poste
    # de production et sollicite un service tiers. La latence, elle, est
    # toujours mesurée et suffit à diagnostiquer une lenteur réseau.
    parser.add_argument('--test-debit', action='store_true',
                        help='Mesurer aussi le débit descendant (télécharge ~10 Mo)')

    args = parser.parse_args()
    _forcer_sortie_utf8()

    if not args.quiet:
        print("=" * 78)
        print(f"ParcInfo System Information Collector v{COLLECTOR_VERSION}")
        print("=" * 78)
        if not is_elevated():
            print("\n⚠ Exécution sans privilèges administrateur : SMART détaillé, TPM,")
            print("  BitLocker et clé OEM risquent d'être absents du rapport.")
        print("\n[*] Collecte des informations système...")

    # Barre de progression : la collecte dure une bonne minute et resterait
    # sinon indiscernable d'un blocage. En mode --quiet, aucun rappel.
    info = collect_system_info(progress=None if args.quiet else console_progress(),
                               test_debit=args.test_debit)

    if not args.quiet:
        print()
        for line in build_summary_lines(info):
            print(line)

    # Générer rapport PDF
    if not args.quiet:
        print("[*] Génération du rapport PDF...")

    pdf_content, report_file = generate_pdf_report(info, args.client_id, args.client_name)
    if report_file and not args.quiet:
        print(f"    ✓ Rapport sauvegardé: {report_file}")

    if args.no_send:
        if not args.quiet:
            print("\n[*] --no-send : aucun envoi au serveur.")
        return 0

    # Envoyer à ParcInfo
    if not args.quiet:
        print(f"\n[*] Envoi à {args.server}...")
        if args.client_id:
            print(f"    Client ID: {args.client_id}")
        elif args.client_name:
            print(f"    Client: {args.client_name}")
        else:
            print("    ⚠️ Aucun client spécifié - le serveur utilisera le client par défaut")

    # Reconnaissance automatique : sans client explicite, demander au serveur
    # s'il connaît déjà cette machine par son adresse MAC. Évite d'atterrir
    # dans « Découverte réseau » alors que l'appareil est déjà inventorié.
    client_id = args.client_id
    if not client_id and not args.client_name:
        _clients, suggestion = fetch_clients(args.server, mac_address=info.get('mac_address'),
                                            token=args.token)
        if suggestion:
            client_id = suggestion.get('id')
            if not args.quiet:
                print(f"    Client reconnu d'après l'adresse MAC : "
                      f"{suggestion.get('nom')} (ID {client_id})")

    success, result = send_to_parcinfo(info, args.server, args.token,
                                       client_id=client_id,
                                       client_name=args.client_name)

    if not success:
        if not args.quiet:
            print(f"    ✗ Erreur: {result}")
        return 1

    if not args.quiet:
        print("    ✓ Succès!")
        print("\n[+] Appareil enregistré:")
        print(f"    ID: {result.get('device_id')}")
        print(f"    Hostname: {result.get('hostname')}")
        print(f"    IP: {result.get('ip_address')}")
        print(f"    MAC: {result.get('mac_address')}")
        if result.get('peripherals_created'):
            print(f"    Périphériques créés: {result.get('peripherals_created')}")

    # Envoyer le rapport PDF en tant que document joint
    device_id = result.get('device_id')
    client_id = client_id or result.get('client_id')
    if report_file and device_id and client_id:
        if not args.quiet:
            print("\n[*] Envoi du rapport vers les documents de l'appareil...")

        success_report, result_report = upload_report_to_parcinfo(
            pdf_content, report_file, args.server, device_id, client_id,
            token=args.token
        )

        if not args.quiet:
            if success_report:
                print("    ✓ Rapport joint enregistré")
                print(f"    Document ID: {result_report.get('document_id')}")
            else:
                print(f"    ⚠️ Erreur lors du stockage du rapport: {result_report}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
