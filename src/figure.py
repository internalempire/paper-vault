"""Figure citate nei passaggi della fonte usati dalla sintesi.

Il modello seleziona e cita; Python verifica il richiamo e ritaglia l'originale.
I ritagli sono proposte da approvare: nessuna geometria euristica è una prova di
completezza, soprattutto per figure vettoriali, multipannello o su più pagine.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

import pymupdf

NUMERO = r"S?[1-9]\d{0,2}"
CAPTION = re.compile(rf"^(?:Figure|Figura|Fig\.)\s*({NUMERO})(?!\d)[.:\s]", re.I)
RICHIAMO = re.compile(
    rf"\b(?:Figures?|Figure|Figura|Figg?\.?|Figs\.?)\s*"
    rf"({NUMERO}[A-F]?(?:\s*(?:,|and|e|[-–])\s*(?:{NUMERO}[A-F]?))*)", re.I
)
TITOLI = re.compile(r"^#{2,6} (.+)$", re.M)
BLOCCHI = re.compile(
    rf"<!-- paper-pipeline:figura ({NUMERO}) -->\n.*?<!-- /paper-pipeline:figura -->",
    re.S,
)
PENDENTI = "<!-- paper-pipeline:figure-da-verificare -->"


def normalizza(testo: str) -> str:
    testo = unicodedata.normalize("NFKC", testo).replace("\u00ad", "")
    testo = re.sub(r"(?<=\w)-\s*\n(?=\w)", "", testo)
    return re.sub(r"\s+", " ", testo).strip()


def numeri_citati(testo: str) -> set[str]:
    numeri = set()
    for m in RICHIAMO.finditer(testo):
        elenco = m.group(1).upper()
        numeri.update(re.findall(NUMERO, elenco))
        for intervallo in re.finditer(r"(?<!\w)(\d+)\s*[-–]\s*(\d+)", elenco):
            a, b = map(int, intervallo.groups())
            if 0 < b - a <= 30:
                numeri.update(str(n) for n in range(a, b + 1))
    return numeri


def percorso_sicuro(radice: Path, relativo: str) -> Path:
    """Gli allegati non possono uscire dal vault, neanche attraverso symlink."""
    p = PurePosixPath(relativo)
    if not relativo or p.is_absolute() or any(v in ("..", ".") for v in relativo.split("/")) or "\\" in relativo:
        raise ValueError(f"percorso non sicuro: {relativo!r}")
    destinazione = (radice / relativo).resolve()
    if not destinazione.is_relative_to(radice.resolve()):
        raise ValueError(f"percorso fuori dal vault: {relativo!r}")
    return destinazione


def didascalie(pdf: pymupdf.Document) -> dict[str, list[dict]]:
    trovate: dict[str, list[dict]] = {}
    for pagina in pdf:
        for blocco in pagina.get_text("blocks"):
            testo = normalizza(blocco[4])
            m = CAPTION.match(testo)
            if m:
                numero = m.group(1).upper()
                trovate.setdefault(numero, []).append({
                    "pagina_pdf": pagina.number + 1,
                    "didascalia": testo,
                    "bbox_didascalia": list(blocco[:4]),
                })
    return trovate


def proponi_ritaglio(pagina: pymupdf.Page, didascalia: list[float]) -> list[float] | None:
    """Raccoglie la grafica contigua sopra la didascalia; l'anteprima resta obbligatoria.

    Il rendering di pagina preserva testo, vettori, maschere e pannelli che una
    semplice extract_image perderebbe. Sono escluse linee sottili e bordi pagina.
    """
    cap = pymupdf.Rect(didascalia)
    oggetti = [pymupdf.Rect(i["bbox"]) for i in pagina.get_image_info()]
    oggetti += [pymupdf.Rect(d["rect"]) for d in pagina.get_drawings()]
    oggetti = [r for r in oggetti if r.width > 3 and r.height > 3 and r.get_area() > 60
               and r.y1 <= cap.y0 - 2 and r.y0 > 45
               and min(r.x1, cap.x1) - max(r.x0, cap.x0) > 0]
    if not oggetti:
        return None
    seme = min(oggetti, key=lambda r: cap.y0 - r.y1)
    if cap.y0 - seme.y1 > 100:
        return None
    insieme = pymupdf.Rect(seme)
    rimasti = list(oggetti)
    while True:
        vicini = [r for r in rimasti if (insieme + (-85, -85, 85, 85)).intersects(r)]
        if not vicini:
            break
        for r in vicini:
            insieme |= r
            rimasti.remove(r)
    # Le scritte dei pannelli sono spesso oggetti separati. Le aggiungiamo intorno
    # alla grafica, fermandoci alla prosa precedente: una catena di piccoli blocchi
    # di testo non deve trascinare nel ritaglio metà articolo.
    testi = []
    limite = 45.0
    serif = re.compile(r"Times|Joanna|Minion|Garamond|Palatino|Georgia|Cambria|Roman", re.I)
    for b in pagina.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        r = pymupdf.Rect(b["bbox"])
        if r.y0 <= 45 or r.y1 > cap.y0 - 2 or min(r.x1, cap.x1) <= max(r.x0, cap.x0):
            continue
        spans = [s for line in b["lines"] for s in line["spans"]]
        testo = normalizza(" ".join(s["text"] for s in spans))
        prosa = len(testo) > 260 or any(serif.search(s["font"]) for s in spans)
        if prosa and r.y1 <= insieme.y0:
            limite = max(limite, r.y1 + 8)
        if not prosa:
            testi.append(r)
    bordo_superiore = max(limite, insieme.y0 - 110)
    for r in testi:
        if r.y0 >= bordo_superiore and r.y0 <= insieme.y1 + 40:
            insieme |= r
    # Mantiene tutta la larghezza della didascalia per etichette e pannelli laterali.
    insieme = pymupdf.Rect(min(cap.x0, insieme.x0), insieme.y0 - 5,
                          max(cap.x1, insieme.x1), cap.y0 - 2) & pagina.rect
    return list(insieme) if insieme.width > 60 and insieme.height > 40 else None


def salva_ritaglio(pdf: pymupdf.Document, figura: dict, radice: Path, slug: str,
                   pagina: int, bbox: list[float], dpi: int = 180) -> None:
    if not 1 <= pagina <= len(pdf) or len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise ValueError("pagina o coordinate del ritaglio non valide")
    p = pdf[pagina - 1]
    rect = pymupdf.Rect(bbox)
    if rect.is_empty or not p.rect.contains(rect) or rect.width < 10 or rect.height < 10:
        raise ValueError("il ritaglio deve essere contenuto nella pagina e non vuoto")
    dati = p.get_pixmap(clip=rect, dpi=dpi, alpha=False, annots=False).tobytes("png")
    digest = hashlib.sha256(dati).hexdigest()
    nome = f"assets/{slug}/figura-{figura['numero'].lower()}-{digest[:16]}.png"
    percorso = percorso_sicuro(radice, nome)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    percorso.write_bytes(dati)
    figura.update(percorso=nome, sha256=digest, pagina_pdf=pagina, bbox=list(rect),
                  dpi=dpi, stato="da_verificare")


def prepara(nota: dict, documento, markdown: str, radice: Path, slug: str) -> tuple[list[dict], list[str]]:
    """Solo richiami provati nel testo sorgente, associati a sezioni presenti nella nota."""
    richieste = nota.get("figure_richieste") or []
    if not richieste:
        return [], []
    titoli = TITOLI.findall(markdown)
    figure: list[dict] = []
    avvisi: list[str] = []
    fonte = normalizza((documento.sorgente_web or {}).get("testo_corpo", documento.document_text(marcatori=False)))
    pdf = pymupdf.open(documento.percorso) if documento.formato == "pdf" else None
    try:
        catalogo = didascalie(pdf) if pdf is not None else (documento.sorgente_web or {}).get("figure", {})
        for richiesta in richieste:
            numero = richiesta["numero"].upper()
            if any(g["numero"] == numero for g in figure):
                avvisi.append(f"Figura {numero}: richiamo duplicato, mantenuta la prima collocazione")
                continue
            f = {**richiesta, "numero": numero, "stato": "non_disponibile"}
            figure.append(f)
            citazione = normalizza(richiesta["passaggio_fonte"])
            motivo = ""
            if len(citazione) < 25 or citazione not in fonte or numero not in numeri_citati(citazione):
                motivo = "passaggio della fonte o richiamo alla figura non verificato"
            elif CAPTION.match(citazione):
                motivo = "serve un passaggio del corpo dell'articolo, non la sola didascalia"
            elif titoli.count(richiesta["sezione_nota"]) != 1 or richiesta["sezione_nota"] == "Riferimento bibliografico":
                motivo = "sezione della sintesi assente o ambigua"
            else:
                f["selezione_verificata"] = True
                if pdf is None and documento.sorgente_web is None:
                    motivo = "HTML locale senza catalogo immagini: fornire l’URL del full-text"
                elif len(catalogo.get(numero, [])) != 1:
                    motivo = "didascalia non trovata o figura su più blocchi/pagine: selezione manuale necessaria"
                elif pdf is None:
                    from src.web_article import salva_immagine
                    f.update(catalogo[numero][0])
                    try:
                        salva_immagine(f, radice, slug)
                    except (ValueError, RuntimeError) as exc:
                        motivo = f"immagine non recuperata: {exc}"
                else:
                    f.update(catalogo[numero][0])
                    bbox = proponi_ritaglio(pdf[f["pagina_pdf"] - 1], f["bbox_didascalia"])
                    if bbox is None:
                        motivo = "area della figura non riconosciuta: indicare un ritaglio manuale"
                    else:
                        try:
                            salva_ritaglio(pdf, f, radice, slug, f["pagina_pdf"], bbox)
                        except (ValueError, RuntimeError) as exc:
                            motivo = f"ritaglio non riuscito: {exc}"
            if motivo:
                f["problema"] = motivo
                avvisi.append(f"Figura {numero}: {motivo}")
        return figure, avvisi
    finally:
        if pdf is not None:
            pdf.close()


def inserisci(testo: str, titolo: str, blocco: str) -> str:
    headings = list(TITOLI.finditer(testo))
    corrispondenze = [i for i, h in enumerate(headings) if h.group(1) == titolo]
    if len(corrispondenze) != 1:
        raise ValueError(f"sezione «{titolo}» assente o ambigua: inserire la figura manualmente")
    i = corrispondenze[0]
    fine = headings[i + 1].start() if i + 1 < len(headings) else len(testo)
    return testo[:fine].rstrip() + "\n\n" + blocco + "\n\n" + testo[fine:]


def aggiungi_al_markdown(testo: str, figure: list[dict]) -> str:
    for f in figure:
        if f["stato"] != "approvata":
            continue
        numero = f["numero"]
        percorso_sicuro(Path("."), f["percorso"])
        # La didascalia è testo originale, non istruzioni markdown/HTML.
        didascalia = html.escape(f.get("didascalia", f"Figura {numero}"))
        didascalia = re.sub(r"([\\`*\[\]_])", r"\\\1", didascalia)
        origine = (f"pagina PDF {f['pagina_pdf']}" if "pagina_pdf" in f else "dal full-text web")
        blocco = (f"<!-- paper-pipeline:figura {numero} -->\n"
                  f"![Figura {numero}]({f['percorso']})\n\n"
                  f"{didascalia}\n\n*Figura originale, {origine}.*\n"
                  "<!-- /paper-pipeline:figura -->")
        testo = inserisci(testo, f["sezione_nota"], blocco)
    if any(f["stato"] not in ("approvata", "esclusa") for f in figure):
        testo = testo.rstrip() + "\n\n" + PENDENTI + "\n"
    return testo


def fondi_figure(remoto: str, locale: str) -> str:
    """Aggiunge figure nuove conservando testo e blocchi già presenti sul server."""
    esistenti = {m.group(1) for m in BLOCCHI.finditer(remoto)}
    for m in BLOCCHI.finditer(locale):
        if m.group(1) in esistenti:
            continue
        precedenti = list(TITOLI.finditer(locale[:m.start()]))
        if not precedenti:
            raise ValueError("figura senza sezione di destinazione")
        remoto = inserisci(remoto, precedenti[-1].group(1), m.group(0))
        esistenti.add(m.group(1))
    return remoto


def anteprima(contenuto: dict, radice: Path, destinazione: Path) -> Path:
    schede = []
    esc = html.escape
    for f in contenuto.get("figure", []):
        img = ""
        if f.get("percorso"):
            p = percorso_sicuro(radice, f["percorso"])
            src = esc(os.path.relpath(p, destinazione.parent), quote=True)
            img = f'<a href="{src}"><img src="{src}" alt="Figura {esc(f["numero"])}"></a>'
        schede.append(f'<article><h2>Figura {esc(f["numero"])} — {esc(f["stato"])}</h2>'
                      f'<p>Sezione: {esc(f["sezione_nota"])}</p>'
                      f'<blockquote>{esc(f["passaggio_fonte"])}</blockquote>{img}'
                      f'<p>{esc(f.get("didascalia", ""))}</p>'
                      f'<p>{esc(f.get("problema", ""))}</p></article>')
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    destinazione.write_text('<!doctype html><html lang="it"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Verifica figure</title>'
        '<style>body{font:18px/1.5 system-ui;max-width:1000px;margin:32px auto;padding:0 20px}'
        'img{max-width:100%;height:auto}article{border-top:1px solid #bbb;margin-top:36px}'
        'blockquote{border-left:3px solid #888;padding-left:20px}</style>'
        '<h1>Figure della sintesi operativa</h1><p>Verifica tutti i pannelli, le etichette e la '
        'corrispondenza con il passaggio citato. Apri le immagini per ingrandirle.</p>'
        + ''.join(schede) + '</html>', encoding="utf-8")
    return destinazione


def main(argv: list[str] | None = None) -> int:
    from src.ingest import Configurazione
    from src.render import render_da_file, salva

    parser = argparse.ArgumentParser(description="Controlla e approva le figure senza chiamate LLM.")
    parser.add_argument("json", type=Path)
    parser.add_argument("--note", type=Path, help="cartella note e assets (default: configurazione)")
    parser.add_argument("--approva", nargs="+", default=[], metavar="NUMERO")
    parser.add_argument("--escludi", nargs="+", default=[], metavar="NUMERO")
    parser.add_argument("--ritaglio", nargs=6, metavar=("NUMERO", "PAGINA_PDF", "X0", "Y0", "X1", "Y1"))
    parser.add_argument("--riprova", action="store_true", help="riprova le figure web non disponibili dalla fonte salvata, senza LLM")
    args = parser.parse_args(argv)
    root = (args.note or Configurazione.carica().note).expanduser()
    contenuto = json.loads(args.json.read_text(encoding="utf-8"))
    figure = contenuto.get("figure", [])
    if not figure:
        print("Nessuna figura selezionata in questo JSON. Le note precedenti non vengono rielaborate.")
        return 0
    if args.riprova:
        if contenuto.get("sorgente", {}).get("tipo") != "web":
            parser.error("--riprova richiede una fonte web salvata nel JSON")
        from src.web_article import da_snapshot
        da_riprovare = [f for f in figure if f["stato"] == "non_disponibile"]
        if da_riprovare:
            nota = {**contenuto["nota"], "figure_richieste": [{k: f[k] for k in ("numero", "passaggio_fonte", "sezione_nota")} for f in da_riprovare]}
            nuove, avvisi = prepara(nota, da_snapshot(contenuto["sorgente"]),
                                     render_da_file(args.json), root, args.json.stem)
            sostituzioni = {f["numero"]: f for f in nuove}
            figure[:] = [sostituzioni.get(f["numero"], f) for f in figure]
            for avviso in avvisi:
                print(avviso)
    per_numero = {f["numero"]: f for f in figure}
    richiesti = set(args.approva + args.escludi + ([args.ritaglio[0]] if args.ritaglio else []))
    if richiesti - per_numero.keys():
        parser.error("numero figura non presente nel JSON")
    if set(args.approva) & set(args.escludi):
        parser.error("una figura non può essere approvata ed esclusa insieme")
    if args.ritaglio:
        if contenuto.get("sorgente", {}).get("tipo") == "web":
            parser.error("il ritaglio manuale è disponibile solo per PDF")
        n, pagina, *coordinate = args.ritaglio
        if not per_numero[n].get("selezione_verificata"):
            parser.error("il richiamo nella fonte non è stato verificato: correggere la selezione prima del ritaglio")
        with pymupdf.open(contenuto["pdf"]) as pdf:
            salva_ritaglio(pdf, per_numero[n], root, args.json.stem, int(pagina), list(map(float, coordinate)))
        per_numero[n].pop("problema", None)
    for n in args.approva:
        f = per_numero[n]
        if not f.get("selezione_verificata") or f["stato"] not in ("da_verificare", "approvata") or not f.get("percorso"):
            parser.error(f"Figura {n}: manca un’immagine verificabile")
        dati = percorso_sicuro(root, f["percorso"]).read_bytes()
        if hashlib.sha256(dati).hexdigest() != f["sha256"]:
            parser.error(f"Figura {n}: il file è cambiato rispetto all'estrazione")
        f["stato"] = "approvata"
    for n in args.escludi:
        per_numero[n]["stato"] = "esclusa"
    if args.approva or args.escludi or args.ritaglio or args.riprova:
        salva(args.json, contenuto)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{args.json.stem}.md").write_text(render_da_file(args.json), encoding="utf-8")
    p = anteprima(contenuto, root, root / ".anteprime" / f"{args.json.stem}.html")
    print(f"Anteprima: {p}")
    print("; ".join(f"Figura {f['numero']}: {f['stato']}" for f in figure))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
