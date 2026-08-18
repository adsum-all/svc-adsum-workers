"""Quand une tâche doit repasser, et ce qu'il faut faire quand elle échoue.

Le calcul du prochain passage est isolé ici, sans horloge ni sommeil, pour une raison
simple : une politique de reprise qui ne s'éprouve qu'en attendant vraiment ne
s'éprouve jamais. Un plafond de backoff mal posé se découvre en production, un
dimanche, quand une file s'est arrêtée pendant six heures.

Quatre règles, chacune contre une panne observée ailleurs.

**Le backoff est exponentiel et plafonné.** Sans exponentielle, une base indisponible
reçoit une requête par seconde et met plus longtemps à se relever. Sans plafond,
douze échecs de suite portent l'attente à plus d'une journée, et la file ne repart
jamais d'elle-même une fois le service rétabli.

**La gigue est proportionnelle et déterministe.** Sans elle, deux instances démarrées
par le même déploiement frappent à la même seconde, indéfiniment. Elle est tirée
d'une graine passée en argument plutôt que d'un tirage libre, sinon le comportement
n'est pas reproductible d'un essai à l'autre.

**Un succès remet le compteur à zéro.** Garder l'attente longue après le
rétablissement fait traîner la reprise sans aucun bénéfice.

**Le temps est monotone.** Un ajustement d'horloge par le service de temps, même de
quelques secondes, ferait sauter un passage ou en déclencherait une rafale si les
échéances étaient calculées sur l'heure murale.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cadence:
    """À quel rythme une tâche repasse, et comment elle recule quand elle échoue."""

    nom: str
    #: Le chemin de la tâche chez le service appelé. Déclaré ici pour qu'ajouter une
    #: tâche ne demande de toucher qu'une table.
    chemin: str
    #: Secondes entre deux passages réussis.
    intervalle_s: float
    #: Plafond de l'attente après échecs. Au-delà, la file ne repartirait pas seule.
    plafond_s: float = 900.0
    #: Part de l'intervalle tirée au hasard, pour désynchroniser deux instances.
    #: Un dixième suffit : plus décalerait visiblement les passages.
    gigue: float = 0.1

    def __post_init__(self) -> None:
        if self.intervalle_s <= 0:
            raise ValueError(
                f"« {self.nom} » : un intervalle nul ou négatif ferait tourner la "
                "boucle sans jamais rendre la main.")
        if self.plafond_s < self.intervalle_s:
            raise ValueError(
                f"« {self.nom} » : un plafond plus court que l'intervalle rendrait "
                "le recul plus fréquent que le rythme normal.")
        if not 0.0 <= self.gigue < 1.0:
            raise ValueError(f"« {self.nom} » : la gigue vaut entre zéro et un.")


def attente(cadence: Cadence, echecs: int, graine: float) -> float:
    """Combien de secondes attendre avant le prochain passage.

    ``echecs`` est le nombre d'échecs consécutifs, zéro après un succès. ``graine``
    est un nombre entre zéro et un, fourni par l'appelant : la fonction reste ainsi
    pure et son résultat vérifiable.
    """
    if echecs <= 0:
        base = cadence.intervalle_s
    else:
        # Doublement à chaque échec, borné avant le calcul pour qu'un compteur qui
        # s'emballe ne produise pas un nombre trop grand pour être manipulé.
        facteur = 2 ** min(echecs, 20)
        base = min(cadence.intervalle_s * facteur, cadence.plafond_s)

    if cadence.gigue <= 0:
        return base
    # Gigue centrée : autant en avance qu'en retard. Une gigue uniquement positive
    # décalerait lentement tous les passages vers la droite, tour après tour.
    ecart = base * cadence.gigue
    return max(0.5, base - ecart + 2 * ecart * _borne(graine))


def _borne(valeur: float) -> float:
    return min(1.0, max(0.0, valeur))


#: Les tâches du service commerce et leur rythme.
#:
#: Ces valeurs remplacent les crons journaliers de l'hébergeur, qui sont la vraie
#: raison d'être de ce service : un déploiement payé à neuf heures cinq attendait
#: jusqu'au lendemain, parce que la formule d'hébergement ne permet qu'un passage
#: par jour. Ici la file avance en continu.
TACHES_COMMERCE = (
    # Le plus visible pour le client : il vient de payer et attend son module.
    Cadence("deploiements", "/api/v1/taches/deploiements", intervalle_s=30.0,
            plafond_s=600.0),
    # La boîte d'envoi. Une relance écrite mais non postée ne sert à rien, et le
    # motif de séparation entre écriture et envoi est justement de pouvoir rejouer.
    Cadence("envois", "/api/v1/taches/envois", intervalle_s=60.0, plafond_s=900.0),
    # Les relances d'impayé se calculent sur des échéances en jours : les passer
    # toutes les minutes ne trouverait rien de plus.
    Cadence("relances", "/api/v1/taches/relances", intervalle_s=3600.0,
            plafond_s=7200.0),
    # Une suspension ferme les droits d'un client. Espacée volontairement : rien
    # n'urge à couper une heure plus tôt, et une erreur y coûte cher.
    Cadence("suspensions", "/api/v1/taches/suspensions", intervalle_s=3600.0,
            plafond_s=7200.0),
    # La durée de conservation. Une fois par jour : la donnée la plus récente à
    # effacer a un an, et une politique que personne ne déclenche n'est pas une
    # politique. Espacée aussi parce qu'elle efface : ce qui efface se déclenche
    # rarement et se lit dans un journal.
    Cadence("retention", "/api/v1/taches/retention", intervalle_s=86400.0,
            plafond_s=86400.0),
)


#: Les tâches de la passerelle. Une seule pour l'instant, et elle n'est pas
#: cosmétique : sans quelqu'un pour la déclencher, la durée de conservation du
#: registre n'existe que dans une note et le registre grossit indéfiniment.
TACHES_PASSERELLE = (
    # Une fois par jour suffit largement : la donnée la plus récente à effacer a
    # treize mois. Plus souvent ne ferait que relire une table pour rien.
    Cadence("purge", "/api/v1/taches/purge", intervalle_s=86400.0,
            plafond_s=86400.0),
)
