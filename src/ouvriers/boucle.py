"""La boucle : ce qui fait avancer les files, en continu.

Ce service existe pour une raison précise. Les tâches du commerce sont exposées en
HTTP et déclenchées par les crons de l'hébergeur, qui n'en permet qu'un par jour dans
la formule retenue. Un module payé à neuf heures cinq attendait donc le lendemain
pour être déployé. Ici, la file avance toutes les trente secondes.

Cinq décisions de conduite, chacune contre une panne qui se paie cher.

**Une tâche ne se recouvre jamais elle-même.** Chaque tâche a son fil, qui appelle,
attend le résultat, puis dort. Deux passages simultanés de la même tâche se
disputeraient les mêmes lignes ; le service appelé s'en protège par des baux, mais
compter là-dessus reviendrait à envoyer sciemment du travail perdu.

**Les tâches différentes avancent en parallèle.** Une relance lente ne doit pas
retarder un déploiement : ce sont des files sans rapport entre elles.

**L'arrêt est propre.** Sur signal, la tâche en cours va à son terme et aucune
nouvelle ne démarre. Tuer au milieu d'un appel laisserait un bail posé chez le
commerce, qui se libérera tout seul à son échéance, mais ferait perdre le tour.

**L'attente est interruptible.** Un fil qui dort une heure ne répondrait pas à un
ordre d'arrêt avant une heure. L'attente passe donc par un événement, réveillé par
l'arrêt.

**Le temps est monotone.** Un ajustement d'horloge par le service de temps ferait
sauter un passage, ou en déclencherait une rafale.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .cadence import Cadence, attente

JOURNAL = logging.getLogger("ouvriers")


class ArretDefinitif(Exception):
    """Une panne que le temps ne répare pas : secret refusé, route disparue.

    Une famille à part parce que la conduite est l'inverse de celle d'un échec
    ordinaire. Reculer puis réessayer sur un secret faux fait croire à une panne
    passagère alors que les files sont arrêtées pour de bon, et le seul signal est
    une ligne d'avertissement toutes les quinze minutes. La boucle s'arrête donc, et
    le processus sort avec un code que le superviseur voit.
    """


class Executeur(Protocol):
    """Ce qui fait réellement le travail d'une tâche."""

    def executer(self, cadence: Cadence) -> dict:
        """Rend le compte rendu de la tâche. Lève en cas d'échec."""
        ...


@dataclass
class Compteur:
    """L'état d'une tâche, lisible de l'extérieur pendant que la boucle tourne."""

    nom: str
    passages: int = 0
    succes: int = 0
    echecs_consecutifs: int = 0
    dernier_resultat: str = "jamais exécutée"
    dernier_passage_s: float | None = None
    #: Le dernier compte rendu du service appelé. Sert à voir ce que la file a fait,
    #: pas seulement qu'elle a tourné.
    dernier_compte_rendu: dict = field(default_factory=dict)

    def instantane(self) -> dict:
        return {
            "tache": self.nom,
            "passages": self.passages,
            "succes": self.succes,
            "echecs_consecutifs": self.echecs_consecutifs,
            "dernier_resultat": self.dernier_resultat,
            "dernier_compte_rendu": self.dernier_compte_rendu,
        }


class Boucle:
    """Un fil par tâche, tous arrêtables ensemble."""

    def __init__(
        self,
        executeur: Executeur,
        cadences: tuple[Cadence, ...],
        graine: Callable[[], float] | None = None,
        horloge: Callable[[], float] = time.monotonic,
    ) -> None:
        if not cadences:
            raise ValueError(
                "Une boucle sans tâche tournerait sans rien faire. Mieux vaut ne pas "
                "démarrer que laisser croire que les files avancent.")
        self._executeur = executeur
        self._cadences = cadences
        self._horloge = horloge
        # Une graine injectable pour que la gigue soit reproductible dans un test.
        # Par défaut, un tirage réel : c'est ce qui désynchronise deux instances.
        self._graine = graine or _tirage
        self._arret = threading.Event()
        self._fils: list[threading.Thread] = []
        self.compteurs: dict[str, Compteur] = {
            c.nom: Compteur(c.nom) for c in cadences}
        #: Renseigné quand une panne définitive a arrêté la boucle. Lu par le
        #: processus principal pour choisir son code de sortie.
        self.arret_definitif: str | None = None

    # -- Conduite ------------------------------------------------------------

    def demarrer(self) -> None:
        """Lancer un fil par tâche. Ne bloque pas."""
        if self._fils:
            raise RuntimeError("Cette boucle tourne déjà.")
        self._arret.clear()
        for cadence in self._cadences:
            fil = threading.Thread(
                target=self._tourner, args=(cadence,),
                name=f"ouvrier-{cadence.nom}", daemon=True)
            fil.start()
            self._fils.append(fil)
        JOURNAL.info("Boucle démarrée sur %d tâches", len(self._cadences))

    def arreter(self, delai_s: float = 30.0) -> bool:
        """Demander l'arrêt et attendre la fin des tâches en cours.

        Rend vrai si tous les fils se sont arrêtés dans le délai. Faux veut dire
        qu'une tâche est encore chez le service appelé : le processus peut sortir,
        le bail posé là-bas se libérera de lui-même à son échéance.
        """
        self._arret.set()
        limite = self._horloge() + delai_s
        for fil in self._fils:
            restant = max(0.0, limite - self._horloge())
            fil.join(timeout=restant)
        vivants = [f.name for f in self._fils if f.is_alive()]
        self._fils = []
        if vivants:
            JOURNAL.warning("Tâches encore en cours à l'arrêt : %s", ", ".join(vivants))
            return False
        JOURNAL.info("Boucle arrêtée proprement")
        return True

    def demander_arret(self) -> None:
        """Armer l'arrêt sans rien attendre.

        Séparé de la méthode d'arrêt parce qu'un gestionnaire de signal ne peut pas
        joindre des fils : il n'a le droit que de poser un drapeau, et c'est le
        processus principal qui fait le reste en sortant de son attente.
        """
        self._arret.set()

    def patienter(self, secondes: float) -> None:
        """Attendre au plus ce délai, interrompu par l'ordre d'arrêt.

        Ce dont le fil principal a besoin pour passer en veille à intervalle
        régulier sans devenir sourd à une demande d'arrêt pendant ce temps.
        """
        self._arret.wait(timeout=secondes)

    def attendre(self) -> None:
        """Bloquer jusqu'à l'ordre d'arrêt. Ce que fait le processus principal."""
        while not self._arret.is_set():
            # Réveil régulier plutôt qu'attente infinie : sous Windows, une attente
            # sans délai n'est pas interrompue par un signal, et le service ne
            # répondrait alors jamais à une demande d'arrêt.
            self._arret.wait(timeout=1.0)

    @property
    def arretee(self) -> bool:
        return self._arret.is_set()

    def instantane(self) -> dict:
        """L'état des files, lisible pendant que la boucle tourne."""
        return {
            "en_marche": bool(self._fils) and not self._arret.is_set(),
            "arret_definitif": self.arret_definitif,
            "taches": [c.instantane() for c in self.compteurs.values()],
        }

    # -- Interne -------------------------------------------------------------

    def _tourner(self, cadence: Cadence) -> None:
        compteur = self.compteurs[cadence.nom]
        while not self._arret.is_set():
            self._un_passage(cadence, compteur)
            if self._arret.is_set():
                return
            # L'attente passe par l'événement : un sommeil sec ne répondrait à un
            # ordre d'arrêt qu'après une heure sur les tâches lentes.
            self._arret.wait(timeout=attente(
                cadence, compteur.echecs_consecutifs, self._graine()))

    def _un_passage(self, cadence: Cadence, compteur: Compteur) -> None:
        debut = self._horloge()
        compteur.passages += 1
        try:
            compte_rendu = self._executeur.executer(cadence)
        except ArretDefinitif as e:
            # Un secret faux ou une route disparue ne se règlent pas en attendant.
            # Traités comme une panne passagère, ils arrêtaient les quatre files pour
            # de bon, avec pour seul signal une ligne d'avertissement toutes les
            # quinze minutes : personne ne s'en apercevait avant qu'un client
            # réclame. La boucle s'arrête donc, bruyamment, et le processus sort avec
            # un code d'erreur que le superviseur voit.
            compteur.echecs_consecutifs += 1
            compteur.dernier_resultat = f"arrêt définitif : {e}"[:300]
            self.arret_definitif = compteur.dernier_resultat
            JOURNAL.error(
                "Tâche %s : %s. Les files s'arrêtent, une intervention est "
                "nécessaire.", cadence.nom, e)
            self._arret.set()
        except Exception as e:  # noqa: BLE001
            # Large volontairement : un fil qui meurt sur une exception inattendue
            # arrête sa file en silence jusqu'au prochain redémarrage, et personne
            # ne s'en aperçoit avant la réclamation d'un client.
            compteur.echecs_consecutifs += 1
            # Le message du commerce peut nommer une organisation ou une facture.
            # Le type dit ce qu'il faut faire ; le détail appartient aux traces du
            # service qui l'a produit.
            compteur.dernier_resultat = f"{type(e).__name__} : {str(e)[:120]}"
            JOURNAL.warning(
                "Tâche %s en échec (%d d'affilée) : %s",
                cadence.nom, compteur.echecs_consecutifs, compteur.dernier_resultat)
        else:
            compteur.succes += 1
            compteur.echecs_consecutifs = 0
            compteur.dernier_resultat = "succès"
            # Seuls les compteurs sont retenus. Le compte rendu complet porte les
            # organisations suspendues, les numéros de facture et les identifiants
            # de déploiement : l'ordonnanceur n'en a aucun usage, et ses traces
            # vivent ailleurs que celles du commerce, avec d'autres lecteurs.
            chiffres = _chiffres(compte_rendu)
            compteur.dernier_compte_rendu = chiffres
            if any(v > 0 for v in chiffres.values()):
                JOURNAL.info("Tâche %s : %s", cadence.nom, chiffres)
        finally:
            compteur.dernier_passage_s = self._horloge() - debut


def _chiffres(compte_rendu: dict) -> dict[str, int]:
    """Ce que l'ordonnanceur retient d'un compte rendu : combien, jamais lesquels.

    Les listes et les objets du compte rendu nomment des organisations, des
    factures et des déploiements. Ils appartiennent au service commerce, qui les
    trace déjà chez lui pour ses propres lecteurs. Ici, seule la quantité importe :
    elle suffit à dire si la file avance, et à ne journaliser que les passages
    utiles. Une tâche qui trouve une file vide toutes les trente secondes produit
    sinon deux mille huit cents lignes par jour, dans lesquelles la seule qui
    compte se perd.
    """
    retenu: dict[str, int] = {}
    for cle, valeur in compte_rendu.items():
        if isinstance(valeur, bool):
            retenu[cle] = int(valeur)
        elif isinstance(valeur, (int, float)):
            retenu[cle] = int(valeur)
        elif isinstance(valeur, (list, dict, str)):
            retenu[cle] = len(valeur)
    return retenu


def _tirage() -> float:
    import random

    return random.random()
