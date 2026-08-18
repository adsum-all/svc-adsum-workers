"""Le point d'entrée du service d'ordonnancement.

Un processus qui tourne, et non une fonction déclenchée par un cron d'hébergeur.
C'est toute la raison d'être de ce service : la formule d'hébergement du reste de la
plateforme ne permet qu'un déclenchement par jour, ce qui faisait attendre au
lendemain un module payé le matin.

Le processus se pose sur n'importe quoi qui sait faire tourner du Python en continu :
une machine de l'exploitant, un conteneur ailleurs, un service géré. Il n'a besoin
d'aucune base : tout l'état vit chez le service commerce.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from types import FrameType

from .alerte import CanalPasserelle, CanalRegistre, Veilleur
from .boucle import Boucle
from .cadence import TACHES_COMMERCE, TACHES_PASSERELLE
from .commerce import ExecuteurCommerce


def configurer_journal() -> None:
    """Une trace lisible, sans jamais de secret.

    Le niveau vient de l'environnement pour qu'un incident se diagnostique sans
    redéployer, et le format porte l'heure : sans elle, une trace ne sert à rien
    pour reconstituer un enchaînement.
    """
    logging.basicConfig(
        level=os.environ.get("ADSUM_JOURNAL_NIVEAU", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )


def construire() -> Boucle:
    url = os.environ.get("ADSUM_COMMERCE_URL", "")
    secret = os.environ.get("ADSUM_CRON_SECRET", "")
    manquants = [nom for nom, valeur in
                 (("ADSUM_COMMERCE_URL", url), ("ADSUM_CRON_SECRET", secret))
                 if not valeur]
    if manquants:
        raise RuntimeError(
            f"Variables requises absentes : {', '.join(manquants)}. "
            "L'ordonnanceur refuse de démarrer plutôt que de tourner à vide en "
            "laissant croire que les files avancent."
        )
    return Boucle(ExecuteurCommerce(url, secret), TACHES_COMMERCE)


def construire_passerelle() -> Boucle | None:
    """La boucle des tâches de la passerelle, si elle est configurée.

    Une boucle séparée et non des tâches ajoutées à la première : les deux services
    ont des adresses et des secrets différents, et une panne de la passerelle ne
    doit pas faire reculer la file des déploiements, qui n'a rien à voir.
    """
    url = os.environ.get("ADSUM_PASSERELLE_URL", "")
    secret = os.environ.get("ADSUM_PASSERELLE_SECRET", "")
    if not url or not secret:
        return None
    return Boucle(ExecuteurCommerce(url, secret), TACHES_PASSERELLE)


def construire_veilleur() -> Veilleur | None:
    """Le veilleur, si un canal d'alerte est configuré.

    Optionnel à dessein : un ordonnanceur qui refuserait de démarrer faute de canal
    d'alerte laisserait les files à l'arrêt pour une raison qui n'empêche rien
    d'avancer. Mais l'absence est dite au démarrage, sinon l'exploitation se croit
    surveillée alors que personne ne sera prévenu.
    """
    passerelle = os.environ.get("ADSUM_PASSERELLE_URL", "")
    secret = os.environ.get("ADSUM_PASSERELLE_SECRET", "")
    destinataire = os.environ.get("ADSUM_ALERTE_DESTINATAIRE", "")
    canal = os.environ.get("ADSUM_ALERTE_CANAL", "telegram")
    if not all((passerelle, secret, destinataire)):
        return None

    # Le registre, quand le commerce est joignable. Le canal réveille quelqu'un, le
    # registre garde la trace : sans lui, personne ne peut dire une semaine plus tard
    # combien de fois la panne s'est produite ni si elle a été traitée.
    commerce = os.environ.get("ADSUM_COMMERCE_URL", "")
    cron = os.environ.get("ADSUM_CRON_SECRET", "")
    registre = CanalRegistre(commerce, cron) if commerce and cron else None

    return Veilleur(CanalPasserelle(passerelle, secret, destinataire, canal), registre)


#: Entre deux passages de veille. Assez court pour qu'un incident soit signalé dans
#: la minute, assez long pour que la veille elle-même ne pèse rien.
VEILLE_S = 30.0


def main() -> int:
    configurer_journal()
    journal = logging.getLogger("ouvriers")
    boucle = construire()

    def sur_signal(numero: int, _: FrameType | None) -> None:
        # On ne fait qu'armer l'arrêt : arrêter la boucle depuis un gestionnaire de
        # signal la ferait joindre des fils depuis un contexte où c'est interdit.
        journal.info("Signal %s reçu, arrêt demandé", numero)
        boucle.demander_arret()

    for numero in (signal.SIGINT, signal.SIGTERM):
        signal.signal(numero, sur_signal)

    veilleur = construire_veilleur()
    if veilleur is None:
        journal.warning(
            "Aucun canal d'alerte configuré : une file arrêtée ne préviendra "
            "personne. Posez ADSUM_PASSERELLE_URL, ADSUM_PASSERELLE_SECRET et "
            "ADSUM_ALERTE_DESTINATAIRE.")

    passerelle = construire_passerelle()
    if passerelle is None:
        journal.warning(
            "Passerelle non configurée : sa purge de conservation ne sera pas "
            "déclenchée, et son registre grossira sans limite.")

    boucle.demarrer()
    if passerelle is not None:
        passerelle.demarrer()

    # La veille tourne dans le fil principal, pas dans un fil de tâche : une tâche
    # qui alerte sur elle-même n'alerte plus quand elle est justement bloquée.
    while not boucle.arretee:
        boucle.patienter(VEILLE_S)
        if veilleur is not None:
            veilleur.examiner(boucle)

    propre = boucle.arreter()
    if passerelle is not None:
        propre = passerelle.arreter() and propre

    if boucle.arret_definitif is not None:
        # Code distinct de l'arrêt sale : un superviseur doit pouvoir séparer
        # « relance-moi » de « viens voir, la configuration est fausse ».
        journal.error("Arrêt sur panne définitive : %s", boucle.arret_definitif)
        return 2
    return 0 if propre else 1


if __name__ == "__main__":
    sys.exit(main())
