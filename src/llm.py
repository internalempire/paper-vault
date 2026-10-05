"""Le parti comuni delle chiamate al modello, indipendenti dal fornitore.

Le due chiamate — classificazione ed estrazione — stanno in `llm_anthropic.py` e
`llm_openai.py`. Qui vive tutto ciò che non dipende da chi risponde: il prompt letto dal suo
file, il conteggio di token e costi, e il giro di correzione quando il JSON non è conforme.

**Perché l'uscita strutturata solo sulla classificazione.** La classificazione restituisce un
oggetto minuscolo con tutti i campi obbligatori: è il caso per cui `json_schema` è fatto.
Lo schema della nota è l'opposto — decine di campi quasi tutti facoltativi, perché la regola 2
del prompt impone di **omettere** ciò che il documento non dice invece di riempirlo. Entrambi i
fornitori, in modalità strict, pretendono che ogni proprietà compaia fra le obbligatorie: il
modello produrrebbe sezioni vuote pur di soddisfare lo schema, che è esattamente il difetto che
quella regola esiste per evitare.

Per l'estrazione si usa quindi la via lunga: il prompt descrive la forma, `src/schema.py` la
verifica, e se la verifica fallisce si rimanda al modello l'elenco degli errori chiedendo di
correggere. Un giro di riparazione, non di più: se sbaglia due volte è un problema del prompt,
e va visto, non nascosto da un terzo tentativo.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from src.schema import NotaNonValida, valida

log = logging.getLogger(__name__)

TIPI = ("narrative review", "linea guida", "altro")

# La pipeline non ha vincoli di latenza: conviene insistere invece che fallire (HANDOVER §9).
TENTATIVI_HTTP = 5

# Il tetto è largo perché superarlo tronca il JSON a metà, e un JSON troncato non si recupera:
# la chiamata è spesa e persa. Sui modelli che ragionano il conto comprende anche i token di
# ragionamento, e una linea guida con novanta raccomandazioni ne consuma molti più di una
# review. Se il modello non arriva a tanto, l'API rifiuta la richiesta subito e gratis: è un
# errore più economico di una risposta troncata.
MAX_TOKEN_ESTRAZIONE = 64000
MAX_TOKEN_CLASSIFICAZIONE = 2000

# Quanti caratteri del documento bastano a riconoscerne il tipo: titolo, abstract, e come il
# documento si autodefinisce. Passare settantacinque pagine a un modello economico è spesa
# senza resa.
CARATTERI_PER_CLASSIFICARE = 6000

# $ per milione di token, ingresso e uscita. Serve solo a scrivere il costo nei log. Un modello
# assente qui non produce una stima sbagliata: produce «prezzo non impostato».
PREZZI: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

SCHEMA_CLASSIFICAZIONE = {
    "type": "object",
    "properties": {
        "tipo": {"enum": list(TIPI)},
        "tipo_rilevato": {
            "type": ["string", "null"],
            "description": "Se tipo è 'altro', il tipo effettivo (trial randomizzato, "
            "case report, editoriale). Altrimenti null.",
        },
    },
    "required": ["tipo", "tipo_rilevato"],
    "additionalProperties": False,
}


class EstrazioneFallita(RuntimeError):
    """Il modello non ha prodotto una nota conforme, nemmeno dopo la correzione."""


@dataclass
class Uso:
    """Quanto è costata una chiamata. Si accumula lungo la pipeline."""

    modello: str = ""
    token_ingresso: int = 0
    token_uscita: int = 0
    # Somma di costi già calcolati, per gli usi che mettono insieme modelli diversi: lì il
    # listino non si può più applicare al totale, perché i prezzi non sono gli stessi.
    _costo: float | None = None

    @property
    def costo(self) -> float | None:
        """Il costo stimato, o `None` se il modello non è a listino.

        `None` e non zero: un costo di zero su una corsa che ha speso davvero è peggio di un
        costo assente, perché sembra un dato.
        """
        if self._costo is not None:
            return self._costo
        if self.modello not in PREZZI:
            return None
        ingresso, uscita = PREZZI[self.modello]
        return self.token_ingresso / 1e6 * ingresso + self.token_uscita / 1e6 * uscita

    def __add__(self, altro: "Uso") -> "Uso":
        costi = [c for c in (self.costo, altro.costo) if c is not None]
        return Uso(
            modello=self.modello if self.modello == altro.modello else "misto",
            token_ingresso=self.token_ingresso + altro.token_ingresso,
            token_uscita=self.token_uscita + altro.token_uscita,
            _costo=sum(costi) if len(costi) == 2 else None,
        )

    def __str__(self) -> str:
        costo = f"~${self.costo:.4f}" if self.costo is not None else "prezzo non impostato"
        return (f"{self.modello}: {self.token_ingresso} token in, "
                f"{self.token_uscita} out, {costo}")


@dataclass
class Risultato:
    """La nota estratta e quanto è costato ottenerla."""

    nota: dict[str, Any]
    uso: Uso
    riparazioni: int = 0


class Estrattore(Protocol):
    """Quello che `ingest.py` si aspetta da un fornitore, qualunque esso sia."""

    def classifica(self, prompt: "Prompt", testo: str) -> tuple[str, str | None, Uso]: ...

    def estrai(self, prompt: "Prompt", testo: str, *, vocabolario: list[str]) -> Risultato: ...


# --------------------------------------------------------------------------------------
# Il prompt
# --------------------------------------------------------------------------------------


@dataclass
class Prompt:
    """Il prompt di estrazione, letto dal file e pronto per le istruzioni di sistema."""

    sistema: str
    classificazione: str

    @classmethod
    def da_file(cls, percorso: str | Path) -> "Prompt":
        testo = Path(percorso).read_text(encoding="utf-8")
        return cls(sistema=_sezione(testo, "SYSTEM PROMPT"),
                   classificazione=_sezione(testo, "Passo 1 — Classificazione"))

    def con_vocabolario(self, tag: list[str]) -> str:
        """Il system prompt con il vocabolario controllato al suo posto.

        Senza vocabolario il modello propone tag liberi: dopo trenta paper si ritrovano
        `sepsi`, `sepsis`, `shock-settico` e `settico` come tag distinti (HANDOVER §4). Fino
        alla Fase 4 il vocabolario può essere vuoto, ma il segnaposto va comunque sostituito,
        altrimenti il modello legge le graffe come parte delle istruzioni.
        """
        if not tag:
            log.warning("nessun vocabolario tag: i tag proposti andranno rivisti a mano")
        return self.sistema.replace(
            "{{VOCABOLARIO_TAG}}", "\n".join(tag) if tag else "(vocabolario ancora vuoto)"
        )


def _sezione(testo: str, titolo: str) -> str:
    """Il contenuto di una sezione del file di prompt, fino all'intestazione successiva.

    Il prompt vive nel suo file markdown e non va duplicato qui: è l'artefatto centrale del
    progetto, validato a mano su tre documenti, e due copie divergerebbero al primo ritocco.
    """
    apertura = re.search(rf"^(#+) {re.escape(titolo)}\s*$", testo, flags=re.MULTILINE)
    if not apertura:
        raise ValueError(f"sezione «{titolo}» non trovata nel file di prompt")

    # Si ferma alla prima intestazione di pari livello o superiore: il system prompt contiene
    # le proprie sottosezioni, e fermarsi alla prima di queste lo troncherebbe alla riga uno.
    livello = len(apertura.group(1))
    resto = testo[apertura.end() :]
    chiusura = re.search(rf"^#{{1,{livello}}} ", resto, flags=re.MULTILINE)
    sezione = resto[: chiusura.start()] if chiusura else resto
    return sezione.strip().rstrip("-").strip()


# --------------------------------------------------------------------------------------
# Il giro di correzione
# --------------------------------------------------------------------------------------


def estrai_con_correzione(
    chiama: Callable[[list[dict[str, str]]], tuple[str, Uso]],
    testo: str,
    *,
    modello: str,
) -> Risultato:
    """Chiede la nota, la valida, e se non è conforme chiede di correggerla. Una volta sola.

    `chiama` riceve la conversazione e restituisce il testo grezzo della risposta più il suo
    costo. È l'unico punto in cui i due fornitori differiscono.
    """
    messaggi: list[dict[str, str]] = [{"role": "user", "content": testo}]
    uso = Uso(modello)

    for riparazioni in range(2):
        grezzo, uso_chiamata = chiama(messaggi)
        uso = uso + uso_chiamata
        try:
            nota = valida(json_della_risposta(grezzo))
        except (NotaNonValida, json.JSONDecodeError) as exc:
            if riparazioni:
                raise EstrazioneFallita(
                    f"il modello non ha prodotto una nota conforme: {exc}"
                ) from exc
            log.warning("nota non conforme, chiedo la correzione: %s", exc)
            messaggi += [
                {"role": "assistant", "content": grezzo},
                {"role": "user", "content": richiesta_di_correzione(exc)},
            ]
            continue
        log.info("nota estratta (%s)%s", uso, " dopo correzione" if riparazioni else "")
        return Risultato(nota=nota, uso=uso, riparazioni=riparazioni)
    raise AssertionError("irraggiungibile")


def richiesta_di_correzione(errore: Exception) -> str:
    if isinstance(errore, json.JSONDecodeError):
        return (
            "La risposta precedente non è JSON valido: "
            f"{errore}. Restituisci di nuovo la nota, solo l'oggetto JSON, senza preamboli "
            "né blocchi di codice."
        )
    return (
        "La nota precedente non è conforme allo schema. Errori:\n"
        f"{errore}\n\n"
        "Correggi **solo** questi punti e restituisci di nuovo l'oggetto JSON completo. "
        "Non riscrivere il contenuto e non aggiungere campi che il documento non contiene."
    )


def json_della_risposta(grezzo: str) -> dict[str, Any]:
    """Il JSON della risposta, tolto l'eventuale blocco di codice che lo racchiude.

    Il prompt chiede di non usare blocchi di codice; capita lo stesso, e rifiutare la risposta
    per tre backtick costerebbe una chiamata intera.
    """
    ripulito = re.sub(r"^```(?:json)?\s*|\s*```$", "", grezzo.strip())
    return json.loads(ripulito)
