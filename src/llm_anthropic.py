"""Le chiamate al modello via API Anthropic.

Le parti comuni — prompt, conteggio dei costi, giro di correzione — stanno in `llm.py`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from src.llm import (
    CARATTERI_PER_CLASSIFICARE,
    MAX_TOKEN_CLASSIFICAZIONE,
    MAX_TOKEN_ESTRAZIONE,
    SCHEMA_CLASSIFICAZIONE,
    TENTATIVI_HTTP,
    EstrazioneFallita,
    Prompt,
    Risultato,
    Uso,
    estrai_con_correzione,
)

log = logging.getLogger(__name__)


@dataclass
class EstrattoreAnthropic:
    client: anthropic.Anthropic = field(
        default_factory=lambda: anthropic.Anthropic(max_retries=TENTATIVI_HTTP)
    )
    modello_estrazione: str = "claude-opus-5"
    modello_classificazione: str = "claude-haiku-4-5"

    def classifica(self, prompt: Prompt, testo: str) -> tuple[str, str | None, Uso]:
        risposta = self.client.messages.create(
            model=self.modello_classificazione,
            max_tokens=MAX_TOKEN_CLASSIFICAZIONE,
            system=prompt.classificazione,
            messages=[{"role": "user", "content": testo[:CARATTERI_PER_CLASSIFICARE]}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA_CLASSIFICAZIONE}},
        )
        uso = _uso(risposta, self.modello_classificazione)
        dati = json.loads(_testo(risposta))
        log.info("classificato come «%s» (%s)", dati["tipo"], uso)
        return dati["tipo"], dati.get("tipo_rilevato"), uso

    def estrai(self, prompt: Prompt, testo: str, *, vocabolario: list[str]) -> Risultato:
        sistema = prompt.con_vocabolario(vocabolario)

        def chiama(messaggi):
            # In streaming: con un documento lungo e un tetto di uscita alto la richiesta non
            # streaming supera i tempi massimi dell'SDK.
            with self.client.messages.stream(
                model=self.modello_estrazione,
                max_tokens=MAX_TOKEN_ESTRAZIONE,
                system=sistema,
                messages=messaggi,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            ) as flusso:
                risposta = flusso.get_final_message()
            _controlla(risposta)
            return _testo(risposta), _uso(risposta, self.modello_estrazione)

        return estrai_con_correzione(chiama, testo, modello=self.modello_estrazione)


def _controlla(risposta) -> None:
    if risposta.stop_reason == "refusal":
        dettagli = getattr(risposta, "stop_details", None)
        raise EstrazioneFallita(
            f"il modello ha rifiutato la richiesta "
            f"({getattr(dettagli, 'category', 'senza categoria')})"
        )
    if risposta.stop_reason == "max_tokens":
        raise EstrazioneFallita(
            f"risposta troncata a {MAX_TOKEN_ESTRAZIONE} token: il JSON è incompleto e non "
            "recuperabile. Alza MAX_TOKEN_ESTRAZIONE oppure spezza il documento per sezioni."
        )


def _testo(risposta) -> str:
    return "".join(b.text for b in risposta.content if b.type == "text").strip()


def _uso(risposta, modello: str) -> Uso:
    uso = getattr(risposta, "usage", None)
    return Uso(modello, getattr(uso, "input_tokens", 0) or 0,
               getattr(uso, "output_tokens", 0) or 0)
