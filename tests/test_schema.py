"""Verifiche sullo schema del JSON intermedio.

Lo schema ha un solo requisito sostanziale (HANDOVER §12.1): deve coprire tutte le sezioni
presenti nei tre golden output. `test_ogni_sezione_dei_golden_ha_un_campo` è il guardiano di
quel requisito — se un golden cambia struttura, o se lo schema perde un campo, fallisce.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.schema import NotaNonValida, carica_schema, errori, valida  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"
# Solo le note "nude", cioè l'uscita del modello: `salvato-*.json` è l'involucro che la
# pipeline scrive su disco, con dentro anche i metadati CrossRef.
NOTE = sorted(FIXTURES.glob("nota-*.json"))

# Ogni intestazione di secondo livello che compare nei golden, e il campo dello schema che la
# produce. `sezioni` è il contenitore di tutto ciò che è struttura libera del corpo.
SEZIONI_DEI_GOLDEN = {
    "Argomento": "argomento",
    "In breve": "in_breve",
    "Ambito": "ambito",
    "Novità rispetto alle precedenti": "novita",
    "Legenda del sistema di grading": "grading_legenda",
    "Dosaggi e soglie": "dosaggi",
    "Raccomandazioni": "raccomandazioni",
    "Aree di incertezza dichiarate": "aree_incertezza",
    "Riferimento bibliografico": "bibliografia",
}
PREFISSI_DEI_GOLDEN = {
    "Tabella di riferimento rapido": "tabelle",
}

# "Cosa non fare" nasce da campi diversi secondo il tipo di nota: nelle review è la tabella di
# trappole degli autori, nelle linee guida è la vista trasversale sulle raccomandazioni negative.
COSA_NON_FARE = {"narrative review": "trappole", "linea guida": "raccomandazioni"}

# Nelle review la sezione può intitolarsi «Cosa non fare …» oppure «Trappole …»: il prompt
# lascia il titolo al contenuto, e i due golden lo dimostrano.
PREFISSI_TRAPPOLE = ("Cosa non fare", "Trappole")


def _campi_dello_schema() -> set[str]:
    return set(carica_schema()["properties"])


def _titoli_h2(percorso: Path) -> list[str]:
    return re.findall(r"^## (.+)$", percorso.read_text(encoding="utf-8"), flags=re.MULTILINE)


def _tipo(percorso: Path) -> str:
    return re.search(r"^tipo: (.+)$", percorso.read_text(encoding="utf-8"), flags=re.MULTILINE).group(1)


@pytest.mark.parametrize("percorso", NOTE, ids=lambda p: p.stem)
def test_le_note_di_esempio_sono_conformi(percorso: Path) -> None:
    valida(json.loads(percorso.read_text(encoding="utf-8")))


@pytest.mark.parametrize("percorso", sorted(GOLDEN.glob("*.md")), ids=lambda p: p.stem)
def test_ogni_sezione_dei_golden_ha_un_campo(percorso: Path) -> None:
    campi = _campi_dello_schema()
    for titolo in _titoli_h2(percorso):
        atteso = SEZIONI_DEI_GOLDEN.get(titolo)
        if atteso is None and titolo.startswith(PREFISSI_TRAPPOLE):
            atteso = COSA_NON_FARE[_tipo(percorso)]
        if atteso is None:
            atteso = next(
                (c for p, c in PREFISSI_DEI_GOLDEN.items() if titolo.startswith(p)), "sezioni"
            )
        assert atteso in campi, f"'{titolo}' mappa su '{atteso}', che lo schema non ha"


def test_le_tre_forme_di_nota_sono_rappresentate() -> None:
    """review, linea guida e il ripiego 'altro': i tre rami del Passo 1 del prompt."""
    tipi = {json.loads(p.read_text(encoding="utf-8"))["meta"]["tipo"] for p in NOTE}
    assert tipi == {"narrative review", "linea guida", "altro"}


def test_campi_ignoti_rifiutati() -> None:
    """Un campo inventato dal modello deve fermarsi qui, non arrivare al renderer."""
    nota = json.loads((FIXTURES / "nota-minima.json").read_text(encoding="utf-8"))
    nota["cosa_non_fare"] = ["…"]
    with pytest.raises(NotaNonValida):
        valida(nota)


def test_metadati_bibliografici_non_accettati_dal_modello() -> None:
    """Autori, rivista, anno e DOI vengono da CrossRef (HANDOVER §4): non sono campi del modello."""
    nota = json.loads((FIXTURES / "nota-minima.json").read_text(encoding="utf-8"))
    for campo in ("autori", "rivista", "anno", "doi"):
        sporca = {**nota, "meta": {**nota["meta"], campo: "inventato"}}
        assert errori(sporca), f"lo schema accetta meta.{campo}, che il modello non deve produrre"


def test_documento_senza_paginazione_deve_dichiarare_il_criterio() -> None:
    """Senza la riga in testa alla nota, i rimandi '§3.4' non si capiscono (§3 del prompt)."""
    nota = json.loads((FIXTURES / "nota-review.json").read_text(encoding="utf-8"))
    assert nota["meta"]["criterio_riferimenti"] == "sezione"
    nota["meta"]["nota_riferimenti"] = None
    with pytest.raises(NotaNonValida):
        valida(nota)


def test_criterio_riferimenti_limitato_ai_valori_previsti() -> None:
    nota = json.loads((FIXTURES / "nota-minima.json").read_text(encoding="utf-8"))
    nota["meta"]["criterio_riferimenti"] = "paragrafo"
    with pytest.raises(NotaNonValida):
        valida(nota)


def test_raccomandazione_negativa_ha_la_categoria_giusta() -> None:
    """'nessun beneficio' e 'danno' sono indicazioni cliniche diverse: la vista deve distinguerle."""
    nota = json.loads((FIXTURES / "nota-linea-guida.json").read_text(encoding="utf-8"))
    negative = [r for r in nota["raccomandazioni"] if r["negativa"]]
    assert {r["categoria_negativa"] for r in negative} == {"danno", "nessun beneficio"}

    nota["raccomandazioni"][-1]["categoria_negativa"] = "sconsigliata"
    with pytest.raises(NotaNonValida):
        valida(nota)


def test_ambiguita_conservata_e_non_risolta() -> None:
    """L'incoerenza interna alla fonte va marcata, non sanata scegliendo un valore (§8)."""
    nota = json.loads((FIXTURES / "nota-review.json").read_text(encoding="utf-8"))
    ambigui = [d for d in nota["dosaggi"] if d["ambiguo"]]
    assert ambigui, "il fixture deve contenere il caso di ambiguità reale del corpus"
    for d in ambigui:
        assert "[formulazione ambigua nella fonte]" in d["valore"]


def test_dosaggi_assenti_solo_con_elenco_vuoto() -> None:
    for percorso in NOTE:
        nota = json.loads(percorso.read_text(encoding="utf-8"))
        if nota.get("dosaggi_assenti"):
            assert not nota.get("dosaggi"), f"{percorso.name}: dosaggi_assenti con dosaggi estratti"


def test_errori_leggibili() -> None:
    with pytest.raises(NotaNonValida) as info:
        valida({"meta": {}})
    messaggio = str(info.value)
    assert "meta" in messaggio and "title_it" in messaggio


def test_ogni_golden_ha_dosaggi_e_soglie() -> None:
    """La sezione è obbligatoria: se il documento non ha valori azionabili lo dice esplicitamente."""
    for percorso in sorted(GOLDEN.glob("*.md")):
        assert "## Dosaggi e soglie" in percorso.read_text(encoding="utf-8"), percorso.name


def test_ogni_review_del_corpus_raccoglie_le_trappole() -> None:
    """Nel Palmer e nell'Aletras gli autori identificano errori espliciti: vanno raggruppati."""
    for percorso in sorted(GOLDEN.glob("*.md")):
        if _tipo(percorso) != "narrative review":
            continue
        titoli = _titoli_h2(percorso)
        assert any(t.startswith(PREFISSI_TRAPPOLE) for t in titoli), percorso.name


def test_una_linea_guida_con_raccomandazioni_negative_ha_la_vista_cosa_non_fare() -> None:
    """Nelle review la sezione è condizionata (solo se gli autori identificano errori espliciti);
    in una linea guida che contiene raccomandazioni di classe 3 non è opzionale."""
    for percorso in sorted(GOLDEN.glob("*.md")):
        testo = percorso.read_text(encoding="utf-8")
        if _tipo(percorso) == "linea guida" and "COR 3" in testo:
            assert re.search(r"^## Cosa non fare", testo, flags=re.MULTILINE), percorso.name


def test_la_vista_cosa_non_fare_distingue_danno_da_nessun_beneficio() -> None:
    """Sulle linee guida che fanno la distinzione, la sola classe numerica non basta (§3B)."""
    testo = (GOLDEN / "2026-aha-acc-embolia-polmonare-acuta.md").read_text(encoding="utf-8")
    vista = testo[testo.index("## Cosa non fare") :]
    vista = vista[: vista.index("\n## ", 1)]
    assert "3: Harm" in vista and "3: No Benefit" in vista
    assert "rischio > beneficio" in vista and "beneficio = rischio" in vista


def test_la_vista_non_rimuove_le_raccomandazioni_dal_loro_posto_tematico() -> None:
    """La duplicazione è voluta: la vista è trasversale, non una migrazione (HANDOVER §6)."""
    testo = (GOLDEN / "2026-aha-acc-embolia-polmonare-acuta.md").read_text(encoding="utf-8")
    raccomandazioni = testo[testo.index("## Raccomandazioni") : testo.index("## Cosa non fare")]
    assert raccomandazioni.count("`COR 3: Harm`") == 3  # le altre tre stanno nelle tabelle
    assert "3: Harm / C-LD" in raccomandazioni
