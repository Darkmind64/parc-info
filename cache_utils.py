"""
Module de caching pour optimisation performance.
Cache les résultats de requêtes fréquentes avec TTL (Time To Live).
"""

import time
import logging
from functools import wraps
from typing import Any, Callable, Optional, Dict

logger = logging.getLogger('parcinfo')

class CacheManager:
    """Gestionnaire de cache en mémoire avec TTL."""

    def __init__(self, default_ttl=300):
        """
        Initialise le gestionnaire de cache.

        Args:
            default_ttl: TTL par défaut en secondes (5 min)
        """
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        """
        Récupère une valeur du cache.

        Args:
            key: Clé du cache

        Returns:
            Valeur cachée ou None si expirée/inexistante
        """
        if key not in self.cache:
            return None

        entry = self.cache[key]

        # Vérifier si expiré
        if time.time() > entry['expires_at']:
            del self.cache[key]
            return None

        entry['hits'] += 1
        return entry['value']

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """
        Stocke une valeur dans le cache.

        Args:
            key: Clé du cache
            value: Valeur à stocker
            ttl: TTL spécifique (sinon utilise default_ttl)
        """
        ttl = ttl or self.default_ttl
        self.cache[key] = {
            'value': value,
            'expires_at': time.time() + ttl,
            'hits': 0,
            'created_at': time.time()
        }

    def invalidate(self, key: str = None):
        """
        Invalide une clé ou tout le cache.

        Args:
            key: Clé à invalider (None = tout le cache)
        """
        if key:
            if key in self.cache:
                del self.cache[key]
        else:
            self.cache.clear()

    def stats(self) -> Dict[str, Any]:
        """Retourne les statistiques du cache."""
        total_hits = sum(e['hits'] for e in self.cache.values())
        return {
            'entries': len(self.cache),
            'total_hits': total_hits,
            'avg_hits': total_hits / len(self.cache) if self.cache else 0
        }


# Instance globale
_cache_manager = CacheManager(default_ttl=300)  # 5 minutes par défaut


def get_cache_manager() -> CacheManager:
    """Retourne le gestionnaire de cache global."""
    return _cache_manager


def cache_result(ttl: int = 300, key_prefix: str = ''):
    """
    Décorateur pour cacher le résultat d'une fonction.

    Args:
        ttl: Time To Live en secondes
        key_prefix: Préfixe pour la clé de cache
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Construire une clé unique basée sur la fonction et ses arguments
            cache_key = f"{key_prefix}:{func.__name__}:{str(args)}:{str(kwargs)}"

            # Essayer de récupérer du cache
            cached = _cache_manager.get(cache_key)
            if cached is not None:
                logger.debug(f'💾 Cache hit: {cache_key}')
                return cached

            # Exécuter la fonction
            result = func(*args, **kwargs)

            # Stocker en cache
            _cache_manager.set(cache_key, result, ttl)
            logger.debug(f'💾 Cache set: {cache_key}')

            return result

        return wrapper
    return decorator


def invalidate_cache_pattern(pattern: str = ''):
    """
    Invalide toutes les clés correspondant à un pattern.

    Args:
        pattern: Pattern à chercher dans les clés (vide = tout)
    """
    if not pattern:
        _cache_manager.invalidate()
        logger.info('🗑️ Cache entièrement invalidé')
        return

    keys_to_delete = [k for k in _cache_manager.cache.keys() if pattern in k]
    for key in keys_to_delete:
        _cache_manager.invalidate(key)

    if keys_to_delete:
        logger.info(f'🗑️ Cache invalidé: {len(keys_to_delete)} entrées ({pattern})')


# Fonctions de commodité pour les cas courants

def cache_get_liste(list_name: str, ttl: int = 600):
    """Cache pour get_liste() - 10 min par défaut."""
    return f"liste:{list_name}"


def cache_get_clients(ttl: int = 600):
    """Cache pour get_clients() - 10 min par défaut."""
    return "clients_list"


def cache_config(key: str, ttl: int = 900):
    """Cache pour cfg_get() - 15 min par défaut."""
    return f"config:{key}"


# Export
__all__ = [
    'CacheManager',
    'get_cache_manager',
    'cache_result',
    'invalidate_cache_pattern',
]
