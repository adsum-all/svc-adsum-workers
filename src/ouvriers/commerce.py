"""L'exécuteur : appeler les tâches du service commerce.

Les tâches vivent là-bas, avec la base et les règles. Ce service ne les réécrit pas,
il les déclenche : dupliquer ici la logique de suspension d'un impayé donnerait deux
versions de la règle, et l'une des deux serait fausse un jour.

Trois points de conduite.

**Le secret d'ordonnanceur prouve l'appartenance.** Personne n'est derrière ces
appels. La route de tâche du commerce est fermée par ce secret, comparé en temps
constant de son côté ; ici on se contente de le présenter et de ne jamais l'écrire
dans un journal.

**Un refus d'authentification n'est pas réessayable.** Reculer exponentiellement sur
un secret faux ferait croire à une panne passagère alors que la file est arrêtée
pour de bon. Le message le dit, en clair, dès le premier échec.

**Le délai est plus long que la tâche la plus lente.** Une tâche coupée au milieu
laisse un bail posé chez le commerce ; il se libérera seul, mais le tour est perdu et
le travail reprend au passage suivant. Mieux vaut attendre.
"""
from __future__ import annotations

import json
from typing import Any

from .boucle import ArretDefinitif
from .cadence import Cadence


class EchecTache(Exception):
    """La tâche n'a pas abouti. La boucle reculera avant de réessayer."""


class AppelRefuse(EchecTache, ArretDefinitif):
    """Le service a refusé l'appel lui-même : secret faux, route inconnue.

    Une sous-classe distincte parce que la conduite est différente : reculer ne
    servira à rien, et le message doit dire qu'il faut intervenir. Elle hérite
    aussi d'ArretDefinitif, ce qui fait arrêter la boucle : traitée comme une
    panne passagère, elle laissait les quatre files mortes en silence.
    """


class ExecuteurCommerce:
    """Déclenche les tâches du service commerce par HTTP."""

    def __init__(
        self, url_commerce: str, secret: str,
        transport: Any = None, delai_s: float = 120.0,
    ) -> None:
        if not url_commerce or not secret:
            raise ValueError(
                "L'exécuteur exige l'adresse du service commerce et le secret "
                "d'ordonnanceur. Sans eux, les files ne bougent pas et rien ne le dit."
            )
        self._url = url_commerce.rstrip("/")
        self._secret = secret
        self._transport = transport
        self._delai = delai_s

    def executer(self, cadence: Cadence) -> dict:
        code, corps = self._appeler(cadence.chemin)

        if code in (401, 403):
            raise AppelRefuse(
                "Le service commerce refuse l'ordonnanceur. Le secret partagé est "
                "faux ou a été changé d'un seul côté : les files sont arrêtées "
                "jusqu'à ce que quelqu'un le corrige.")
        if code == 404:
            raise AppelRefuse(
                f"La tâche « {cadence.nom} » n'existe pas chez le service commerce "
                f"({cadence.chemin}). Le nom a changé, ou le service déployé est "
                "plus ancien que cet ordonnanceur.")
        if code >= 400:
            raise EchecTache(
                f"Tâche « {cadence.nom} » : le service a répondu {code}. "
                f"{_detail(corps)}")
        return corps

    def sonde(self) -> bool:
        """Le service commerce répond-il ? Sans secret : la sonde y est ouverte."""
        try:
            code, _ = self._appeler("/health", methode="GET", authentifie=False)
        except EchecTache:
            return False
        return 200 <= code < 300

    def _appeler(
        self, chemin: str, methode: str = "POST", authentifie: bool = True,
    ) -> tuple[int, dict]:
        import httpx

        entetes = {"Content-Type": "application/json"}
        if authentifie:
            entetes["Authorization"] = f"Bearer {self._secret}"

        try:
            if self._transport is not None:
                reponse = self._transport.envoyer(
                    methode, f"{self._url}{chemin}", entetes, None, self._delai)
                return reponse.code, _json(reponse.corps)
            brute = httpx.request(
                methode, f"{self._url}{chemin}", headers=entetes, timeout=self._delai)
        except Exception as e:  # noqa: BLE001
            # Le message ne reprend jamais les en-têtes : une trace d'exception de
            # bibliothèque HTTP les inclut volontiers, secret compris.
            raise EchecTache(
                f"Service commerce injoignable : {type(e).__name__}") from None
        return brute.status_code, _json(brute.content)


def _json(corps: bytes) -> dict:
    if not corps:
        return {}
    try:
        charge = json.loads(corps.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return charge if isinstance(charge, dict) else {"resultat": charge}


def _detail(corps: dict) -> str:
    return str(corps.get("detail") or corps.get("message") or "")[:300]
