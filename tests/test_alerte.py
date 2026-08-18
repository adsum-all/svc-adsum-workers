"""La veille : prévenir un humain quand une file cesse d'avancer.

Sans elle, l'observabilité ne sert qu'à qui regarde, et personne ne regarde à trois
heures du matin. Le premier signal est alors la réclamation d'un client dont le
module payé n'est jamais arrivé, plusieurs jours plus tard.

Ce qui est éprouvé, et pourquoi chaque point compte.

Une alerte qui se répète noie le canal, fait couper les notifications, et rend muette
la prochaine, celle qui comptait.

Une alerte sans retour à la normale laisse un incident ouvert dans la tête de tout le
monde : quelqu'un se déplace pour rien, ou ne se déplace pas alors qu'il le fallait.

Un canal d'alerte en panne ne doit pas arrêter les files. C'est un service externe de
plus, et le jour où il tombe, le travail doit continuer.

Et une alerte doit dire ce qui ne se passe plus pour les clients, pas seulement le
nom d'une tâche que personne ne se rappelle à trois heures du matin.
"""
from __future__ import annotations

import pytest

from ouvriers.alerte import SEUIL_ECHECS, CanalPasserelle, Veilleur, incidents


class CanalObserve:
    """Note les alertes portées. Peut refuser, pour éprouver la panne du canal."""

    def __init__(self, en_panne: bool = False) -> None:
        self.portees: list[tuple[str, str, str]] = []
        self.en_panne = en_panne

    def alerter(self, titre: str, corps: str, gravite: str) -> None:
        if self.en_panne:
            raise RuntimeError("canal d'alerte injoignable")
        self.portees.append((titre, corps, gravite))


class BoucleFigee:
    """Une boucle dont on décide l'instantané, sans rien faire tourner."""

    def __init__(self, etat: dict) -> None:
        self._etat = etat

    def instantane(self) -> dict:
        return self._etat


def etat(taches: list[dict] | None = None, **extra) -> dict:
    return {
        "en_marche": True,
        "arret_definitif": None,
        "taches": taches if taches is not None else [],
        **extra,
    }


def tache(nom: str, echecs: int = 0, resultat: str = "succès") -> dict:
    return {"tache": nom, "passages": 10, "succes": 10 - echecs,
            "echecs_consecutifs": echecs, "dernier_resultat": resultat,
            "dernier_compte_rendu": {}}


class TestLecture:
    def test_une_boucle_saine_n_alerte_sur_rien(self):
        assert incidents(etat([tache("deploiements"), tache("envois")])) == []

    def test_un_echec_isole_n_alerte_pas(self):
        """Une coupure réseau se rattrape par le recul exponentiel. Réveiller
        quelqu'un pour cela apprend à ignorer les alertes."""
        assert incidents(etat([tache("envois", echecs=SEUIL_ECHECS - 1)])) == []

    def test_des_echecs_repetes_alertent(self):
        trouves = incidents(etat([tache("deploiements", echecs=SEUIL_ECHECS,
                                        resultat="EchecTache : 503")]))
        assert len(trouves) == 1
        assert trouves[0].gravite == "majeur"
        assert "deploiements" in trouves[0].titre

    def test_l_alerte_dit_ce_que_la_panne_coute(self):
        """Nommer la tâche ne suffit pas : celui qui lit à trois heures du matin ne
        se rappelle pas ce qu'elle fait."""
        trouve = incidents(etat([tache("deploiements", echecs=SEUIL_ECHECS)]))[0]
        assert "payé" in trouve.corps
        assert "attend" in trouve.corps

        trouve = incidents(etat([tache("suspensions", echecs=SEUIL_ECHECS)]))[0]
        assert "sans être payé" in trouve.corps

    def test_un_arret_definitif_est_critique(self):
        trouves = incidents(etat(arret_definitif="secret refusé par le commerce"))
        assert len(trouves) == 1
        assert trouves[0].gravite == "critique"
        assert "ne repartira pas seul" in trouves[0].corps

    def test_un_arret_definitif_n_est_pas_double_par_les_files(self):
        """Quatre alertes pour un seul incident font perdre du temps à qui les lit."""
        trouves = incidents(etat(
            [tache("deploiements", echecs=50), tache("envois", echecs=50)],
            arret_definitif="secret refusé"))
        assert len(trouves) == 1

    def test_une_boucle_a_l_arret_est_signalee(self):
        trouves = incidents(etat(en_marche=False))
        assert any(i.cle == "boucle-arretee" for i in trouves)


class TestVeille:
    def test_une_alerte_part_une_seule_fois(self):
        """Répéter toutes les trente secondes noie le canal et fait couper les
        notifications, ce qui rend muette la prochaine alerte."""
        canal = CanalObserve()
        veilleur = Veilleur(canal)
        boucle = BoucleFigee(etat([tache("envois", echecs=SEUIL_ECHECS)]))

        for _ in range(5):
            veilleur.examiner(boucle)

        assert len(canal.portees) == 1

    def test_le_retour_a_la_normale_est_annonce(self):
        canal = CanalObserve()
        veilleur = Veilleur(canal)

        veilleur.examiner(BoucleFigee(etat([tache("envois", echecs=SEUIL_ECHECS)])))
        assert len(canal.portees) == 1

        veilleur.examiner(BoucleFigee(etat([tache("envois", echecs=0)])))
        assert len(canal.portees) == 2
        assert "Retour à la normale" in canal.portees[1][0]

    def test_un_incident_qui_revient_realerte(self):
        """Refermé puis rouvert, c'est un nouvel incident : il doit se signaler."""
        canal = CanalObserve()
        veilleur = Veilleur(canal)
        en_panne = BoucleFigee(etat([tache("envois", echecs=SEUIL_ECHECS)]))
        saine = BoucleFigee(etat([tache("envois", echecs=0)]))

        veilleur.examiner(en_panne)
        veilleur.examiner(saine)
        veilleur.examiner(en_panne)

        titres = [t for t, _, _ in canal.portees]
        assert titres.count("La file « envois » n'avance plus") == 2

    def test_deux_files_en_panne_donnent_deux_alertes(self):
        """Elles appellent des gestes différents, et l'une peut se régler sans
        l'autre."""
        canal = CanalObserve()
        Veilleur(canal).examiner(BoucleFigee(etat([
            tache("deploiements", echecs=SEUIL_ECHECS),
            tache("relances", echecs=SEUIL_ECHECS)])))
        assert len(canal.portees) == 2

    def test_un_canal_en_panne_ne_fait_pas_tomber_la_veille(self):
        """Le jour où le canal d'alerte tombe, les files doivent continuer."""
        canal = CanalObserve(en_panne=True)
        veilleur = Veilleur(canal)
        partis = veilleur.examiner(
            BoucleFigee(etat([tache("envois", echecs=SEUIL_ECHECS)])))
        assert partis == []
        assert veilleur.ouverts == set(), "L'incident reste ouvert pour être retenté"

    def test_un_incident_non_porte_est_retente_au_passage_suivant(self):
        canal = CanalObserve(en_panne=True)
        veilleur = Veilleur(canal)
        boucle = BoucleFigee(etat([tache("envois", echecs=SEUIL_ECHECS)]))

        veilleur.examiner(boucle)
        canal.en_panne = False
        veilleur.examiner(boucle)

        assert len(canal.portees) == 1


class TestCanalPasserelle:
    class Reponse:
        def __init__(self, code: int) -> None:
            self.code = code
            self.corps = b"{}"

    class TransportObserve:
        def __init__(self, code: int = 200) -> None:
            self.appels: list[dict] = []
            self.code = code

        def envoyer(self, methode, url, entetes, corps=None, delai=30.0):
            import json as _json

            self.appels.append({"url": url, "entetes": entetes,
                                "corps": _json.loads(corps.decode())})
            return TestCanalPasserelle.Reponse(self.code)

    def test_passe_par_la_passerelle_avec_une_cle_stable(self):
        """La clé rend l'alerte idempotente : deux ordonnanceurs qui constatent le
        même incident n'écrivent qu'un message."""
        t = self.TransportObserve()
        canal = CanalPasserelle("https://passerelle.test", "secret", "12345",
                                transport=t)
        canal.alerter("Titre", "Corps", "critique")
        canal.alerter("Titre", "Corps", "critique")

        cles = {a["corps"]["cle_idempotence"] for a in t.appels}
        assert len(cles) == 1
        assert t.appels[0]["url"] == "https://passerelle.test/api/v1/envois"
        assert t.appels[0]["entetes"]["Authorization"] == "Bearer secret"
        assert t.appels[0]["corps"]["categorie"] == "alerte"

    def test_deux_incidents_differents_ont_des_cles_differentes(self):
        t = self.TransportObserve()
        canal = CanalPasserelle("https://passerelle.test", "secret", "12345",
                                transport=t)
        canal.alerter("Un", "Corps", "majeur")
        canal.alerter("Deux", "Corps", "majeur")
        assert len({a["corps"]["cle_idempotence"] for a in t.appels}) == 2

    def test_un_refus_de_la_passerelle_leve(self):
        """C'est ce qui permet au veilleur de garder l'incident ouvert et de
        réessayer plutôt que de le croire signalé."""
        canal = CanalPasserelle("https://passerelle.test", "secret", "12345",
                                transport=self.TransportObserve(code=502))
        with pytest.raises(RuntimeError):
            canal.alerter("Titre", "Corps", "critique")

    def test_refuse_de_se_construire_sans_destinataire(self):
        """Sans destinataire, les alertes partent dans le vide et l'exploitation se
        croit surveillée."""
        with pytest.raises(ValueError):
            CanalPasserelle("https://passerelle.test", "secret", "")
