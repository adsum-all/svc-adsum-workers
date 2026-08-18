"""Prévenir un humain quand une file cesse d'avancer.

L'observabilité dit ce qui se passe à qui regarde. Personne ne regarde à trois heures
du matin, et c'est précisément le moment où une file s'arrête. Sans alerte, le
premier signal est la réclamation d'un client dont le module payé n'est jamais
arrivé, plusieurs jours plus tard.

Quatre règles, chacune contre une façon connue de rendre une alerte inutile.

**On n'alerte que sur ce qui appelle un geste.** Une alerte qu'on ne peut pas
traiter apprend à ignorer les alertes, et la suivante, celle qui comptait, passe
inaperçue. Trois situations seulement : les files sont arrêtées pour de bon, une file
échoue depuis assez longtemps pour que ce ne soit plus un incident passager, ou le
service appelé ne répond plus du tout.

**Une alerte ne se répète pas.** Un même incident qui écrit toutes les trente
secondes noie le canal et fait couper les notifications. Une situation alerte une
fois, et une seule, tant qu'elle dure.

**Le retour à la normale est annoncé.** Sans lui, personne ne sait si l'incident est
clos, et quelqu'un se déplace pour rien, ou pire, ne se déplace pas.

**Une alerte qui échoue ne fait pas tomber la boucle.** Le canal d'alerte est un
service externe de plus : le jour où il est en panne, les files doivent continuer
d'avancer. Un échec d'alerte est journalisé, jamais propagé.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .boucle import Boucle

JOURNAL = logging.getLogger("ouvriers.alerte")

#: Échecs consécutifs sur une tâche avant d'alerter. En deçà, c'est une coupure
#: réseau : le recul exponentiel la rattrape sans qu'on réveille personne. Au-delà,
#: la file recule déjà de plusieurs minutes et n'avance plus.
SEUIL_ECHECS = 5


class Canal(Protocol):
    """Ce qui sait porter une alerte à un humain."""

    def alerter(self, titre: str, corps: str, gravite: str) -> None:
        """Lève en cas d'échec ; l'appelant journalise et continue."""
        ...


@dataclass(frozen=True)
class Incident:
    """Une situation qui appelle un geste, et son identité stable.

    La clé sert à ne pas répéter : deux constats de la même situation portent la
    même clé, et le second ne réalerte pas.
    """

    cle: str
    titre: str
    corps: str
    gravite: str


def incidents(etat: dict[str, Any]) -> list[Incident]:
    """Lire l'état de la boucle et en tirer ce qui mérite un appel.

    Fonction pure sur l'instantané, sans horloge ni réseau : c'est ce qui la rend
    vérifiable. Une politique d'alerte qui ne s'éprouve qu'en attendant une vraie
    panne ne s'éprouve jamais.
    """
    trouves: list[Incident] = []

    if etat.get("arret_definitif"):
        trouves.append(Incident(
            cle="arret-definitif",
            titre="Les files ADSUM sont arrêtées",
            corps=(
                f"L'ordonnanceur s'est arrêté sur une panne que le temps ne répare "
                f"pas : {etat['arret_definitif']}\n\n"
                "Plus aucun module payé n'est déployé, plus aucune relance ne part. "
                "Une intervention est nécessaire ; le service ne repartira pas seul."
            ),
            gravite="critique",
        ))
        # Rien d'autre n'a d'intérêt : toutes les files sont mortes de la même cause,
        # et quatre alertes pour un incident font perdre du temps à qui les lit.
        return trouves

    for tache in etat.get("taches", []):
        echecs = tache.get("echecs_consecutifs", 0)
        if echecs >= SEUIL_ECHECS:
            trouves.append(Incident(
                cle=f"echecs:{tache['tache']}",
                titre=f"La file « {tache['tache']} » n'avance plus",
                corps=(
                    f"{echecs} échecs consécutifs.\n"
                    f"Dernier résultat : {tache.get('dernier_resultat', 'inconnu')}\n\n"
                    f"{_consequence(tache['tache'])}"
                ),
                gravite="majeur",
            ))

    if not etat.get("en_marche", True):
        trouves.append(Incident(
            cle="boucle-arretee",
            titre="L'ordonnanceur ADSUM ne tourne plus",
            corps="La boucle n'est plus en marche et aucune file n'avance.",
            gravite="critique",
        ))

    return trouves


def _consequence(tache: str) -> str:
    """Ce que cette file en panne coûte, en clair.

    Une alerte qui nomme seulement la tâche oblige celui qui la reçoit à se souvenir
    de ce qu'elle fait, à trois heures du matin. Elle doit dire ce qui ne se passe
    plus pour les clients.
    """
    return {
        "deploiements": "Les modules payés ne sont plus mis en place. Un client qui "
                        "vient de régler attend devant un écran qui ne change pas.",
        "envois": "Les messages écrits restent en boîte : aucune relance ne part.",
        "relances": "Les impayés ne sont plus relancés. Aucun client n'est suspendu "
                    "à tort, mais rien n'est réclamé non plus.",
        "suspensions": "Les impayés dont le délai est écoulé ne sont plus suspendus. "
                       "Le service continue d'être rendu sans être payé.",
    }.get(tache, "Cette file n'avance plus.")


@dataclass
class Veilleur:
    """Constate, alerte une fois, et annonce le retour à la normale."""

    canal: Canal
    #: Le registre, quand il y en a un. Le canal réveille quelqu'un, le registre
    #: garde la trace : sans lui, personne ne peut dire une semaine plus tard
    #: combien de fois la panne s'est produite ni si elle a été traitée.
    registre: Any = None
    #: Les clés d'incident déjà signalées et encore ouvertes.
    ouverts: set[str] = field(default_factory=set)

    def examiner(self, boucle: Boucle) -> list[str]:
        """Un passage de veille. Rend les clés sur lesquelles une alerte est partie.

        Appelé par le processus principal à intervalle régulier, jamais par un fil de
        tâche : une tâche qui alerte sur elle-même n'alerte plus quand elle est
        justement bloquée.
        """
        etat = boucle.instantane()
        courants = {i.cle: i for i in incidents(etat)}
        partis: list[str] = []

        for cle, incident in courants.items():
            if cle in self.ouverts:
                # Déjà signalé et toujours vrai. Répéter noierait le canal et ferait
                # couper les notifications, ce qui rend muette la prochaine alerte.
                continue
            self._inscrire(incident)
            if self._porter(incident.titre, incident.corps, incident.gravite):
                self.ouverts.add(cle)
                partis.append(cle)

        for cle in sorted(self.ouverts - set(courants)):
            # Sans cette annonce, personne ne sait si l'incident est clos : quelqu'un
            # se déplace pour rien, ou ne se déplace pas alors qu'il le fallait.
            self._radier(cle)
            if self._porter(
                    "Retour à la normale", f"L'incident « {cle} » est terminé.",
                    "information"):
                self.ouverts.discard(cle)

        return partis

    def _inscrire(self, incident: Incident) -> None:
        """Écrire au registre. Un échec ici n'empêche jamais l'alerte de partir."""
        if self.registre is None:
            return
        try:
            self.registre.ouvrir(
                incident.cle, incident.titre, incident.corps, incident.gravite)
        except Exception as e:  # noqa: BLE001
            JOURNAL.error("Incident « %s » non inscrit : %s",
                          incident.cle, type(e).__name__)

    def _radier(self, cle: str) -> None:
        if self.registre is None:
            return
        try:
            self.registre.fermer(cle)
        except Exception as e:  # noqa: BLE001
            JOURNAL.error("Incident « %s » non fermé au registre : %s",
                          cle, type(e).__name__)

    def _porter(self, titre: str, corps: str, gravite: str) -> bool:
        try:
            self.canal.alerter(titre, corps, gravite)
        except Exception as e:  # noqa: BLE001
            # Le canal d'alerte est un service externe de plus. Le jour où il tombe,
            # les files doivent continuer d'avancer : l'échec se journalise et ne se
            # propage pas. L'incident reste ouvert et sera retenté au passage suivant.
            JOURNAL.error("Alerte « %s » non portée : %s", titre, type(e).__name__)
            return False
        return True


class CanalRegistre:
    """Écrire l'incident dans le registre de l'éditeur, en plus de l'annoncer.

    Le canal seul ne suffit pas. Un message part, quelqu'un le lit ou non, et une
    semaine plus tard personne ne peut dire combien de fois la file s'est arrêtée ni
    si l'incident de mardi a été traité. Le registre garde la trace, le canal réveille
    quelqu'un : les deux servent, et aucun ne remplace l'autre.

    La clé de l'incident est celle du veilleur, donc la même situation avance un
    compteur au lieu d'ouvrir une seconde ligne.
    """

    def __init__(self, url_commerce: str, secret: str, transport: Any = None,
                 delai_s: float = 20.0) -> None:
        if not url_commerce or not secret:
            raise ValueError(
                "Le registre d'incidents exige l'adresse du service commerce et le "
                "secret d'ordonnanceur.")
        self._url = url_commerce.rstrip("/")
        self._secret = secret
        self._transport = transport
        self._delai = delai_s

    def ouvrir(self, cle: str, titre: str, corps: str, gravite: str) -> None:
        self._appeler("POST", "/api/v1/commerce/incidents", {
            "cle": cle, "titre": titre, "detail": corps,
            "gravite": gravite, "source": "ordonnanceur",
        })

    def fermer(self, cle: str) -> None:
        """Le retour à la normale, constaté par la plateforme."""
        import urllib.parse

        self._appeler(
            "DELETE",
            f"/api/v1/commerce/incidents/{urllib.parse.quote(cle, safe='')}", None)

    def _appeler(self, methode: str, chemin: str, charge: dict | None) -> None:
        corps = json.dumps(charge).encode("utf-8") if charge is not None else None
        entetes = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._secret}"}
        url = f"{self._url}{chemin}"

        if self._transport is not None:
            reponse = self._transport.envoyer(methode, url, entetes, corps, self._delai)
            if reponse.code >= 400:
                raise RuntimeError(f"Registre d'incidents : {reponse.code}")
            return

        import httpx

        try:
            reponse = httpx.request(methode, url, content=corps, headers=entetes,
                                    timeout=self._delai, follow_redirects=False)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Registre d'incidents injoignable : {type(e).__name__}") from None
        if reponse.status_code >= 400:
            raise RuntimeError(f"Registre d'incidents : {reponse.status_code}")


class CanalPasserelle:
    """Porter l'alerte par la passerelle, sur le canal choisi.

    La passerelle plutôt qu'un envoi direct : elle porte déjà l'identité de
    l'éditeur, sa déduplication et son registre. Une alerte envoyée à côté d'elle
    serait le seul message de la plateforme dont personne ne garde trace.
    """

    def __init__(self, url_passerelle: str, secret: str, destinataire: str,
                 canal: str = "telegram", transport: Any = None,
                 delai_s: float = 20.0) -> None:
        if not all((url_passerelle, secret, destinataire)):
            raise ValueError(
                "Le canal d'alerte exige l'adresse de la passerelle, le secret "
                "partagé et un destinataire. Sans destinataire, les alertes partent "
                "dans le vide et l'exploitation se croit surveillée.")
        self._url = url_passerelle.rstrip("/")
        self._secret = secret
        self._destinataire = destinataire
        self._canal = canal
        self._transport = transport
        self._delai = delai_s

    def alerter(self, titre: str, corps: str, gravite: str) -> None:
        import hashlib

        # La clé rend l'alerte idempotente côté passerelle : deux processus
        # d'ordonnancement qui constatent le même incident n'écrivent qu'un message.
        # Elle ne porte pas d'horodatage, sinon chaque tentative en créerait un autre.
        empreinte = hashlib.sha256(f"{titre}|{corps}".encode()).hexdigest()[:32]
        charge = json.dumps({
            "canal": self._canal,
            "adresse": self._destinataire,
            "objet": f"[ADSUM {gravite}] {titre}",
            "corps": corps,
            "cle_idempotence": f"alerte-{empreinte}",
            "categorie": "alerte",
        }).encode("utf-8")

        entetes = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._secret}"}
        url = f"{self._url}/api/v1/envois"

        if self._transport is not None:
            reponse = self._transport.envoyer("POST", url, entetes, charge, self._delai)
            if reponse.code >= 400:
                raise RuntimeError(f"Passerelle : {reponse.code}")
            return

        import urllib.error
        import urllib.request

        requete = urllib.request.Request(url, data=charge, headers=entetes,
                                         method="POST")
        try:
            with urllib.request.urlopen(requete, timeout=self._delai) as reponse:
                if reponse.status >= 400:
                    raise RuntimeError(f"Passerelle : {reponse.status}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Passerelle : {e.code}") from None
        except OSError as e:
            raise RuntimeError(f"Passerelle injoignable : {type(e).__name__}") from None
