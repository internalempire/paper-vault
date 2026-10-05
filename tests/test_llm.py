"""Verifiche sulle parti di src/llm.py che non dipendono dal fornitore.

Le chiamate vere stanno in `test_llm_anthropic.py` e `test_llm_openai.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.llm import (  # noqa: E402
    EstrazioneFallita,
    Prompt,
    Uso,
    estrai_con_correzione,
    json_della_risposta,
)

RADICE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PROMPT = Prompt.da_file(RADICE / "prompts" / "prompt-estrazione-v4-json.md")


def nota_valida() -> dict:
    return json.loads((FIXTURES / "nota-review.json").read_text(encoding="utf-8"))


def risponditore(*risposte: str):
    """Un finto `chiama`: restituisce le risposte in ordine e registra la conversazione."""
    coda = list(risposte)
    conversazioni: list[list[dict]] = []

    def chiama(messaggi):
        conversazioni.append([dict(m) for m in messaggi])
        return coda.pop(0), Uso("modello-finto", 1000, 200)

    chiama.conversazioni = conversazioni
    return chiama


# -- il prompt ---------------------------------------------------------------------------


def test_il_system_prompt_arriva_intero() -> None:
    """La sezione contiene le proprie sottosezioni: fermarsi alla prima lo troncherebbe."""
    for atteso in ("Fedeltà assoluta", "Passo 3B", "Schema di output", '"bibliografia"'):
        assert atteso in PROMPT.sistema


def test_il_system_prompt_non_contiene_il_documento() -> None:
    assert "{{TESTO_DOCUMENTO}}" not in PROMPT.sistema


def test_vocabolario_sostituito() -> None:
    reso = PROMPT.con_vocabolario(["sepsi", "shock-settico"])
    assert "{{VOCABOLARIO_TAG}}" not in reso
    assert "sepsi\nshock-settico" in reso


def test_vocabolario_vuoto_avvisa(caplog) -> None:
    """Senza vocabolario i tag divergono: dopo trenta paper si hanno quattro nomi per la sepsi."""
    reso = PROMPT.con_vocabolario([])
    assert "{{VOCABOLARIO_TAG}}" not in reso
    assert "andranno rivisti a mano" in caplog.text


def test_sezione_inesistente(tmp_path: Path) -> None:
    finto = tmp_path / "prompt.md"
    finto.write_text("# Un prompt senza le sezioni attese\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non trovata"):
        Prompt.da_file(finto)


# -- il giro di correzione ---------------------------------------------------------------


def test_nota_conforme_al_primo_colpo() -> None:
    risultato = estrai_con_correzione(
        risponditore(json.dumps(nota_valida())), "documento", modello="m"
    )
    assert risultato.riparazioni == 0
    assert risultato.nota["meta"]["tipo"] == "narrative review"


def test_blocco_di_codice_tollerato() -> None:
    """Il prompt lo vieta, capita lo stesso, e rifiutare per tre backtick costa una chiamata."""
    grezzo = "```json\n" + json.dumps(nota_valida()) + "\n```"
    assert estrai_con_correzione(risponditore(grezzo), "d", modello="m").riparazioni == 0


def test_una_nota_non_conforme_viene_corretta_una_volta() -> None:
    sporca = nota_valida()
    sporca["meta"]["criterio_riferimenti"] = "paragrafo"
    chiama = risponditore(json.dumps(sporca), json.dumps(nota_valida()))
    risultato = estrai_con_correzione(chiama, "d", modello="m")
    assert risultato.riparazioni == 1
    assert risultato.uso.token_ingresso == 2000  # due chiamate, entrambe contate


def test_la_richiesta_di_correzione_elenca_gli_errori() -> None:
    sporca = nota_valida()
    del sporca["meta"]["titolo_nota"]
    chiama = risponditore(json.dumps(sporca), json.dumps(nota_valida()))
    estrai_con_correzione(chiama, "d", modello="m")
    correzione = chiama.conversazioni[1][-1]["content"]
    assert "titolo_nota" in correzione
    assert "non aggiungere campi che il documento non contiene" in correzione


def test_due_fallimenti_non_diventano_un_ciclo() -> None:
    """Se sbaglia due volte è un problema del prompt, e va visto."""
    chiama = risponditore(json.dumps({"meta": {}}), json.dumps({"meta": {}}))
    with pytest.raises(EstrazioneFallita, match="non ha prodotto una nota conforme"):
        estrai_con_correzione(chiama, "d", modello="m")
    assert len(chiama.conversazioni) == 2


def test_json_illeggibile_chiede_di_rifare() -> None:
    chiama = risponditore("Ecco la nota che mi hai chiesto!", json.dumps(nota_valida()))
    assert estrai_con_correzione(chiama, "d", modello="m").riparazioni == 1
    assert "non è JSON valido" in chiama.conversazioni[1][-1]["content"]


def test_il_documento_arriva_come_primo_messaggio() -> None:
    chiama = risponditore(json.dumps(nota_valida()))
    estrai_con_correzione(chiama, "il testo del documento", modello="m")
    assert chiama.conversazioni[0] == [{"role": "user", "content": "il testo del documento"}]


def test_json_della_risposta() -> None:
    assert json_della_risposta('```json\n{"a": 1}\n```') == {"a": 1}
    assert json_della_risposta('  {"a": 1}  ') == {"a": 1}


# -- costi -------------------------------------------------------------------------------


def test_costo_stimato() -> None:
    uso = Uso("claude-opus-5", token_ingresso=100_000, token_uscita=4_000)
    assert uso.costo == pytest.approx(0.6)  # 100k×$5/M + 4k×$25/M


def test_usi_di_modelli_diversi_si_sommano_come_misto() -> None:
    totale = Uso("claude-haiku-4-5", 5_000, 50) + Uso("claude-opus-5", 100_000, 4_000)
    assert totale.modello == "misto"
    assert totale.token_ingresso == 105_000


def test_il_costo_di_una_corsa_mista_non_si_perde() -> None:
    """`misto` non è in listino: il totale va sommato dai costi, non ricalcolato dai token."""
    totale = Uso("claude-haiku-4-5", 5_000, 50) + Uso("claude-opus-5", 100_000, 4_000)
    assert totale.costo == pytest.approx(0.005 + 0.00025 + 0.5 + 0.1)


def test_modello_fuori_listino_non_finge_di_costare_zero() -> None:
    """Uno zero su una corsa che ha speso davvero sembra un dato, e non lo è."""
    uso = Uso("un-modello-non-a-listino", 1000, 100)
    assert uso.costo is None
    assert "prezzo non impostato" in str(uso)


def test_un_totale_con_un_prezzo_ignoto_resta_ignoto() -> None:
    totale = Uso("claude-opus-5", 1000, 100) + Uso("un-modello-non-a-listino", 1000, 100)
    assert totale.costo is None
