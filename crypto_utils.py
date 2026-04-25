"""
Module de chiffrement pour identifiants et données sensibles.
Utilise Fernet (symétrique, sécurisé, compatible).
"""

import os
import base64
import logging
from hashlib import sha256
from cryptography.fernet import Fernet

logger = logging.getLogger('parcinfo')

class CryptoManager:
    """Gestionnaire de chiffrement pour les données sensibles."""

    def __init__(self, secret_key_file='secret.key'):
        """
        Initialise le gestionnaire crypto.

        Args:
            secret_key_file: Chemin vers le fichier contenant la clé secrète
        """
        self.secret_key_file = secret_key_file
        self.cipher = self._init_cipher()

    def _init_cipher(self):
        """Initialise ou charge la clé de chiffrement Fernet."""
        try:
            # Charger la clé secrète depuis le fichier
            if not os.path.exists(self.secret_key_file):
                logger.warning(f"Fichier clé secrète non trouvé: {self.secret_key_file}")
                return None

            with open(self.secret_key_file, 'r') as f:
                secret_key = f.read().strip()

            # Dériver une clé Fernet à partir de la clé secrète
            # Utiliser SHA256 + base64 pour créer une clé de 32 bytes
            # Salt fixe mais sûr car Fernet ajoute son propre IV aléatoire
            hash_input = secret_key + 'parcinfo_cipher_salt'
            derived_key = sha256(hash_input.encode()).digest()  # 32 bytes

            # Encoder en base64 pour Fernet (doit être 44 caractères en base64)
            fernet_key = base64.urlsafe_b64encode(derived_key)

            # Créer le cipher Fernet
            cipher = Fernet(fernet_key)
            return cipher

        except Exception as e:
            logger.error(f"Erreur initialisation cipher: {e}")
            return None

    def encrypt(self, plaintext):
        """
        Chiffre un texte en clair.

        Args:
            plaintext (str): Texte à chiffrer

        Returns:
            str: Texte chiffré (base64), ou None si erreur
        """
        if not plaintext:
            return None

        if not self.cipher:
            logger.warning("Cipher non disponible, retour en clair")
            return plaintext

        try:
            # Chiffrer le texte
            encrypted_bytes = self.cipher.encrypt(plaintext.encode())
            # Retourner en base64 string
            encrypted_str = encrypted_bytes.decode()
            return encrypted_str
        except Exception as e:
            logger.error(f"Erreur chiffrement: {e}")
            return None

    def decrypt(self, encrypted_text):
        """
        Déchiffre un texte chiffré.

        Args:
            encrypted_text (str): Texte chiffré (base64)

        Returns:
            str: Texte en clair, ou None si erreur
        """
        if not encrypted_text:
            return None

        if not self.cipher:
            logger.warning("Cipher non disponible, retour en clair")
            return encrypted_text

        try:
            # Déchiffrer le texte
            decrypted_bytes = self.cipher.decrypt(encrypted_text.encode())
            decrypted_str = decrypted_bytes.decode()
            return decrypted_str
        except Exception as e:
            logger.error(f"Erreur déchiffrement: {e}")
            # Retourner le texte original si déchiffrement échoue
            # (peut arriver si le texte n'était pas chiffré)
            return encrypted_text

    def is_encrypted(self, text):
        """
        Vérifie si un texte est chiffré (commence par 'gAAAAAB').

        Args:
            text (str): Texte à vérifier

        Returns:
            bool: True si probablement chiffré, False sinon
        """
        if not text:
            return False
        return text.startswith('gAAAAAB')


# Instance globale du gestionnaire crypto
def get_crypto_manager(secret_key_file='secret.key'):
    """Retourne l'instance du gestionnaire crypto."""
    return CryptoManager(secret_key_file)


# Fonctions de commodité
def encrypt_password(password, secret_key_file='secret.key'):
    """Chiffre un mot de passe."""
    if not password:
        return None
    crypto = get_crypto_manager(secret_key_file)
    return crypto.encrypt(password)


def decrypt_password(encrypted_password, secret_key_file='secret.key'):
    """Déchiffre un mot de passe."""
    if not encrypted_password:
        return None
    crypto = get_crypto_manager(secret_key_file)
    return crypto.decrypt(encrypted_password)
