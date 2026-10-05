"""Entrypoint: PDF, HTML o URL full-text in ingresso, una nota Markdown in uscita.

    python -m src.ingest paper.pdf

Il percorso è quello dello HANDOVER §6: estrazione del testo → DOI → CrossRef → classificazione
→ estrazione → JSON → markdown → il PDF in archivio. Niente watcher, niente Docker, niente
SQLite: quelli sono la Fase 3.

`--dry-run` percorre tutto ciò che non costa — estrazione, DOI, metadati — e si ferma prima
della chiamata al modello, dicendo cosa avrebbe fatto. È il modo di guardare un documento
nuovo prima di spendere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import dotenv

from src import metadata, render
from src.extract import Documento, extract
from src.llm import Estrattore, Prompt, Uso
from src.metadata import Metadati
from src.tags import vocabolario

log = logging.getLogger(__name__)

RADICE = Path(__file__).resolve().parents[1]


class ChiaveMancante(RuntimeError):
    """Manca la chiave API del fornitore configurato."""


@dataclass
class Configurazione:
    """I percorsi e i modelli. `config.toml` sovrascrive i valori qui sotto."""

    note: Path = RADICE / "note"
    json: Path = RADICE / "note" / ".json"
    tags: Path = RADICE / "tags.md"
    prompt: Path = RADICE / "prompts" / "prompt-estrazione-v4-json.md"
    fornitore: str = "anthropic"
    modello_estrazione: str = ""
    modello_classificazione: str = ""
    ocr: bool = True
    tabelle: bool = True
    soglia_token_avviso: int = 150_000
    crossref_mailto: str | None = None
    timeout: float = 15.0

    @classmethod
    def carica(cls, percorso: str | Path | None = None) -> "Configurazione":
        percorso = Path(percorso) if percorso else RADICE / "config.toml"
        if not percorso.is_file():
            log.info("nessun config.toml: uso i valori predefiniti")
            return cls()
        dati = tomllib.loads(percorso.read_text(encoding="utf-8"))
        percorsi = dati.get("percorsi", {})
        modelli = dati.get("modelli", {})
        estrazione = dati.get("estrazione", {})
        meta = dati.get("metadati", {})
        return cls(
            fornitore=modelli.get("fornitore", cls.fornitore),
            note=_espandi(percorsi.get("note"), cls.note),
            json=_espandi(percorsi.get("json"), cls.json),
            prompt=_espandi(estrazione.get("prompt"), cls.prompt),
            modello_estrazione=modelli.get("estrazione", ""),
            modello_classificazione=modelli.get("classificazione", ""),
            ocr=estrazione.get("ocr", cls.ocr),
            tabelle=estrazione.get("tabelle", cls.tabelle),
            soglia_token_avviso=estrazione.get("soglia_token_avviso", cls.soglia_token_avviso),
            crossref_mailto=meta.get("crossref_mailto"),
            timeout=meta.get("timeout_secondi", cls.timeout),
        )


def _espandi(valore, predefinito: Path) -> Path:
    """Il percorso da configurazione, con la tilde sciolta e il relativo ancorato al progetto.

    Un `note = "note"` in config.toml deve indicare la stessa cartella da qualunque directory
    si lanci il comando: risolverlo sulla directory corrente significherebbe scrivere le note
    dove capita di trovarsi.
    """
    if not valore:
        return predefinito
    percorso = Path(valore).expanduser()
    return percorso if percorso.is_absolute() else RADICE / percorso


@dataclass
class Esito:
    """Cosa è stato prodotto, e quanto è costato."""

    slug: str
    documento: Documento
    metadati: Metadati
    nota: Path | None = None
    pubblicata: str | None = None
    json: Path | None = None
    documento_sorgente: str | Path | None = None
    uso: Uso = field(default_factory=Uso)
    avvisi: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# La pipeline
# --------------------------------------------------------------------------------------


def prepara_ingresso(percorso: str | Path, configurazione: Configurazione) -> Esito:
    """Estrazione e metadati senza LLM: lo stesso risultato può essere approvato dalla UI."""
    from src.web_article import e_url

    percorso = str(percorso) if e_url(percorso) else Path(percorso).expanduser().resolve()
    avvisi: list[str] = []

    # 1. Testo. Il conteggio token serve a sapere quando il documento singolo smetterà di
    #    entrare in una chiamata sola (§6.2): finché non succede, il map-reduce si rimanda.
    documento = extract(
        percorso, ocr=configurazione.ocr, tabelle=configurazione.tabelle
    )
    if documento.token_stimati > configurazione.soglia_token_avviso:
        avvisi.append(
            f"~{documento.token_stimati} token stimati, oltre la soglia di "
            f"{configurazione.soglia_token_avviso}: valutare il taglio per sezioni via TOC"
        )

    # 2. Metadati. Il DOI si cerca sulla prima pagina: più avanti ci sono quelli della
    #    bibliografia, che appartengono ad altri articoli.
    # Nell’HTML una sola "pagina" comprende anche la bibliografia: niente regex sul corpo.
    doi = (documento.sorgente_web.get("doi") if documento.sorgente_web is not None else
           metadata.trova_doi(documento.pagine[0].testo) if documento.pagine else None)
    metadati = metadata.recupera(
        doi, mailto=configurazione.crossref_mailto, timeout=configurazione.timeout
    )
    if metadati.mancanti:
        avvisi.append(f"metadati da completare a mano: {', '.join(metadati.mancanti)}")
    if documento.pagine and not metadata.titolo_riconosciuto(
        metadati.titolo, documento.pagine[0].testo
    ):
        avvisi.append(
            f"il titolo del DOI «{metadati.titolo}» non compare nella prima pagina: "
            "il DOI potrebbe essere di un altro articolo citato lì. Da verificare a mano"
        )

    # 3. I numeri di pagina stampati si rileggono ora che CrossRef ha detto da quale comincia
    #    l'articolo: è l'indizio che scioglie i casi in cui due serie crescono di pari passo.
    if documento.formato == "pdf" and metadati.prima_pagina and not documento.etichette_ricostruite:
        documento = extract(
            percorso,
            ocr=configurazione.ocr,
            tabelle=configurazione.tabelle,
            prima_pagina=metadati.prima_pagina,
        )
    if not documento.etichette_ricostruite and documento.formato == "pdf":
        avvisi.append("numeri di pagina non ricostruiti: i rimandi saranno ai numeri di sezione")
    if documento.codifica_sospetta:
        avvisi.append(
            "il PDF usa font a codifica propria: lettere greche e prefissi di unità possono "
            "uscire sbagliati (`µm` letto come `mm`, un fattore mille). Le unità di misura "
            "della nota vanno ricontrollate sull'originale"
        )

    return Esito(slug="", documento=documento, metadati=metadati, avvisi=avvisi,
                 documento_sorgente=percorso)


def ingest(
    percorso: str | Path,
    configurazione: Configurazione,
    *,
    estrattore: Estrattore | None = None,
    dry_run: bool = False,
    forza: bool = False,
    data_nota: dt.date | str | None = None,
    vault=None,
    preparato: Esito | None = None,
) -> Esito:
    esito = preparato or prepara_ingresso(percorso, configurazione)
    documento, metadati, avvisi = esito.documento, esito.metadati, esito.avvisi
    percorso = esito.documento_sorgente or documento.percorso

    # La versione Fase 1 dell'idempotenza: lo stato in SQLite arriva con l'automazione. La
    # chiave è il DOI, non il nome del file — il nome dipende dal titolo tradotto, che il
    # modello potrebbe rendere diversamente a ogni giro.
    if (gia_fatto := _gia_elaborato(configurazione.json, metadati.doi)) and not forza:
        log.info(
            "%s è già stato elaborato: rigenero il markdown senza chiamare l'API. "
            "Il documento resta dov'è — usa --forza per rielaborarlo davvero",
            metadati.doi,
        )
        esito.slug = gia_fatto.stem
        esito.json = gia_fatto
        if not dry_run:
            salvato = json.loads(gia_fatto.read_text(encoding="utf-8"))
            _prepara_figure(esito, salvato, configurazione)
            esito.nota = _scrivi(
                configurazione.note / f"{gia_fatto.stem}.md", render.render_da_file(gia_fatto)
            )
            _pubblica(esito, vault, avvisi)
        return esito
    if not metadati.doi:
        avvisi.append("senza DOI non c'è modo di riconoscere un documento già elaborato")

    if dry_run:
        log.info("dry-run: mi fermo prima della chiamata al modello")
        return esito

    # 4-5. Classificazione ed estrazione.
    prompt = Prompt.da_file(configurazione.prompt)
    estrattore = estrattore or crea_estrattore(configurazione)
    testo = documento.document_text()
    tipo, _, uso_classificazione = estrattore.classifica(prompt, testo)
    risultato = estrattore.estrai(
        prompt, testo, vocabolario=vocabolario(configurazione.tags)
    )
    esito.uso = uso_classificazione + risultato.uso

    # Il nome del file esce dal titolo **italiano**: la nota vive in un vault italiano e va
    # ritrovata da chi ci cerca dentro, non da un indicizzatore.
    esito.slug = slug = metadata.slug(metadati, risultato.nota["meta"]["title_it"])
    percorso_json = configurazione.json / f"{slug}.json"
    percorso_nota = configurazione.note / f"{slug}.md"

    if risultato.nota["meta"]["tipo"] != tipo:
        # Non è un errore: il classificatore vede solo l'inizio del documento. Ma è il segnale
        # che il tipo è dubbio, e su un documento dubbio la nota va guardata.
        avvisi.append(
            f"classificato «{tipo}» ma estratto come «{risultato.nota['meta']['tipo']}»: "
            "vale la pena rileggere la nota"
        )

    # 6-7. JSON e markdown. Il documento **non** viene toccato: resta dove l'utente l'ha messo,
    #      e il frontmatter ne registra il percorso assoluto. Un archivio parallelo dentro il
    #      vault significherebbe due copie dello stesso PDF che divergono appena una viene
    #      annotata, e il dubbio su quale sia quella buona.
    esito.documento_sorgente = percorso
    collegamento = str(percorso)

    salvato = render.involucro(risultato.nota, metadati, pdf=collegamento, data_nota=data_nota)
    if documento.sorgente_web:
        salvato["sorgente"] = documento.sorgente_web
    # Salva prima l'estrazione pagata: un problema con le figure non deve perderla.
    esito.json = render.salva(percorso_json, salvato)
    _prepara_figure(esito, salvato, configurazione)
    esito.nota = _scrivi(percorso_nota, render.render_da_file(esito.json))

    if nuovi := risultato.nota["meta"].get("tags_nuovi"):
        avvisi.append(f"tag proposti da validare: {', '.join(nuovi)}")
    _pubblica(esito, vault, avvisi)
    log.info("%s — %s", esito.nota, esito.uso)
    return esito


def _prepara_figure(esito: Esito, salvato: dict, configurazione: Configurazione) -> None:
    """Riprende anche un'estrazione immagini interrotta, senza ripagare l'LLM."""
    if not salvato["nota"].get("figure_richieste"):
        return
    from src.figure import prepara, anteprima

    try:
        if "figure" not in salvato:
            base = render.render(salvato["nota"], Metadati(**salvato["metadati"]),
                                 pdf=salvato["pdf"], data_nota=salvato.get("data_nota"))
            documento = esito.documento
            # Il DOI può essere riaperto da un PDF o URL diverso: le figure devono
            # restare ancorate esattamente alla fonte della nota già pagata.
            if salvato.get("sorgente", {}).get("tipo") == "web":
                from src.web_article import da_snapshot
                documento = da_snapshot(salvato["sorgente"])
            elif salvato.get("pdf") and str(documento.percorso) != salvato["pdf"]:
                documento = extract(salvato["pdf"], ocr=configurazione.ocr, tabelle=configurazione.tabelle)
            salvato["figure"], avvisi = prepara(
                salvato["nota"], documento, base, configurazione.note, esito.slug
            )
            esito.avvisi.extend(avvisi)
            render.salva(esito.json, salvato)
        if any(f["stato"] not in ("approvata", "esclusa") for f in salvato["figure"]):
            preview = anteprima(salvato, configurazione.note,
                                configurazione.note / ".anteprime" / f"{esito.slug}.html")
            esito.avvisi.append(f"figure da verificare prima della pubblicazione: {preview}")
    except (OSError, ValueError, RuntimeError) as exc:
        esito.avvisi.append(f"preparazione figure fallita; JSON conservato, riprovare senza --forza: {exc}")


def _pubblica(esito: Esito, vault, avvisi: list[str]) -> None:
    """Manda la nota al vault e ne cancella la copia locale, in quest'ordine.

    Un fallimento nella pubblicazione **non** fa fallire l'elaborazione: la nota c'è, è costata
    una chiamata, e resta qui pronta per essere rimandata su. Diventa un avviso, non un errore.
    """
    if vault is None or esito.nota is None:
        return
    from src.pubblica import PubblicazioneFallita, pubblica_file

    try:
        esito.pubblicata = pubblica_file(vault, esito.nota, cancella=True)
        esito.nota = None
        # L'indice è generato dalla pipeline, non da una query dentro l'applicazione: va
        # rifatto a ogni nota nuova, altrimenti la pagina di apertura resta indietro.
        from src.indice import pubblica_indice

        pubblica_indice(vault, esito.json.parent)
    except PubblicazioneFallita as exc:
        avvisi.append(f"pubblicazione fallita, la nota resta in locale: {exc}")


def _gia_elaborato(cartella: Path, doi: str | None) -> Path | None:
    """Il JSON di un documento con lo stesso DOI, se c'è già.

    Scorrere la cartella è sufficiente a questi volumi — duecento paper l'anno — e non
    richiede l'indice che arriverà con l'automazione.
    """
    if not doi or not cartella.is_dir():
        return None
    atteso = doi.lower()
    for percorso in sorted(cartella.glob("*.json")):
        try:
            dati = json.loads(percorso.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if ((dati.get("metadati") or {}).get("doi") or "").lower() == atteso:
            return percorso
    return None


def crea_estrattore(configurazione: Configurazione) -> Estrattore:
    """L'estrattore del fornitore configurato.

    Due implementazioni concrete, nessuno strato in mezzo: `llm_anthropic` e `llm_openai`
    espongono gli stessi due metodi e condividono in `llm.py` tutto ciò che non dipende da chi
    risponde. I modelli lasciati vuoti in configurazione prendono i valori predefiniti del
    fornitore, che è il modo di non fissare qui una scelta ancora aperta (HANDOVER §10.3).
    """
    if configurazione.fornitore == "anthropic":
        from src.llm_anthropic import EstrattoreAnthropic as Classe

        variabile = "ANTHROPIC_API_KEY"
    elif configurazione.fornitore == "openai":
        from src.llm_openai import EstrattoreOpenAI as Classe

        variabile = "OPENAI_API_KEY"
    else:
        raise ValueError(
            f"fornitore «{configurazione.fornitore}» sconosciuto: attesi «anthropic» o «openai»"
        )

    modelli = {
        nome: valore
        for nome, valore in (
            ("modello_estrazione", configurazione.modello_estrazione),
            ("modello_classificazione", configurazione.modello_classificazione),
        )
        if valore
    }
    try:
        return Classe(**modelli)
    except Exception as exc:
        # L'SDK dice «api_key client option must be set», che non aiuta a capire *dove*.
        raise ChiaveMancante(
            f"{variabile} non è impostata: mettila in .env (vedi .env.example) oppure "
            f"esportala nella shell. Fornitore configurato: {configurazione.fornitore}."
        ) from exc


def _scrivi(percorso: Path, contenuto: str) -> Path:
    percorso.parent.mkdir(parents=True, exist_ok=True)
    percorso.write_text(contenuto, encoding="utf-8")
    return percorso


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _riepilogo(esito: Esito, *, dry_run: bool) -> str:
    doc = esito.documento
    righe = [
        f"documento        {(doc.sorgente_web or {}).get('url_finale', doc.percorso.name)}",
        f"pagine           {doc.n_pagine} ({doc.formato}), {doc.n_tabelle} tabelle",
        f"token stimati    ~{doc.token_stimati}",
        f"riferimenti      per {doc.criterio_riferimenti}",
        f"doi              {esito.metadati.doi or '— non trovato'}",
        f"autori           {esito.metadati.autori_brevi or '—'}",
        f"rivista          {esito.metadati.rivista or '—'} ({esito.metadati.anno or '—'})",
        f"slug             {esito.slug or '— dal titolo italiano, dopo l\'estrazione'}",
    ]
    if dry_run:
        righe.append("")
        righe.append("dry-run: nessuna chiamata al modello, nessun file scritto")
    else:
        righe += [
            f"nota             {esito.nota or ('pubblicata sul vault: ' + esito.pubblicata) if esito.pubblicata else (esito.nota or '—')}",
            f"json             {esito.json or '—'}",
            f"documento        resta in {esito.documento_sorgente or '—'}",
            f"costo            {esito.uso}",
        ]
    if esito.avvisi:
        righe.append("")
        righe.append("avvisi:")
        righe += [f"  - {a}" for a in esito.avvisi]
    return "\n".join(righe)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.ingest",
        description="Trasforma un PDF o full-text web in una nota markdown consultabile.",
    )
    parser.add_argument("percorso", help="PDF, HTML locale o URL HTTP(S) del full-text")
    parser.add_argument("--out", type=Path, help="cartella delle note (default: da config.toml)")
    parser.add_argument("--json-out", type=Path, help="cartella dei JSON intermedi")
    parser.add_argument("--model", help="modello per l'estrazione, se diverso da quello in config")
    parser.add_argument("--config", type=Path, help="file di configurazione")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="estrai, cerca il DOI e i metadati, poi fermati prima di spendere",
    )
    parser.add_argument(
        "--pubblica",
        action="store_true",
        help="invia la nota al vault remoto e cancella la copia locale, dopo la verifica",
    )
    parser.add_argument(
        "--forza",
        action="store_true",
        help="rielabora anche se il JSON esiste già (spende una chiamata)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # `override=False`: una variabile già presente nell'ambiente vince sul file. Serve a poter
    # provare una chiave diversa per una singola corsa senza toccare il .env.
    dotenv.load_dotenv(RADICE / ".env", override=False)
    configurazione = Configurazione.carica(args.config)
    if args.out:
        configurazione.note = args.out.expanduser()
    if args.json_out:
        configurazione.json = args.json_out.expanduser()
    if args.model:
        configurazione.modello_estrazione = args.model

    vault = None
    if args.pubblica:
        from src.pubblica import Vault

        vault = Vault.da_ambiente()

    try:
        esito = ingest(
            args.percorso, configurazione, dry_run=args.dry_run, forza=args.forza, vault=vault
        )
    except Exception as exc:  # il PDF resta dov'era: si può ridare in pasto
        log.error("%s: %s", type(exc).__name__, exc)
        return 1
    print(_riepilogo(esito, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
