"""Le chiamate al modello via API OpenAI.

Le parti comuni — prompt, conteggio dei costi, giro di correzione — stanno in `llm.py`: questo
file contiene solo ciò che cambia fra un fornitore e l'altro.

Usa la **Responses API** (`client.responses.create`), non Chat Completions: è quella su cui i
modelli che ragionano espongono `reasoning.effort`, ed è quella che riporta i token di
ragionamento separatamente in `usage`. Il prompt di estrazione va in `instructions`, il
documento in `input`.

Una differenza che conta rispetto ad Anthropic: su un modello che ragiona, `max_output_tokens`
comprende **anche** i token di ragionamento. Un tetto tarato sulla sola lunghezza della nota
farebbe finire la risposta in `incomplete` prima ancora di cominciare a scriverla.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import openai

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

# Il modello non lo fisso: la scelta è ancora aperta (HANDOVER §10.3) e il listino OpenAI
# cambia più in fretta di questo file. Elenca quelli che la tua chiave può usare con
#     python -c "import openai; [print(m.id) for m in openai.OpenAI().models.list()]"
# e mettine uno in config.toml. Quello che conta è l'accuratezza sui numeri, non il prezzo.
MODELLO_ESTRAZIONE = "gpt-5.5"
MODELLO_CLASSIFICAZIONE = "gpt-5.4-mini"

# Quanto far ragionare il modello. Alto sull'estrazione, dove un dosaggio trascritto male ha
# conseguenze; basso sulla classificazione, che è una scelta fra tre.
SFORZO_ESTRAZIONE = "high"
SFORZO_CLASSIFICAZIONE = "low"


@dataclass
class EstrattoreOpenAI:
    client: openai.OpenAI = field(
        default_factory=lambda: openai.OpenAI(max_retries=TENTATIVI_HTTP)
    )
    modello_estrazione: str = MODELLO_ESTRAZIONE
    modello_classificazione: str = MODELLO_CLASSIFICAZIONE

    def classifica(self, prompt: Prompt, testo: str) -> tuple[str, str | None, Uso]:
        risposta = self.client.responses.create(
            model=self.modello_classificazione,
            instructions=prompt.classificazione,
            input=testo[:CARATTERI_PER_CLASSIFICARE],
            max_output_tokens=MAX_TOKEN_CLASSIFICAZIONE,
            reasoning={"effort": SFORZO_CLASSIFICAZIONE},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "classificazione",
                    "schema": SCHEMA_CLASSIFICAZIONE,
                    "strict": True,
                }
            },
        )
        _controlla(risposta, MAX_TOKEN_CLASSIFICAZIONE)
        uso = _uso(risposta, self.modello_classificazione)
        dati = json.loads(risposta.output_text)
        log.info("classificato come «%s» (%s)", dati["tipo"], uso)
        return dati["tipo"], dati.get("tipo_rilevato"), uso

    def estrai(self, prompt: Prompt, testo: str, *, vocabolario: list[str]) -> Risultato:
        istruzioni = prompt.con_vocabolario(vocabolario)

        def chiama(messaggi):
            # In streaming: con centomila token di ingresso e un tetto di uscita alto la
            # richiesta non streaming supera i tempi massimi dell'SDK.
            with self.client.responses.stream(
                model=self.modello_estrazione,
                instructions=istruzioni,
                input=messaggi,
                max_output_tokens=MAX_TOKEN_ESTRAZIONE,
                reasoning={"effort": SFORZO_ESTRAZIONE},
            ) as flusso:
                risposta = flusso.get_final_response()
            _controlla(risposta, MAX_TOKEN_ESTRAZIONE)
            return risposta.output_text, _uso(risposta, self.modello_estrazione)

        return estrai_con_correzione(chiama, testo, modello=self.modello_estrazione)


def _controlla(risposta, tetto: int) -> None:
    """Traduce i modi in cui la Responses API smette in errori leggibili.

    `status` è `incomplete` sia quando il tetto è stato raggiunto sia quando è intervenuto il
    filtro dei contenuti: sono due situazioni diverse e vanno dette con parole diverse, perché
    portano a due rimedi diversi.
    """
    if risposta.status == "incomplete":
        motivo = getattr(risposta.incomplete_details, "reason", None)
        if motivo == "max_output_tokens":
            raise EstrazioneFallita(
                f"risposta troncata a {tetto} token: il JSON è incompleto e non recuperabile. "
                "Su un modello che ragiona il tetto comprende anche i token di ragionamento — "
                "alzalo, oppure spezza il documento per sezioni."
            )
        raise EstrazioneFallita(f"risposta incompleta ({motivo or 'motivo non riportato'})")
    if risposta.status == "failed":
        errore = getattr(risposta.error, "message", None) or "senza messaggio"
        raise EstrazioneFallita(f"la richiesta è fallita: {errore}")
    if not risposta.output_text.strip():
        # Capita quando il modello spende tutto il tetto in ragionamento senza scrivere nulla.
        raise EstrazioneFallita(
            "il modello non ha prodotto testo: probabile tetto di uscita troppo basso per il "
            "ragionamento richiesto"
        )


def _uso(risposta, modello: str) -> Uso:
    uso = getattr(risposta, "usage", None)
    return Uso(modello, getattr(uso, "input_tokens", 0) or 0,
               getattr(uso, "output_tokens", 0) or 0)
