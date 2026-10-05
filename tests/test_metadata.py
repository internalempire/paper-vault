"""Verifiche su src/metadata.py.

Nessun test tocca la rete: CrossRef e PubMed sono simulati. Quello che conta qui non è che le
API rispondano, ma che quando rispondono male — o non rispondono — la nota esca comunque, con
i campi vuoti dichiarati invece che riempiti a caso (HANDOVER §4).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import metadata  # noqa: E402
from src.metadata import (  # noqa: E402
    Metadati,
    recupera,
    slug,
    titolo_riconosciuto,
    trova_doi,
)

CROSSREF_COMPLETO = {
    "message": {
        "author": [{"family": "Palmer", "given": "Biff F."}, {"family": "Clegg", "given": "Deborah J."}],
        "title": ["Pathophysiology of Ketoacidosis: Core Curriculum 2026"],
        "container-title": ["American Journal of Kidney Diseases"],
        "short-container-title": ["Am J Kidney Dis"],
        "published-print": {"date-parts": [[2026, 8]]},
        "volume": "88", "issue": "2", "page": "301-314",
        "DOI": "10.1053/j.ajkd.2026.02.646",
    }
}
PUBMED_RICERCA = {"esearchresult": {"idlist": ["40000001"]}}
PUBMED_RIEPILOGO = {
    "result": {
        "40000001": {
            "authors": [
                {"name": "Palmer BF", "authtype": "Author"},
                {"name": "Kidney Foundation", "authtype": "CollectiveName"},
            ],
            "title": "Pathophysiology of Ketoacidosis.",
            "fulljournalname": "American journal of kidney diseases",
            "source": "Am J Kidney Dis",
            "pubdate": "2026 Aug", "volume": "88", "issue": "2", "pages": "301-314",
        }
    }
}


class RispostaFinta:
    def __init__(self, dati, stato=200):
        self._dati, self.status_code = dati, stato

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._dati


@pytest.fixture
def api(monkeypatch):
    """Sostituisce le due API. `chiamate` registra chi è stato interrogato."""
    stato = {"crossref": CROSSREF_COMPLETO, "pubmed_ricerca": PUBMED_RICERCA,
             "pubmed_riepilogo": PUBMED_RIEPILOGO, "chiamate": []}

    def finta_get(url, **kwargs):
        if "crossref" in url:
            stato["chiamate"].append("crossref")
            risposta = stato["crossref"]
        elif "esearch" in url:
            stato["chiamate"].append("pubmed")
            risposta = stato["pubmed_ricerca"]
        else:
            risposta = stato["pubmed_riepilogo"]
        if risposta is None:
            raise requests.ConnectionError("simulata")
        return RispostaFinta(risposta)

    monkeypatch.setattr(metadata.requests, "get", finta_get)
    return stato


# -- DOI ---------------------------------------------------------------------------------


def test_doi_semplice() -> None:
    assert trova_doi("doi:10.1053/j.ajkd.2026.02.646") == "10.1053/j.ajkd.2026.02.646"


def test_punteggiatura_di_contorno_scartata() -> None:
    assert trova_doi("(doi: 10.1161/CIR.0000000000001415).") == "10.1161/CIR.0000000000001415"
    assert trova_doi("vedi https://doi.org/10.3389/fcvm.2025.1731305, in fondo") == \
        "10.3389/fcvm.2025.1731305"


def test_doi_andato_a_capo_nella_barra_laterale() -> None:
    """Sulla prima pagina di Elsevier il DOI si spezza, e in mezzo finisce l'abstract."""
    prima_pagina = "\n".join([
        "Am J Kidney Dis.",
        "doi: 10.1053/",
        "relative insulin deficiency and counterregulatory hormone excess. Other forms",
        "j.ajkd.2026.02.646",
    ])
    assert trova_doi(prima_pagina) == "10.1053/j.ajkd.2026.02.646"


def test_nessun_doi() -> None:
    assert trova_doi("un testo qualunque, 10.5 mg di farmaco") is None


# -- autori e formattazione --------------------------------------------------------------


def test_autore_collettivo_non_scalza_il_primo_firmatario() -> None:
    """Le linee guida di società cominciano con `Writing Committee Members`."""
    autori = metadata._autori_crossref([
        {"name": "Writing Committee Members"},
        {"family": "Creager", "given": "Mark A."},
        {"family": "Barnes", "given": "Geoffrey D."},
    ])
    assert autori == ["Creager MA", "Barnes GD"]


def test_autore_collettivo_tenuto_se_non_c_e_nessun_altro() -> None:
    assert metadata._autori_crossref([{"name": "GBD 2021 Collaborators"}]) == \
        ["GBD 2021 Collaborators"]


def test_abbreviazione_senza_punti() -> None:
    assert metadata._abbreviazione("Front. Cardiovasc. Med.", "Frontiers in Cardiovascular Medicine") == \
        "Front Cardiovasc Med"


def test_abbreviazione_finta_scartata() -> None:
    """CrossRef a volte ripete il nome esteso: non è un'abbreviazione, si cerca altrove."""
    assert metadata._abbreviazione("American Journal of Kidney Diseases",
                                   "American Journal of Kidney Diseases") is None


def test_et_al_non_raddoppia_il_punto() -> None:
    m = Metadati(autori=[f"Autore{i} A" for i in range(20)], titolo="T", rivista_abbreviata="R",
                 anno=2026, volume="1", pagine="1-2", doi="10.1/x")
    assert ", et al. **T.**" in m.citazione()
    assert "et al.." not in m.citazione()


# -- recupero ----------------------------------------------------------------------------


def test_crossref_completo_non_interroga_pubmed(api) -> None:
    m = recupera("10.1053/j.ajkd.2026.02.646", mailto="x@y.z")
    assert api["chiamate"] == ["crossref"]
    assert m.autori_brevi == "Palmer BF, Clegg DJ"
    assert m.rivista_abbreviata == "Am J Kidney Dis"
    assert not m.mancanti


def test_pubmed_completa_i_buchi_di_crossref(api) -> None:
    """A CrossRef mancano spesso le pagine delle linee guida e le abbreviazioni delle riviste."""
    api["crossref"] = {"message": {**CROSSREF_COMPLETO["message"], "page": None,
                                   "short-container-title": None}}
    m = recupera("10.1053/j.ajkd.2026.02.646")
    assert api["chiamate"] == ["crossref", "pubmed"]
    assert m.pagine == "301-314"
    assert m.rivista_abbreviata == "Am J Kidney Dis"
    # il nome esteso resta quello di CrossRef, scritto con le maiuscole giuste
    assert m.rivista == "American Journal of Kidney Diseases"


def test_crossref_irraggiungibile_si_ripiega_su_pubmed(api) -> None:
    api["crossref"] = None
    m = recupera("10.1053/j.ajkd.2026.02.646")
    assert m.autori == ["Palmer BF"]  # il nome collettivo di PubMed è escluso
    assert m.anno == 2026


def test_entrambe_le_fonti_mute_non_fermano_la_pipeline(api) -> None:
    """Meglio una nota con i metadati da completare che nessuna nota (§6.3)."""
    api["crossref"] = api["pubmed_ricerca"] = None
    m = recupera("10.1053/j.ajkd.2026.02.646")
    assert m.doi == "10.1053/j.ajkd.2026.02.646"
    assert m.mancanti == ["autori", "titolo", "rivista", "anno"]


def test_senza_doi_nessuna_chiamata(api) -> None:
    m = recupera(None)
    assert api["chiamate"] == []
    assert m.mancanti


def test_il_doi_conserva_le_maiuscole_del_documento(api) -> None:
    """CrossRef normalizza a minuscole: `CIR` di Circulation diventerebbe `cir`."""
    api["crossref"] = {"message": {**CROSSREF_COMPLETO["message"],
                                   "DOI": "10.1161/cir.0000000000001415"}}
    assert recupera("10.1161/CIR.0000000000001415").doi == "10.1161/CIR.0000000000001415"


def test_anno_di_stampa_preferito_all_anticipazione_online(api) -> None:
    """Online a dicembre, sul fascicolo a febbraio: per la citazione conta il fascicolo."""
    api["crossref"] = {"message": {**CROSSREF_COMPLETO["message"],
                                   "published-online": {"date-parts": [[2025, 12]]},
                                   "published-print": {"date-parts": [[2026, 2]]}}}
    assert recupera("10.1/x").anno == 2026


# -- slug --------------------------------------------------------------------------------


def test_slug_senza_parole_vuote_e_senza_accenti() -> None:
    m = Metadati(autori=["Aletras G"], anno=2026)
    assert slug(m, "La resistenza diuretica nella sindrome cardiorenale") == \
        "2026-aletras-resistenza-diuretica-sindrome-cardiorenale"


def test_slug_senza_metadati() -> None:
    assert slug(Metadati(), "Fisiopatologia della chetoacidosi") == "fisiopatologia-chetoacidosi"


def test_slug_non_spreca_posti_con_le_paroline() -> None:
    """«resistenza ai diuretici nella sindrome cardiorenale»: `ai` non deve mangiare un posto
    e lasciare fuori `cardiorenale`, che è il sostantivo che identifica il documento."""
    m = Metadati(autori=["Aletras G"], anno=2026)
    assert slug(m, "Resistenza ai diuretici nella sindrome cardiorenale: meccanismi") == \
        "2026-aletras-resistenza-diuretici-sindrome-cardiorenale"


def test_doi_con_trattino_tipografico_normalizzato() -> None:
    """Nei PDF il DOI è composto con trattini non-breaking: così com'è non risolve."""
    assert trova_doi("doi: 10.1136/bmj‑2026-100287") == "10.1136/bmj-2026-100287"


def test_doi_ripulito_dai_caratteri_di_controllo() -> None:
    assert trova_doi("doi: 10.1136/bmj-2026-100287\x08 BMJ") == "10.1136/bmj-2026-100287"


def test_entita_html_sciolte_nei_metadati(api) -> None:
    """CrossRef codifica i campi per il web: `Echo Research &amp; Practice`."""
    api["crossref"] = {"message": {**CROSSREF_COMPLETO["message"],
                                   "container-title": ["Echo Research &amp; Practice"],
                                   "short-container-title": ["Echo Res &amp; Pract"]}}
    m = recupera("10.1/x")
    assert m.rivista == "Echo Research & Practice"
    assert m.rivista_abbreviata == "Echo Res & Pract"


# -- il titolo deve corrispondere al documento -----------------------------------------------


PRIMA_PAGINA = """Am J Kidney Dis.
doi: 10.1053/
Pathophysiology of Ketoacidosis:
Core Curriculum 2026
Biff F. Palmer and Deborah J. Clegg
"""


def test_titolo_riconosciuto_anche_se_spezzato_su_piu_righe() -> None:
    """Il PDF manda a capo il titolo, a volte in mezzo a una parola."""
    assert titolo_riconosciuto("Pathophysiology of Ketoacidosis: Core Curriculum 2026", PRIMA_PAGINA)


def test_titolo_riconosciuto_con_sottotitolo_diverso() -> None:
    """CrossRef a volte omette o aggiunge il sottotitolo: basta la maggior parte delle parole."""
    assert titolo_riconosciuto("Pathophysiology of Ketoacidosis", PRIMA_PAGINA)


def test_titolo_di_un_altro_articolo_segnalato() -> None:
    """Il caso che nessun altro controllo vedrebbe: DOI preso da un articolo citato in testa."""
    assert not titolo_riconosciuto(
        "Diuretic resistance in cardiorenal syndrome: mechanisms and management", PRIMA_PAGINA
    )


def test_senza_titolo_nessun_allarme() -> None:
    """L'assenza del titolo la segnala già `mancanti`: non serve dirlo due volte."""
    assert titolo_riconosciuto(None, PRIMA_PAGINA)


def test_il_doi_della_testata_non_scalza_quello_dell_articolo() -> None:
    """MDPI stampa nel colophon anche il DOI della rivista, e viene prima nell'ordine di lettura."""
    prima_pagina = "\n".join([
        "Review",
        "Point of View: A Holistic Four-Interface Conceptual Model",
        "Journal of Personalized Medicine",
        "https://doi.org/10.3390/jpm",
        "Academic Editor: Someone",
        "https://doi.org/10.3390/jpm15050207",
    ])
    assert trova_doi(prima_pagina) == "10.3390/jpm15050207"


def test_suffisso_senza_cifre_scartato_se_c_e_di_meglio() -> None:
    """Un suffisso senza nemmeno una cifra identifica una rivista o una collana, non un articolo."""
    assert trova_doi("10.1016/collana e poi 10.1016/j.aicoj.2026.100127") == \
        "10.1016/j.aicoj.2026.100127"


def test_un_solo_candidato_resta_tale_anche_senza_cifre() -> None:
    """Meglio un DOI dubbio che nessun DOI: sarà CrossRef a smentirlo, e l'avviso a dirlo."""
    assert trova_doi("vedi 10.1016/collana") == "10.1016/collana"


def test_slug_con_lettere_non_scomponibili() -> None:
    """`ø` non è una `o` con un segno sopra: è una lettera a sé, e la scomposizione non la tocca."""
    m = Metadati(autori=["Søreide K"], anno=2024)
    assert slug(m, "Pancreatite acuta grave") == "2024-soreide-pancreatite-acuta-grave"


def test_slug_con_altre_lettere_intere() -> None:
    for cognome, atteso in (("Ångström A", "angstrom"), ("Straße B", "strasse"),
                            ("Łukasz C", "lukasz")):
        assert slug(Metadati(autori=[cognome], anno=2024), "Titolo") == f"2024-{atteso}-titolo"


def test_acuto_e_cronico_non_sono_parole_vuote() -> None:
    """In medicina la distinzione acuto/cronico identifica il documento quanto l'organo."""
    m = Metadati(autori=["Creager MA"], anno=2026)
    assert slug(m, "Embolia polmonare acuta nell'adulto") == \
        "2026-creager-embolia-polmonare-acuta-adulto"
