"""L'ordonnanceur : cadence, reprise, arrêt propre, et appel des tâches.

Aucun sommeil réel. Une politique de reprise qui ne s'éprouve qu'en attendant
vraiment ne s'éprouve jamais : le calcul du prochain passage est une fonction pure,
et c'est elle qui est vérifiée, pas la patience du test.

La boucle, elle, tourne pour de bon, sur des fils réels et des cadences en
millisecondes. Ce qui est éprouvé, dans l'ordre de ce que cela coûte quand cela
casse.

Une file qui s'arrête en silence. C'est le pire des cas : personne ne le voit avant
la réclamation d'un client dont le module payé n'est jamais arrivé. Une exception
inattendue ne doit donc jamais tuer un fil.

Une file qui ne repart pas après une panne. Le recul est plafonné pour cela.

Une tâche qui se recouvre elle-même, ou un arrêt qui coupe au milieu d'un appel.
"""
from __future__ import annotations

import threading
import time

import pytest

from ouvriers.boucle import Boucle
from ouvriers.cadence import TACHES_COMMERCE, Cadence, attente
from ouvriers.commerce import AppelRefuse, EchecTache, ExecuteurCommerce


class Reponse:
    def __init__(self, code: int, corps: bytes = b"{}") -> None:
        self.code = code
        self.corps = corps


class TransportObserve:
    """Note les appels, répond ce qu'on lui a dit. Aucun réseau."""

    def __init__(self, reponses: dict[str, Reponse] | None = None,
                 defaut: Reponse | None = None) -> None:
        self.appels: list[dict] = []
        self.reponses = reponses or {}
        self.defaut = defaut or Reponse(200, b'{"traites": 0}')

    def envoyer(self, methode, url, entetes, corps=None, delai=30.0):
        self.appels.append({"methode": methode, "url": url, "entetes": entetes})
        for motif, reponse in self.reponses.items():
            if motif in url:
                return reponse
        return self.defaut


# ------------------------------------------------------------------ Cadence


class TestCadence:
    def test_une_cadence_sans_echec_garde_son_rythme(self):
        c = Cadence("t", "/t", intervalle_s=30.0, gigue=0.0)
        assert attente(c, 0, 0.5) == 30.0

    def test_le_recul_double_a_chaque_echec(self):
        """Sans exponentielle, une base indisponible reçoit une requête par seconde
        et met plus longtemps à se relever."""
        c = Cadence("t", "/t", intervalle_s=10.0, plafond_s=10_000.0, gigue=0.0)
        assert [attente(c, n, 0.5) for n in (1, 2, 3, 4)] == [20.0, 40.0, 80.0, 160.0]

    def test_le_recul_est_plafonne(self):
        """Sans plafond, douze échecs portent l'attente au-delà d'une journée et la
        file ne repart jamais d'elle-même une fois le service rétabli."""
        c = Cadence("t", "/t", intervalle_s=30.0, plafond_s=600.0, gigue=0.0)
        assert attente(c, 20, 0.5) == 600.0
        assert attente(c, 200, 0.5) == 600.0

    def test_la_gigue_desynchronise_sans_deriver(self):
        """Deux instances démarrées ensemble frapperaient à la même seconde.

        La gigue est centrée : autant en avance qu'en retard. Une gigue uniquement
        positive décalerait lentement tous les passages, tour après tour.
        """
        c = Cadence("t", "/t", intervalle_s=100.0, gigue=0.1)
        assert attente(c, 0, 0.0) == pytest.approx(90.0)
        assert attente(c, 0, 1.0) == pytest.approx(110.0)
        assert attente(c, 0, 0.5) == pytest.approx(100.0)

    def test_l_attente_ne_descend_jamais_sous_une_demi_seconde(self):
        """Une attente nulle ferait tourner la boucle sans rendre la main."""
        c = Cadence("t", "/t", intervalle_s=0.6, gigue=0.9)
        assert attente(c, 0, 0.0) >= 0.5

    def test_une_cadence_incoherente_est_refusee_a_la_construction(self):
        with pytest.raises(ValueError):
            Cadence("t", "/t", intervalle_s=0)
        with pytest.raises(ValueError):
            Cadence("t", "/t", intervalle_s=600.0, plafond_s=30.0)

    def test_les_taches_du_commerce_sont_declarees_de_facon_coherente(self):
        noms = [c.nom for c in TACHES_COMMERCE]
        assert noms == ["deploiements", "envois", "relances", "suspensions",
                        "retention"]
        # La rétention efface : elle se déclenche rarement, et son passage se lit
        # dans un journal. Une cadence courte sur une tâche qui supprime est une
        # mauvaise idée même quand elle est idempotente.
        retention = next(c for c in TACHES_COMMERCE if c.nom == "retention")
        assert retention.intervalle_s >= 86400.0
        # Le déploiement est le plus visible pour le client : il vient de payer.
        deploiements = next(c for c in TACHES_COMMERCE if c.nom == "deploiements")
        assert deploiements.intervalle_s <= 60.0
        assert len(set(noms)) == len(noms), "Deux tâches de même nom écraseraient leur compteur"


# ----------------------------------------------------------------- Exécuteur


class TestExecuteur:
    def test_presente_le_secret_et_appelle_la_bonne_route(self):
        t = TransportObserve()
        ExecuteurCommerce("https://commerce.test/", "secret-cron", t).executer(
            TACHES_COMMERCE[0])
        appel = t.appels[0]
        assert appel["url"] == "https://commerce.test/api/v1/taches/deploiements"
        assert appel["entetes"]["Authorization"] == "Bearer secret-cron"

    def test_un_secret_refuse_n_est_pas_une_panne_passagere(self):
        """Reculer sur un secret faux ferait croire à une panne alors que la file
        est arrêtée pour de bon."""
        t = TransportObserve(defaut=Reponse(401, b'{"detail": "non authentifie"}'))
        with pytest.raises(AppelRefuse) as e:
            ExecuteurCommerce("https://commerce.test", "faux", t).executer(
                TACHES_COMMERCE[0])
        assert "secret" in str(e.value).lower()

    def test_une_route_inconnue_le_dit(self):
        t = TransportObserve(defaut=Reponse(404, b"{}"))
        with pytest.raises(AppelRefuse):
            ExecuteurCommerce("https://commerce.test", "s", t).executer(
                TACHES_COMMERCE[0])

    def test_une_erreur_serveur_est_reessayable(self):
        t = TransportObserve(defaut=Reponse(503, b'{"detail": "indisponible"}'))
        with pytest.raises(EchecTache) as e:
            ExecuteurCommerce("https://commerce.test", "s", t).executer(
                TACHES_COMMERCE[0])
        assert not isinstance(e.value, AppelRefuse)

    def test_rend_le_compte_rendu_de_la_tache(self):
        t = TransportObserve(defaut=Reponse(200, b'{"traites": 3, "echecs": 0}'))
        rendu = ExecuteurCommerce("https://commerce.test", "s", t).executer(
            TACHES_COMMERCE[0])
        assert rendu == {"traites": 3, "echecs": 0}

    def test_refuse_de_se_construire_sans_adresse_ni_secret(self):
        with pytest.raises(ValueError):
            ExecuteurCommerce("", "secret")
        with pytest.raises(ValueError):
            ExecuteurCommerce("https://commerce.test", "")

    def test_la_sonde_n_envoie_pas_le_secret(self):
        """La sonde du commerce est ouverte : y présenter le secret l'exposerait
        sans aucun besoin."""
        t = TransportObserve(defaut=Reponse(200, b'{"service": "commerce"}'))
        assert ExecuteurCommerce("https://commerce.test", "s", t).sonde() is True
        assert "Authorization" not in t.appels[0]["entetes"]


# -------------------------------------------------------------------- Boucle


class ExecuteurPilote:
    """Un exécuteur dont on décide le comportement, tâche par tâche."""

    def __init__(self) -> None:
        self.passages: dict[str, int] = {}
        self.leve: Exception | None = None
        self.duree_s = 0.0
        self.simultanes = 0
        self.max_simultanes = 0
        self._verrou = threading.Lock()

    def executer(self, cadence: Cadence) -> dict:
        with self._verrou:
            self.passages[cadence.nom] = self.passages.get(cadence.nom, 0) + 1
            self.simultanes += 1
            self.max_simultanes = max(self.max_simultanes, self.simultanes)
        try:
            if self.duree_s:
                time.sleep(self.duree_s)
            if self.leve is not None:
                raise self.leve
            return {"traites": 1}
        finally:
            with self._verrou:
                self.simultanes -= 1


def rapide(nom: str = "rapide") -> Cadence:
    return Cadence(nom, f"/{nom}", intervalle_s=0.05, plafond_s=0.2, gigue=0.0)


def attendre_que(condition, limite_s: float = 5.0) -> bool:
    """Attendre qu'une condition devienne vraie, sans dormir un temps fixe."""
    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.01)
    return False


class TestBoucle:
    def test_une_tache_repasse_tant_que_la_boucle_tourne(self):
        pilote = ExecuteurPilote()
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.5)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: pilote.passages.get("rapide", 0) >= 3)
        finally:
            boucle.arreter(delai_s=5)

    def test_une_tache_ne_se_recouvre_jamais_elle_meme(self):
        """Deux passages simultanés de la même tâche se disputeraient les mêmes
        lignes : le service appelé s'en protège, mais ce serait du travail perdu."""
        pilote = ExecuteurPilote()
        pilote.duree_s = 0.08
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.5)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: pilote.passages.get("rapide", 0) >= 3)
        finally:
            boucle.arreter(delai_s=5)
        assert pilote.max_simultanes == 1

    def test_des_taches_differentes_avancent_en_parallele(self):
        """Une relance lente ne doit pas retarder un déploiement."""
        pilote = ExecuteurPilote()
        pilote.duree_s = 0.05
        boucle = Boucle(pilote, (rapide("a"), rapide("b")), graine=lambda: 0.5)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: pilote.max_simultanes >= 2)
        finally:
            boucle.arreter(delai_s=5)

    def test_une_exception_inattendue_ne_tue_pas_le_fil(self):
        """Le pire des cas : la file s'arrête en silence, et personne ne le voit
        avant la réclamation d'un client."""
        pilote = ExecuteurPilote()
        pilote.leve = RuntimeError("panne inattendue")
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.0)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: pilote.passages.get("rapide", 0) >= 3)
            compteur = boucle.compteurs["rapide"]
            assert compteur.echecs_consecutifs >= 2
            assert "panne inattendue" in compteur.dernier_resultat
        finally:
            boucle.arreter(delai_s=5)

    def test_un_succes_remet_le_compteur_d_echecs_a_zero(self):
        """Garder l'attente longue après le rétablissement fait traîner la reprise."""
        pilote = ExecuteurPilote()
        pilote.leve = EchecTache("indisponible")
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.0)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: boucle.compteurs["rapide"].echecs_consecutifs >= 2)
            pilote.leve = None
            assert attendre_que(
                lambda: boucle.compteurs["rapide"].echecs_consecutifs == 0)
            assert boucle.compteurs["rapide"].dernier_resultat == "succès"
        finally:
            boucle.arreter(delai_s=5)

    def test_l_arret_laisse_la_tache_en_cours_aller_a_son_terme(self):
        """Couper au milieu laisserait un bail posé chez le service appelé."""
        pilote = ExecuteurPilote()
        pilote.duree_s = 0.15
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.5)
        boucle.demarrer()
        assert attendre_que(lambda: pilote.passages.get("rapide", 0) >= 1)
        assert boucle.arreter(delai_s=5) is True
        assert pilote.simultanes == 0, "Aucune tâche ne doit rester en cours"

    def test_l_arret_interrompt_une_longue_attente(self):
        """Un fil qui dort une heure ne répondrait à un arrêt qu'après une heure."""
        pilote = ExecuteurPilote()
        lente = Cadence("lente", "/lente", intervalle_s=3600.0, plafond_s=7200.0,
                        gigue=0.0)
        boucle = Boucle(pilote, (lente,), graine=lambda: 0.5)
        boucle.demarrer()
        assert attendre_que(lambda: pilote.passages.get("lente", 0) >= 1)
        debut = time.monotonic()
        assert boucle.arreter(delai_s=5) is True
        assert time.monotonic() - debut < 3.0, "L'arrêt ne doit pas attendre le tour"

    def test_l_instantane_dit_ce_que_font_les_files(self):
        pilote = ExecuteurPilote()
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.5)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: boucle.compteurs["rapide"].succes >= 1)
            etat = boucle.instantane()
            assert etat["en_marche"] is True
            tache = etat["taches"][0]
            assert tache["tache"] == "rapide"
            assert tache["dernier_compte_rendu"] == {"traites": 1}
        finally:
            boucle.arreter(delai_s=5)
        assert boucle.instantane()["en_marche"] is False

    def test_une_boucle_sans_tache_est_refusee(self):
        """Mieux vaut ne pas démarrer que laisser croire que les files avancent."""
        with pytest.raises(ValueError):
            Boucle(ExecuteurPilote(), ())

    def test_demarrer_deux_fois_est_refuse(self):
        """Deux jeux de fils sur les mêmes tâches doubleraient chaque passage."""
        boucle = Boucle(ExecuteurPilote(), (rapide(),), graine=lambda: 0.5)
        boucle.demarrer()
        try:
            with pytest.raises(RuntimeError):
                boucle.demarrer()
        finally:
            boucle.arreter(delai_s=5)


class TestArretDefinitif:
    """Un secret faux n'est pas une panne passagère.

    Traité comme telle, il laissait les quatre files mortes en silence : la boucle
    reculait, réessayait, et le seul signal était une ligne d'avertissement toutes
    les quinze minutes. Personne ne s'en apercevait avant qu'un client réclame le
    module qu'il avait payé.
    """

    def test_un_secret_refuse_arrete_la_boucle(self):
        pilote = ExecuteurPilote()
        pilote.leve = AppelRefuse("Le service commerce refuse l'ordonnanceur.")
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.0)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: boucle.arretee, limite_s=5)
            assert boucle.arret_definitif is not None
            assert "refuse" in boucle.arret_definitif.lower()
        finally:
            boucle.arreter(delai_s=5)

    def test_la_boucle_ne_reessaie_pas_indefiniment(self):
        """Un passage, puis l'arrêt. Pas dix mille appels sur un secret mort."""
        pilote = ExecuteurPilote()
        pilote.leve = AppelRefuse("Secret faux")
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.0)
        boucle.demarrer()
        boucle.arreter(delai_s=5)
        assert pilote.passages.get("rapide", 0) == 1

    def test_l_instantane_porte_la_cause(self):
        """C'est ce que lit un exploitant, ou une sonde, pour savoir quoi corriger."""
        pilote = ExecuteurPilote()
        pilote.leve = AppelRefuse("La tâche « envois » n'existe pas")
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.0)
        boucle.demarrer()
        boucle.arreter(delai_s=5)
        etat = boucle.instantane()
        assert etat["arret_definitif"] is not None
        assert "envois" in etat["arret_definitif"]

    def test_un_echec_ordinaire_n_arrete_pas_la_boucle(self):
        """La distinction doit tenir dans les deux sens : une panne passagère
        continue de reculer et de réessayer."""
        pilote = ExecuteurPilote()
        pilote.leve = EchecTache("Service indisponible")
        boucle = Boucle(pilote, (rapide(),), graine=lambda: 0.0)
        boucle.demarrer()
        try:
            assert attendre_que(lambda: pilote.passages.get("rapide", 0) >= 3)
            assert boucle.arret_definitif is None
            assert not boucle.arretee
        finally:
            boucle.arreter(delai_s=5)
