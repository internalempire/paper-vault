"""Metadati bibliografici: DOI dal documento, poi CrossRef con ripiego su PubMed.

Autori, rivista, anno e DOI **non** li produce il modello (HANDOVER §4): sono i dati su cui
un LLM occasionalmente inventa, e un autore sbagliato è peggio di un autore mancante perché
non si vede. Quando entrambe le fonti falliscono i campi restano vuoti e la nota lo dichiara.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
import dataclasses
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

CROSSREF = "https://api.crossref.org/works/{doi}"
PUBMED_RICERCA = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_RIEPILOGO = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# Un DOI comincia sempre con `10.` seguito dal prefisso del registrante.
_RE_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)

# Un DOI che va a capo lascia sulla prima riga il solo prefisso del registrante.
_RE_DOI_TRONCATO = re.compile(r"(10\.\d{4,9}/)\s*$")

# Il seguito plausibile di un DOI: niente spazi, niente punteggiatura di frase.
_RE_SEGUITO_DOI = re.compile(r"[-._;()/:A-Za-z0-9]{4,}")

# Quante righe guardare, dopo il troncamento, prima di rinunciare.
_RIGHE_PER_IL_SEGUITO = 12

# Nei PDF il DOI viene composto con trattini tipografici — non-breaking, en dash, figure dash —
# e a volte porta con sé caratteri di controllo invisibili. Un DOI così non risolve: né CrossRef
# né PubMed lo riconoscono, e finirebbe nel frontmatter come un rimando che non funziona.
_TRATTINI_TIPOGRAFICI = dict.fromkeys(
    map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"), "-"
)
_RE_CONTROLLO = re.compile(r"[\x00-\x1f\x7f]")

# La coda di un DOI trovato nel testo raccoglie punteggiatura di contorno.
_CODA_DA_TOGLIERE = ".,;:)]}>'\""

# Oltre questo numero di autori la citazione si tronca. La soglia è tarata sul corpus: le
# review a firma multipla li elencano tutti, le linee guida di società hanno decine di
# firmatari e vanno troncate sempre.
_AUTORI_PER_ESTESO = 8

# Nel frontmatter l'elenco resta leggibile solo se corto.
_AUTORI_NEL_FRONTMATTER = 3

# Parole che non distinguono un documento dall'altro. `acuta` e `cronica` **non** sono qui:
# in medicina quella distinzione identifica il documento quanto l'organo.
_PAROLE_VUOTE = {
    "aggiornamento", "alla", "and", "dei", "del", "della", "delle", "for", "guideline",
    "guidelines", "nei", "nel", "nella", "nelle", "per", "the", "una", "with",
    # Forme elise: l'apostrofo le separa dal sostantivo, e senza queste `nell'adulto`
    # occuperebbe due posti dei quattro disponibili con `nell`.
    "all", "coll", "dall", "dell", "nell", "quell", "sull",
}

# Articoli e preposizioni sono quasi tutti di una o due lettere: scartarli per lunghezza
# copre anche quelli che non sono in elenco. Senza, «resistenza ai diuretici nella sindrome
# cardiorenale» diventa `resistenza-ai-diuretici-sindrome`, con il sostantivo che conta tagliato.
_LUNGHEZZA_MINIMA_PAROLA = 3


@dataclass
class Metadati:
    """Ciò che CrossRef o PubMed sanno del documento. Ogni campo può mancare."""

    autori: list[str] = field(default_factory=list)
    titolo: str | None = None  # titolo originale, non tradotto
    rivista: str | None = None  # nome esteso, per il frontmatter
    rivista_abbreviata: str | None = None  # per la citazione Vancouver
    anno: int | None = None
    volume: str | None = None
    fascicolo: str | None = None
    pagine: str | None = None
    doi: str | None = None

    @property
    def mancanti(self) -> list[str]:
        """I campi che né CrossRef né PubMed hanno saputo dare.

        Vanno segnalati, non riempiti: una nota con l'autore sbagliato è peggio di una nota
        senza autore, perché non si vede.
        """
        return [
            nome
            for nome, valore in (
                ("autori", self.autori),
                ("titolo", self.titolo),
                ("rivista", self.rivista),
                ("anno", self.anno),
                ("doi", self.doi),
            )
            if not valore
        ]

    @property
    def autori_brevi(self) -> str:
        """Come compaiono nel frontmatter: elenco se pochi, primo autore et al. se molti."""
        if not self.autori:
            return ""
        if len(self.autori) <= _AUTORI_NEL_FRONTMATTER:
            return ", ".join(self.autori)
        return ", ".join(self.autori[:_AUTORI_NEL_FRONTMATTER]) + ", et al."

    @property
    def autori_citazione(self) -> str:
        if not self.autori:
            return ""
        if len(self.autori) <= _AUTORI_PER_ESTESO:
            return ", ".join(self.autori)
        return ", ".join(self.autori[:3]) + ", et al."

    @property
    def prima_pagina(self) -> str | None:
        """La prima pagina dell'articolo, che scioglie le ambiguità sui numeri stampati."""
        if not self.pagine:
            return None
        return re.split(r"[-–]", self.pagine)[0].strip() or None

    def citazione_breve(self) -> str:
        """Una riga sola in testa alla nota: chi, dove, quando.

        Il DOI e la citazione completa stanno in fondo, sotto «Riferimento bibliografico»:
        ripeterli qui allungherebbe la testa della nota senza aggiungere niente.
        """
        dove = [p for p in (self.rivista_abbreviata or self.rivista, self.anno) if p]
        sede = ", ".join(str(p) for p in dove)
        if self.autori_brevi and sede:
            return f"{self.autori_brevi} — {sede}"
        return self.autori_brevi or sede

    def citazione(self) -> str:
        """La citazione in stile Vancouver, in markdown.

        Es. `Palmer BF, Clegg DJ. **Pathophysiology of Ketoacidosis: Core Curriculum 2026.**
        *Am J Kidney Dis.* 2026;88(2):301-314. doi:10.1053/j.ajkd.2026.02.646`
        """
        pezzi = []
        if autori := self.autori_citazione:
            # "et al." porta già il proprio punto.
            pezzi.append(autori if autori.endswith(".") else f"{autori}.")
        if self.titolo:
            pezzi.append(f"**{self.titolo.rstrip('.')}.**")
        rivista = self.rivista_abbreviata or self.rivista
        if rivista:
            pezzi.append(f"*{rivista.rstrip('.')}.*")
        if riferimento := self._riferimento():
            pezzi.append(riferimento)
        if self.doi:
            pezzi.append(f"doi:{self.doi}")
        return " ".join(pezzi)

    def _riferimento(self) -> str:
        if not self.anno:
            return ""
        testo = str(self.anno)
        if self.volume:
            testo += f";{self.volume}"
            if self.fascicolo:
                testo += f"({self.fascicolo})"
            if self.pagine:
                testo += f":{self.pagine}"
        return testo + "."


# Quota delle parole significative del titolo che devono comparire nella prima pagina.
_QUOTA_PAROLE_DEL_TITOLO = 0.7

# Le parole corte non distinguono un articolo da un altro.
_LUNGHEZZA_PAROLA_SIGNIFICATIVA = 4


def titolo_riconosciuto(titolo: str | None, prima_pagina: str) -> bool:
    """Vero se il titolo che CrossRef attribuisce al DOI compare nella prima pagina del PDF.

    Serve a intercettare l'errore che nessun altro controllo vedrebbe: il DOI viene cercato
    nella prima pagina, e su certi frontespizi lì compare anche il DOI di un commento o di un
    articolo correlato. Se si prendesse quello, la nota uscirebbe con autori, rivista e
    citazione di un altro paper — e tutto il resto sarebbe corretto, quindi niente stonerebbe.

    Il confronto è tollerante: il PDF spezza il titolo su più righe, a volte con la sillabazione,
    e CrossRef può omettere o aggiungere un sottotitolo. Si accetta quindi anche una
    corrispondenza parziale, purché la maggior parte delle parole lunghe del titolo sia lì.
    """
    if not titolo:
        return True  # senza titolo non c'è niente da smentire: lo segnala già `mancanti`
    compatto = _solo_lettere(titolo)
    pagina = _solo_lettere(prima_pagina)
    if compatto and compatto in pagina:
        return True

    parole = [
        p for p in _normalizza(titolo).split("-") if len(p) >= _LUNGHEZZA_PAROLA_SIGNIFICATIVA
    ]
    if not parole:
        return True
    presenti = sum(1 for p in parole if p in pagina)
    return presenti / len(parole) >= _QUOTA_PAROLE_DEL_TITOLO


def _solo_lettere(testo: str) -> str:
    """Lettere e cifre, senza accenti: sopravvive agli a capo e alla sillabazione."""
    return re.sub(r"[^a-z0-9]", "", _normalizza(testo))


def slug(metadati: Metadati, titolo: str, *, parole: int = 4) -> str:
    """Il nome del file della nota: `<anno>-<primo autore>-<parole chiave del titolo>`.

    Il titolo da cui pescare le parole è quello italiano: il file va ritrovato da chi cerca
    nella cartella note, non da un indicizzatore.
    """
    pezzi = []
    if metadati.anno:
        pezzi.append(str(metadati.anno))
    if metadati.autori:
        cognome = metadati.autori[0].split()[0]
        pezzi.append(_normalizza(cognome))
    scelte = [
        p
        for p in _normalizza(titolo).split("-")
        if len(p) >= _LUNGHEZZA_MINIMA_PAROLA and p not in _PAROLE_VUOTE
    ]
    pezzi.extend(scelte[:parole])
    return "-".join(p for p in pezzi if p)


# Lettere che non sono una vocale più un segno diacritico, ma caratteri a sé: la scomposizione
# Unicode non le tocca, e senza questa tabella `Søreide` diventerebbe `s-reide`.
_LETTERE_INTERE = str.maketrans(
    {"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ł": "l", "đ": "d", "ð": "d", "þ": "th",
     "å": "a", "ı": "i"}
)


def _normalizza(testo: str) -> str:
    scomposto = unicodedata.normalize("NFKD", testo.lower().translate(_LETTERE_INTERE))
    senza_accenti = "".join(c for c in scomposto if not unicodedata.combining(c))
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", senza_accenti)).strip("-")



# --------------------------------------------------------------------------------------
# DOI
# --------------------------------------------------------------------------------------


def trova_doi(testo: str) -> str | None:
    """Il DOI dell'articolo, cercato nel testo.

    Va cercato nella **prima pagina**: più avanti il documento è pieno dei DOI delle voci in
    bibliografia, e il primo che si incontra sarebbe quello di un altro articolo.

    Nella barra laterale della prima pagina il DOI va spesso a capo — `doi: 10.1053/` su una
    riga e `j.ajkd.2026.02.646` su quella dopo — e in mezzo, nell'ordine di lettura, ci finisce
    il testo dell'abstract. Il seguito si riconosce perché è l'unica riga vicina fatta di un
    solo pezzo senza spazi.

    Sulla stessa pagina possono convivere più DOI: le riviste MDPI stampano nel colophon anche
    il DOI **della testata** (`10.3390/jpm`) accanto a quello dell'articolo
    (`10.3390/jpm15050207`). Fra i candidati si sceglie quindi il più specifico, non il primo.
    """
    candidati: list[str] = []
    righe = testo.splitlines()
    for numero, riga in enumerate(righe):
        for grezzo in _RE_DOI.findall(riga):
            if doi := _ripulisci_doi(grezzo):
                candidati.append(doi)
        if (troncato := _RE_DOI_TRONCATO.search(riga)) and (
            seguito := _seguito(righe[numero + 1 : numero + 1 + _RIGHE_PER_IL_SEGUITO])
        ):
            if doi := _ripulisci_doi(troncato.group(1) + seguito):
                log.info("DOI ricucito da due righe: %s", doi)
                candidati.append(doi)
    return _piu_specifico(candidati)


def _piu_specifico(candidati: list[str]) -> str | None:
    """Il DOI che identifica l'articolo, fra quelli trovati nella stessa pagina.

    Due criteri, nell'ordine. Un DOI di cui un altro è il prefisso è il più completo dei due:
    `10.3390/jpm` accanto a `10.3390/jpm15050207` è la testata, non l'articolo. E un suffisso
    senza nemmeno una cifra identifica quasi sempre una rivista o una collana: gli articoli
    portano l'anno, il volume o un numero progressivo.
    """
    if not candidati:
        return None
    completi = [d for d in candidati if not any(a != d and a.startswith(d) for a in candidati)]
    con_cifre = [d for d in completi if any(c.isdigit() for c in d.split("/", 1)[1])]
    return (con_cifre or completi)[0]


def _ripulisci_doi(grezzo: str) -> str | None:
    doi = _RE_CONTROLLO.sub("", grezzo).translate(_TRATTINI_TIPOGRAFICI)
    doi = doi.rstrip(_CODA_DA_TOGLIERE)
    # Le parentesi bilanciate fanno parte del DOI, quelle spaiate sono di contorno.
    while doi.count(")") > doi.count("("):
        doi = doi[: doi.rfind(")")]
    return doi if len(doi) > len("10.0000/") else None


def _seguito(righe: list[str]) -> str | None:
    """La prima riga vicina fatta di un solo pezzo senza spazi: il resto di un DOI andato a capo."""
    for riga in righe:
        pezzo = riga.strip()
        if not pezzo:
            continue
        if " " not in pezzo and _RE_SEGUITO_DOI.fullmatch(pezzo):
            return pezzo
    return None


# --------------------------------------------------------------------------------------
# CrossRef
# --------------------------------------------------------------------------------------


def da_crossref(doi: str, *, mailto: str | None = None, timeout: float = 15) -> Metadati | None:
    """Interroga CrossRef. `mailto` non è obbligatorio ma dà accesso alla coda più veloce."""
    intestazioni = {"User-Agent": _user_agent(mailto)}
    try:
        risposta = requests.get(
            CROSSREF.format(doi=requests.utils.quote(doi, safe="")),
            headers=intestazioni,
            params={"mailto": mailto} if mailto else None,
            timeout=timeout,
        )
        risposta.raise_for_status()
        opera = risposta.json()["message"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("CrossRef non ha risposto per %s: %s", doi, exc)
        return None

    rivista = _primo(opera.get("container-title"))
    abbreviata = _abbreviazione(_primo(opera.get("short-container-title")), rivista)
    return Metadati(
        autori=_autori_crossref(opera.get("author") or []),
        titolo=_primo(opera.get("title")),
        rivista=rivista,
        rivista_abbreviata=abbreviata,
        anno=_anno_crossref(opera),
        volume=opera.get("volume"),
        fascicolo=opera.get("issue"),
        pagine=opera.get("page") or opera.get("article-number"),
        # Il DOI da mostrare è quello stampato nel documento: CrossRef lo restituisce tutto
        # minuscolo, e il `CIR` di Circulation diventerebbe `cir`. Sono equivalenti per il
        # resolver, ma la nota deve poter essere confrontata a occhio con la fonte.
        doi=doi,
    )


def _autori_crossref(autori: list[dict]) -> list[str]:
    """Gli autori in forma Vancouver: `{"given": "Biff F.", "family": "Palmer"}` → `Palmer BF`.

    CrossRef mette in testa all'elenco anche gli autori collettivi — le linee guida di società
    cominciano con `Writing Committee Members`. Vanno tenuti solo se non c'è nessuna persona:
    altrimenti il frontmatter attribuirebbe il documento al comitato invece che al primo firmatario.
    """
    persone = [_persona(a) for a in autori if a.get("family")]
    if persone:
        return persone
    return [a["name"].strip() for a in autori if a.get("name")]


def _persona(autore: dict) -> str:
    iniziali = "".join(p[0] for p in re.split(r"[\s.-]+", autore.get("given") or "") if p)
    return f"{autore['family'].strip()} {iniziali}".strip()


def _abbreviazione(breve: str | None, esteso: str | None) -> str | None:
    """L'abbreviazione della rivista, senza i punti che i golden non hanno.

    CrossRef a volte mette in `short-container-title` il nome per esteso: in quel caso non c'è
    abbreviazione, e la si andrà a cercare in PubMed.
    """
    if not breve or breve == esteso:
        return None
    return breve.replace(".", "").strip()


def _anno_crossref(opera: dict) -> int | None:
    """L'anno di pubblicazione, preferendo la stampa all'anticipazione online.

    Un articolo pubblicato online a dicembre e su carta a febbraio ha due anni diversi:
    quello che conta per la citazione è l'anno del fascicolo.
    """
    for chiave in ("published-print", "issued", "published", "published-online"):
        parti = (opera.get(chiave) or {}).get("date-parts") or []
        if parti and parti[0] and parti[0][0]:
            return int(parti[0][0])
    return None


def _primo(valore) -> str | None:
    """Il primo elemento, sciolte le entità HTML.

    CrossRef restituisce i campi già codificati per il web: `Echo Research &amp; Practice`.
    Lasciarle passare mette una stringa da sorgente HTML nel frontmatter di un file markdown.
    """
    if isinstance(valore, list):
        valore = valore[0] if valore else None
    return html.unescape(valore) if valore else None


# --------------------------------------------------------------------------------------
# PubMed
# --------------------------------------------------------------------------------------


def da_pubmed(doi: str, *, timeout: float = 15) -> Metadati | None:
    """Ripiego quando CrossRef non conosce il DOI: cerca il PMID e ne legge il riepilogo."""
    try:
        ricerca = requests.get(
            PUBMED_RICERCA,
            params={"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"},
            timeout=timeout,
        )
        ricerca.raise_for_status()
        identificativi = ricerca.json()["esearchresult"].get("idlist") or []
        if not identificativi:
            log.warning("PubMed non conosce il DOI %s", doi)
            return None

        riepilogo = requests.get(
            PUBMED_RIEPILOGO,
            params={"db": "pubmed", "id": identificativi[0], "retmode": "json"},
            timeout=timeout,
        )
        riepilogo.raise_for_status()
        voce = riepilogo.json()["result"][identificativi[0]]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("PubMed non ha risposto per %s: %s", doi, exc)
        return None

    return Metadati(
        autori=[a["name"] for a in voce.get("authors") or [] if a.get("authtype") == "Author"],
        titolo=(voce.get("title") or "").strip().rstrip("."),
        rivista=voce.get("fulljournalname"),
        rivista_abbreviata=voce.get("source"),
        anno=_anno_pubmed(voce),
        volume=voce.get("volume") or None,
        fascicolo=voce.get("issue") or None,
        pagine=voce.get("pages") or voce.get("elocationid") or None,
        doi=doi,
    )


def _anno_pubmed(voce: dict) -> int | None:
    for chiave in ("pubdate", "epubdate", "sortpubdate"):
        if trovato := re.search(r"\b(19|20)\d{2}\b", str(voce.get(chiave) or "")):
            return int(trovato.group(0))
    return None


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------


def recupera(
    doi: str | None, *, mailto: str | None = None, timeout: float = 15
) -> Metadati:
    """I metadati del documento: CrossRef, poi PubMed, poi niente.

    Non alza eccezioni: un documento senza DOI o con un DOI che nessuno conosce produce
    comunque una nota, con i campi vuoti e l'avvertenza in fondo. Interrompere la pipeline
    perché CrossRef è irraggiungibile significherebbe non avere la nota di un paper che si
    sta leggendo adesso.
    """
    if not doi:
        log.warning("nessun DOI trovato nel documento: metadati bibliografici da completare")
        return Metadati()

    crossref = da_crossref(doi, mailto=mailto, timeout=timeout)
    if crossref and not _da_completare(crossref):
        return crossref

    # Le due fonti si completano a vicenda: CrossRef ha i nomi delle riviste scritti bene,
    # PubMed ha le abbreviazioni della citazione e a volte le pagine che a CrossRef mancano.
    pubmed = da_pubmed(doi, timeout=timeout)
    # Il DOI viene dal documento: è l'unico campo che sopravvive al silenzio di entrambe le
    # fonti, e da solo permette di ritrovare l'articolo a mano.
    metadati = dataclasses.replace(_unisci(crossref, pubmed), doi=doi)
    if metadati.mancanti:
        log.warning(
            "metadati incompleti per %s (%s): la nota li segnalerà da completare a mano",
            doi,
            ", ".join(metadati.mancanti),
        )
    return metadati


def _da_completare(metadati: Metadati) -> list[str]:
    """I campi che giustificano una seconda chiamata. L'abbreviazione serve alla citazione."""
    return metadati.mancanti + [
        nome
        for nome, valore in (("rivista_abbreviata", metadati.rivista_abbreviata),
                             ("pagine", metadati.pagine))
        if not valore
    ]


def _unisci(primario: Metadati | None, secondario: Metadati | None) -> Metadati:
    """Il primo che ha un valore vince, campo per campo."""
    if not primario:
        return secondario or Metadati()
    if not secondario:
        return primario
    valori = {
        campo.name: getattr(primario, campo.name) or getattr(secondario, campo.name)
        for campo in dataclasses.fields(Metadati)
    }
    return Metadati(**valori)


def _user_agent(mailto: str | None) -> str:
    # Le intestazioni HTTP viaggiano in latin-1: niente frecce, niente accenti.
    base = "paper-pipeline/0.1 (medical literature to markdown notes)"
    return f"{base} mailto:{mailto}" if mailto else base
