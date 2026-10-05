"""Articoli HTML pubblici: testo, provenienza e catalogo di figure originali.

Nessun browser, esecuzione JavaScript, cookie di accesso o aggiramento di paywall.
Il catalogo descrive le immagini; il download avviene solo dopo la selezione LLM.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import io
import re
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image
import requests
from bs4 import BeautifulSoup

MAX_HTML = 15 * 1024 * 1024
MAX_IMMAGINE = 25 * 1024 * 1024
TIMEOUT = 30
LABEL = re.compile(r"^(?:Figures?|Figura|Fig\.?)\s*(S?[1-9]\d{0,2})(?![\dA-Za-z])", re.I)
CONTENITORI = "figure, .fig, .figure, .Fig, .ArticleFigure"
DIDASCALIE = "figcaption, .caption, .fig-caption, .figure-caption, .Caption"
SEZIONI_ESCLUSE = re.compile(
    r"^(?:abstract|summary|references|bibliography|bibliografia|keywords|"
    r"related articles|similar articles|cited by|author information|"
    r"acknowledg|funding|conflict|copyright|supplementary|data availability)", re.I
)


def e_url(valore: str | Path) -> bool:
    return str(valore).lower().startswith(("https://", "http://"))


def _valida_url(url: str) -> None:
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("serve un URL HTTP(S) senza credenziali")
    # Anche le immagini e i redirect provengono da HTML non fidato: niente rete privata.
    try:
        indirizzi = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("impossibile risolvere il dominio della fonte") from exc
    if not indirizzi or any(not ipaddress.ip_address(a[4][0]).is_global for a in indirizzi):
        raise ValueError("sono consentite soltanto fonti web su indirizzi pubblici")


def scarica(url: str, *, limite: int, tipi: tuple[str, ...]) -> tuple[bytes, str, str]:
    """GET limitata, TLS verificato, redirect controllati, nessuna credenziale del vault."""
    iniziata = time.monotonic()
    try:
        with requests.Session() as session:
            # Non ereditare cookie, .netrc, autenticazioni o proxy personali.
            session.trust_env = False
            for _ in range(6):
                _valida_url(url)
                with session.get(url, timeout=(10, TIMEOUT), stream=True, allow_redirects=False,
                                  headers={"User-Agent": "paper-vault/1.0 (article reader)"}) as risposta:
                    if risposta.status_code in (301, 302, 303, 307, 308):
                        destinazione = urljoin(url, risposta.headers.get("Location", ""))
                        if urlsplit(url).scheme == "https" and urlsplit(destinazione).scheme != "https":
                            raise ValueError("redirect da HTTPS a una connessione non protetta")
                        url = destinazione
                        continue
                    risposta.raise_for_status()
                    tipo = risposta.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                    if tipo not in tipi:
                        raise ValueError(f"contenuto {tipo or 'senza tipo'} non supportato; serve il full-text HTML o una figura originale")
                    dati = bytearray()
                    for blocco in risposta.iter_content(64 * 1024):
                        if time.monotonic() - iniziata > 90:
                            raise ValueError("tempo massimo di download superato")
                        dati.extend(blocco)
                        if len(dati) > limite:
                            raise ValueError("la risorsa supera la dimensione massima consentita")
                    return bytes(dati), url, tipo
    except requests.RequestException as exc:
        raise ValueError("download non riuscito: verificare URL, accesso pubblico e connessione; in alternativa usare il PDF") from exc
    raise ValueError("troppi redirect nella fonte web")


def _url_immagini(contenitore, base: str) -> list[str]:
    immagini = contenitore.select("img")
    if len(immagini) != 1:
        return []  # non pubblicare solo il primo pannello di una figura composta
    img = immagini[0]
    candidati = []
    for link in contenitore.select("a[href]"):
        if re.search(r"\.(?:png|jpe?g|webp)(?:[?#]|$)", link["href"], re.I):
            candidati.append(link["href"])
    candidati += [img.get(a, "") for a in ("data-original", "data-hi-res", "data-full", "data-src")]
    # Alcuni editori offrono una sorgente originale senza media query oltre alle miniature.
    for source in contenitore.select("picture source[srcset]"):
        if not source.get("media") and len(source["srcset"].split()) == 1:
            candidati.append(source["srcset"])
    for tag in [img, *contenitore.select("picture source")]:
        for attributo in ("data-srcset", "srcset"):
            opzioni = []
            for voce in tag.get(attributo, "").split(","):
                m = re.fullmatch(r"\s*(\S+)\s+(\d+(?:\.\d+)?)[wx]\s*", voce)
                if m:
                    opzioni.append((float(m[2]), m[1]))
            candidati += [u for _, u in sorted(opzioni, reverse=True)]
    candidati.append(img.get("src", ""))
    return list(dict.fromkeys(urljoin(base, u) for u in candidati if u and urlsplit(urljoin(base, u)).scheme in ("http", "https")))


def catalogo_figure(root, base: str) -> dict[str, list[dict]]:
    risultato: dict[str, list[dict]] = {}
    contenitori = root.select(CONTENITORI)
    ids = {id(c) for c in contenitori}
    for contenitore in contenitori:
        # Solo il contenitore esterno: evita duplicati .ArticleFigure > button > figure.
        if any(id(p) in ids for p in contenitore.parents if p is not root):
            continue
        caption = contenitore.select_one(DIDASCALIE)
        if caption is None:
            continue
        testo = caption.get_text(" ", strip=True)
        etichetta = LABEL.match(testo) or LABEL.match(contenitore.get_text(" ", strip=True))
        ident = re.fullmatch(r"(?:f|fig|figure)[-_]?(S?[1-9]\d{0,2})", contenitore.get("id", ""), re.I)
        if not etichetta and not ident:
            continue
        numero = (etichetta or ident)[1].upper()
        urls = _url_immagini(contenitore, base)
        voce = {"numero": numero, "didascalia": testo, "url_pagina": base, "url_immagini": urls}
        if not urls:
            voce["problema"] = "figura senza immagine singola scaricabile (pannelli separati o formato non supportato)"
        if voce not in risultato.setdefault(numero, []):
            risultato[numero].append(voce)
    return risultato


def da_html(dati: bytes | str, url: str, *, originale: str | None = None):
    from src.extract import _HtmlInTesto
    from src.metadata import trova_doi

    soup = BeautifulSoup(dati, "html.parser")
    titolo = soup.find("meta", attrs={"name": "citation_title"})
    titolo = titolo.get("content", "") if titolo else ""
    doi = None
    for meta in soup.find_all("meta"):
        if str(meta.get("name", meta.get("property", ""))).lower() in ("citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi"):
            doi = trova_doi(meta.get("content", ""))
            if doi:
                break
    for inutile in soup.select("script, style, noscript, nav, footer, aside, form, [hidden], [aria-hidden='true']"):
        inutile.decompose()
    root = None
    # Il primo gruppo strutturale valido prevale sui contenitori generici della pagina.
    for selector in ("[itemprop='articleBody'], .article-body, .article__body, .articleBody, .ArticleContent, #artText, #article-body",
                     "article", "main", "body"):
        candidati = soup.select(selector)
        if candidati:
            candidato = max(candidati, key=lambda e: len(e.get_text()))
            if len(candidato.get_text()) >= 1500:
                root = candidato
                break
    if root is None:
        raise ValueError("full-text non riconosciuto: pagina troppo breve, abstract, accesso richiesto o contenuti JavaScript; usare il PDF o HTML completo")
    parser = _HtmlInTesto()
    parser.feed(str(root))
    testo = parser.testo()
    # Un abstract anche molto lungo non è il full-text. Richiediamo prosa sotto
    # sezioni diverse da abstract/bibliografia; è un controllo prudenziale, non universale.
    sezioni = re.split(r"(?m)^(#{1,6}) +(.+)$", testo)
    corpo = [sezioni[i + 2] for i in range(1, len(sezioni) - 2, 3)
             if not (i == 1 and sezioni[i] == "#")
             and not SEZIONI_ESCLUSE.match(re.sub(r"^[\d.\s]+", "", sezioni[i + 1]).strip())]
    if sum(len(p.strip()) for p in corpo) < 1500 or len(root.select("p")) < 3:
        raise ValueError("full-text non riconosciuto: manca un corpo dell'articolo strutturato oltre all'abstract; usare il PDF")
    if titolo and titolo.lower() not in testo[:1000].lower():
        testo = f"# {titolo}\n\n{testo}"
    figure = catalogo_figure(root, url)
    # La citazione che giustifica una figura deve venire dalla prosa, non dalla
    # didascalia stessa o dalla bibliografia.
    copia = BeautifulSoup(str(root), "html.parser")
    for tag in copia.select(CONTENITORI + ", .ref-list, #references, .references"):
        tag.decompose()
    prosa = _HtmlInTesto()
    prosa.feed(str(copia))
    parti = re.split(r"(?m)^(#{1,6} +.+)$", prosa.testo())
    testo_corpo = parti[0] + "\n" + "\n".join(
        parti[i] + "\n" + parti[i + 1] for i in range(1, len(parti) - 1, 2)
        if not re.search(r"^#+ +(?:[\d. ]+)?(?:References|Bibliography|Bibliografia)\b", parti[i], re.I))
    raw = dati.encode("utf-8") if isinstance(dati, str) else dati
    sorgente = {"tipo": "web", "url": originale or url, "url_finale": url,
                "recuperata_il": dt.datetime.now(dt.timezone.utc).isoformat(),
                "sha256_html": hashlib.sha256(raw).hexdigest(), "testo_estratto": testo,
                "doi": doi, "figure": figure, "n_tabelle": parser.n_tabelle, "testo_corpo": testo_corpo}
    return da_snapshot(sorgente)


def da_snapshot(sorgente: dict):
    from src.extract import Documento, Pagina
    testo = sorgente["testo_estratto"]
    return Documento(percorso=Path("articolo-web.html"), formato="html",
                     pagine=[Pagina(0, testo, n_tabelle=sorgente.get("n_tabelle", 0))],
                     toc=[(len(m[1]), m[2], 0) for m in re.finditer(r"(?m)^(#{1,6}) +(.+)$", testo)],
                     sorgente_web=sorgente)


def estrai(url: str):
    dati, finale, _ = scarica(url, limite=MAX_HTML, tipi=("text/html", "application/xhtml+xml"))
    return da_html(dati, finale, originale=url)


def salva_immagine(figura: dict, radice: Path, slug: str) -> None:
    """Conserva i byte originali, verifica il formato reale e registra l'impronta."""
    from src.figure import percorso_sicuro
    errori = []
    for url in figura.get("url_immagini", [])[:5]:
        try:
            dati, finale, mime = scarica(url, limite=MAX_IMMAGINE,
                                       tipi=("image/png", "image/jpeg", "image/webp", "application/octet-stream"))
            if dati.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = "png"
            elif dati.startswith(b"\xff\xd8\xff"):
                ext = "jpg"
            elif dati.startswith(b"RIFF") and dati[8:12] == b"WEBP":
                ext = "webp"
            else:
                raise ValueError("la risorsa non è un'immagine PNG, JPEG o WebP")
            try:
                with Image.open(io.BytesIO(dati)) as immagine:
                    width, height = immagine.size
                    if min(width, height) < 40 or width * height > 50_000_000:
                        raise ValueError("dimensioni immagine fuori dai limiti (40 px–50 megapixel)")
                    if getattr(immagine, "n_frames", 1) != 1:
                        raise ValueError("immagine animata non supportata: non perdere i fotogrammi")
                    immagine.load()
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError("immagine illeggibile o troppo grande") from exc
            digest = hashlib.sha256(dati).hexdigest()
            nome = f"assets/{slug}/figura-{figura['numero'].lower()}-{digest[:16]}.{ext}"
            p = percorso_sicuro(radice, nome)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(dati)
            figura.update(percorso=nome, sha256=digest, url_immagine=finale,
                          larghezza=width, altezza=height, stato="da_verificare")
            figura.pop("problema", None)
            return
        except (ValueError, RuntimeError) as exc:
            errori.append(str(exc))
    raise ValueError(errori[-1] if errori else figura.get("problema", "nessuna immagine scaricabile"))
