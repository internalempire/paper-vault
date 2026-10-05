"""Verifiche su src/llm_openai.py, con un client simulato.

Le forme che qui contano e che differiscono da Anthropic: il prompt va in `instructions` e non
in un campo `system`, l'uscita strutturata sta sotto `text.format` con `name` accanto a
`schema`, e una risposta troncata si riconosce da `status == "incomplete"` invece che da uno
`stop_reason`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.llm import EstrazioneFallita, Prompt  # noqa: E402
from src.llm_openai import EstrattoreOpenAI  # noqa: E402

RADICE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PROMPT = Prompt.da_file(RADICE / "prompts" / "prompt-estrazione-v4-json.md")


class RispostaFinta:
    def __init__(self, testo: str, status: str = "completed", motivo: str | None = None,
                 errore: str | None = None):
        self.output_text = testo
        self.status = status
        self.incomplete_details = type("I", (), {"reason": motivo})() if motivo else None
        self.error = type("E", (), {"message": errore})() if errore else None
        self.usage = type("U", (), {"input_tokens": 1000, "output_tokens": 200})()


class FlussoFinto:
    def __init__(self, risposta):
        self._risposta = risposta

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get_final_response(self):
        return self._risposta


class RisposteFinte:
    def __init__(self, risposte):
        self._risposte = list(risposte)
        self.chiamate: list[dict] = []

    def _prossima(self, kwargs):
        self.chiamate.append(kwargs)
        return self._risposte.pop(0)

    def create(self, **kwargs):
        return self._prossima(kwargs)

    def stream(self, **kwargs):
        return FlussoFinto(self._prossima(kwargs))


class ClientFinto:
    def __init__(self, *risposte):
        self.responses = RisposteFinte(risposte)


def nota_valida() -> dict:
    return json.loads((FIXTURES / "nota-review.json").read_text(encoding="utf-8"))


def estrattore(*risposte) -> EstrattoreOpenAI:
    return EstrattoreOpenAI(client=ClientFinto(*risposte))


def test_classificazione() -> None:
    e = estrattore(RispostaFinta('{"tipo": "linea guida", "tipo_rilevato": null}'))
    tipo, rilevato, uso = e.classifica(PROMPT, "2026 AHA/ACC Guideline for…")
    assert (tipo, rilevato) == ("linea guida", None)
    assert uso.token_ingresso == 1000


def test_la_classificazione_e_vincolata_dallo_schema() -> None:
    """Oggetto minuscolo, tutti i campi obbligatori: è il caso per cui `json_schema` è fatto."""
    e = estrattore(RispostaFinta('{"tipo": "altro", "tipo_rilevato": "editoriale"}'))
    e.classifica(PROMPT, "x" * 100_000)
    chiamata = e.client.responses.chiamate[0]
    formato = chiamata["text"]["format"]
    assert formato["type"] == "json_schema" and formato["strict"] is True
    assert formato["name"], "la Responses API vuole un nome accanto allo schema"
    assert len(chiamata["input"]) < 10_000  # solo l'inizio del documento
    assert chiamata["reasoning"]["effort"] == "low"


def test_il_prompt_va_nelle_istruzioni_non_nel_documento() -> None:
    e = estrattore(RispostaFinta(json.dumps(nota_valida())))
    e.estrai(PROMPT, "il testo del documento", vocabolario=["sepsi"])
    chiamata = e.client.responses.chiamate[0]
    assert "Fedeltà assoluta" in chiamata["instructions"]
    assert "sepsi" in chiamata["instructions"]
    assert chiamata["input"] == [{"role": "user", "content": "il testo del documento"}]


def test_l_estrazione_non_e_vincolata_dallo_schema() -> None:
    """In modalità strict ogni proprietà dovrebbe essere obbligatoria: il modello riempirebbe
    sezioni che il documento non contiene."""
    e = estrattore(RispostaFinta(json.dumps(nota_valida())))
    e.estrai(PROMPT, "…", vocabolario=[])
    assert "text" not in e.client.responses.chiamate[0]


def test_l_estrazione_ragiona_a_fondo() -> None:
    e = estrattore(RispostaFinta(json.dumps(nota_valida())))
    e.estrai(PROMPT, "…", vocabolario=[])
    assert e.client.responses.chiamate[0]["reasoning"]["effort"] == "high"


def test_risposta_troncata_spiega_che_il_tetto_comprende_il_ragionamento() -> None:
    """È la differenza che fa perdere tempo: il tetto non è solo la lunghezza della nota."""
    e = estrattore(RispostaFinta("{'meta'", status="incomplete", motivo="max_output_tokens"))
    with pytest.raises(EstrazioneFallita, match="token di ragionamento"):
        e.estrai(PROMPT, "…", vocabolario=[])


def test_filtro_dei_contenuti_distinto_dal_troncamento() -> None:
    """Due situazioni diverse dietro lo stesso `incomplete`, e due rimedi diversi."""
    e = estrattore(RispostaFinta("", status="incomplete", motivo="content_filter"))
    with pytest.raises(EstrazioneFallita, match="content_filter"):
        e.estrai(PROMPT, "…", vocabolario=[])


def test_richiesta_fallita() -> None:
    e = estrattore(RispostaFinta("", status="failed", errore="modello non disponibile"))
    with pytest.raises(EstrazioneFallita, match="modello non disponibile"):
        e.estrai(PROMPT, "…", vocabolario=[])


def test_risposta_vuota_riconosciuta() -> None:
    """Capita quando il modello spende tutto il tetto in ragionamento senza scrivere nulla."""
    e = estrattore(RispostaFinta("   "))
    with pytest.raises(EstrazioneFallita, match="non ha prodotto testo"):
        e.estrai(PROMPT, "…", vocabolario=[])


def test_correzione_anche_su_openai() -> None:
    sporca = nota_valida()
    sporca["meta"]["criterio_riferimenti"] = "paragrafo"
    e = estrattore(RispostaFinta(json.dumps(sporca)), RispostaFinta(json.dumps(nota_valida())))
    risultato = e.estrai(PROMPT, "…", vocabolario=[])
    assert risultato.riparazioni == 1
    assert e.client.responses.chiamate[1]["input"][-1]["role"] == "user"
