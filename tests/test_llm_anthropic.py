"""Verifiche su src/llm_anthropic.py, con un client simulato.

Nessun test chiama l'API. Si verifica la forma della richiesta e che i modi in cui il modello
può fermarsi — rifiuto, risposta troncata — diano un errore leggibile invece di una nota
silenziosamente sbagliata.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.llm import EstrazioneFallita, Prompt  # noqa: E402
from src.llm_anthropic import EstrattoreAnthropic  # noqa: E402

RADICE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PROMPT = Prompt.da_file(RADICE / "prompts" / "prompt-estrazione-v4-json.md")


class BloccoFinto:
    def __init__(self, testo: str):
        self.type, self.text = "text", testo


class RispostaFinta:
    def __init__(self, testo: str, stop_reason: str = "end_turn", categoria: str | None = None):
        self.content = [BloccoFinto(testo)]
        self.stop_reason = stop_reason
        self.usage = type("U", (), {"input_tokens": 1000, "output_tokens": 200})()
        self.stop_details = type("D", (), {"category": categoria})() if categoria else None


class FlussoFinto:
    def __init__(self, risposta):
        self._risposta = risposta

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get_final_message(self):
        return self._risposta


class MessaggiFinti:
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
        self.messages = MessaggiFinti(risposte)


def nota_valida() -> dict:
    return json.loads((FIXTURES / "nota-review.json").read_text(encoding="utf-8"))


def estrattore(*risposte) -> EstrattoreAnthropic:
    return EstrattoreAnthropic(client=ClientFinto(*risposte))


def test_classificazione() -> None:
    e = estrattore(RispostaFinta('{"tipo": "linea guida", "tipo_rilevato": null}'))
    tipo, rilevato, uso = e.classifica(PROMPT, "2026 AHA/ACC Guideline for…")
    assert (tipo, rilevato) == ("linea guida", None)
    assert uso.modello == "claude-haiku-4-5"


def test_la_classificazione_usa_il_modello_economico_e_solo_l_inizio() -> None:
    e = estrattore(RispostaFinta('{"tipo": "altro", "tipo_rilevato": "editoriale"}'))
    e.classifica(PROMPT, "x" * 100_000)
    chiamata = e.client.messages.chiamate[0]
    assert chiamata["model"] == "claude-haiku-4-5"
    assert len(chiamata["messages"][0]["content"]) < 10_000
    assert chiamata["output_config"]["format"]["type"] == "json_schema"


def test_estrazione_in_streaming_col_modello_frontier() -> None:
    """Documento lungo e tetto di uscita alto: senza streaming si superano i tempi dell'SDK."""
    e = estrattore(RispostaFinta(json.dumps(nota_valida())))
    risultato = e.estrai(PROMPT, "…", vocabolario=[])
    chiamata = e.client.messages.chiamate[0]
    assert chiamata["model"] == "claude-opus-5"
    assert chiamata["thinking"] == {"type": "adaptive"}
    assert "format" not in chiamata.get("output_config", {})  # schema quasi tutto facoltativo
    assert risultato.riparazioni == 0


def test_rifiuto_segnalato_con_la_categoria() -> None:
    e = estrattore(RispostaFinta("", stop_reason="refusal", categoria="cyber"))
    with pytest.raises(EstrazioneFallita, match="cyber"):
        e.estrai(PROMPT, "…", vocabolario=[])


def test_risposta_troncata_non_viene_spacciata_per_nota() -> None:
    """Un JSON tagliato a metà non si recupera: meglio fermarsi e dirlo."""
    e = estrattore(RispostaFinta('{"meta": {"tit', stop_reason="max_tokens"))
    with pytest.raises(EstrazioneFallita, match="troncata"):
        e.estrai(PROMPT, "…", vocabolario=[])
