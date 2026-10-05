"""Interfaccia personale locale: fonte -> testo -> nota/figure -> pubblicazione.

Avvio: python -m src.webapp --open. Ascolta soltanto su 127.0.0.1.
I lavori persistono sotto note/.webapp; nessuna chiamata LLM su upload o refresh.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

import dotenv
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from markdown_it import MarkdownIt
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from src import figure, render
from src.extract import Documento, Pagina
from src.ingest import Configurazione, Esito, _gia_elaborato, ingest, prepara_ingresso
from src.metadata import Metadati
from src.pubblica import Vault, pubblica_file
from src.indice import pubblica_indice

RADICE = Path(__file__).resolve().parents[1]
BUSY = {"estrazione", "elaborazione", "pubblicazione", "recupero_figure"}
log = logging.getLogger(__name__)


def scrivi_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def leggi_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ripristina_preparato(data: dict) -> Esito:
    doc = data["documento"]
    doc["percorso"] = Path(doc["percorso"])
    doc["pagine"] = [Pagina(**p) for p in doc["pagine"]]
    return Esito(slug="", documento=Documento(**doc), metadati=Metadati(**data["metadati"]),
                 avvisi=data["avvisi"], documento_sorgente=data["documento_sorgente"])


class Lavori:
    def __init__(self, conf, root=None, *, estrattore=None, vault_factory=None):
        self.conf = conf
        self.root = Path(root or conf.note / ".webapp")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.estrattore = estrattore
        self.vault_factory = vault_factory or Vault.da_ambiente
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paper-vault")
        self.futures = {}
        for path in self.root.glob("*/job.json"):
            job = leggi_json(path)
            if job["stato"] in BUSY:
                job.update(stato="interrotto", errore="Il servizio è stato riavviato. Il lavoro salvato è disponibile; riprendi dal passaggio interrotto.")
                self.salva(job)

    def cartella(self, id):
        if not re.fullmatch(r"[0-9a-f]{32}", id):
            raise ValueError("Lavoro non valido.")
        return self.root / id

    def leggi(self, id):
        path = self.cartella(id) / "job.json"
        if not path.is_file():
            raise ValueError("Lavoro non trovato.")
        return leggi_json(path)

    def salva(self, job):
        job["aggiornato"] = dt.datetime.now(dt.timezone.utc).isoformat()
        scrivi_json(self.cartella(job["id"]) / "job.json", job)

    def config_lavoro(self, id):
        note = self.cartella(id) / "note"
        return dataclasses.replace(self.conf, note=note, json=note / ".json")

    def elenco(self):
        return sorted([leggi_json(p) for p in self.root.glob("*/job.json")], key=lambda j: j["creato"], reverse=True)

    def nuovo(self, fonte, nome, upload=None):
        with self.lock:
            id = uuid.uuid4().hex
            folder = self.cartella(id)
            folder.mkdir(mode=0o700)
            if upload is not None:
                p = folder / "fonte.pdf"
                upload.save(p)
                with p.open("rb") as stream:
                    intestazione = stream.read(1024)
                if p.stat().st_size > 100 * 1024 * 1024 or b"%PDF-" not in intestazione:
                    p.unlink()
                    raise ValueError("Scegli un PDF valido, fino a 100 MB.")
                fonte = str(p)
            elif not isinstance(fonte, str) or not fonte.startswith(("https://", "http://")):
                raise ValueError("Incolla l’indirizzo HTTP(S) della pagina con il full-text.")
            job = dict(id=id, nome=nome[:240], fonte=fonte, stato="nuovo", errore=None,
                       creato=dt.datetime.now(dt.timezone.utc).isoformat(), slug=None, avvisi=[])
            self.salva(job)
            self.avvia(id, "estrai")
            return self.leggi(id)

    def avvia(self, id, azione, *, sostituisci=False):
        with self.lock:
            job = self.leggi(id)
            if job["stato"] in BUSY:
                raise ValueError("Questo passaggio è già in corso.")
            folder = self.cartella(id)
            if azione == "estrai":
                if job.get("slug"):
                    raise ValueError("La nota è già pronta: riprendi la revisione.")
                stato, lavoro = "estrazione", self._estrai
            elif azione == "genera":
                if not (folder / "preparato.json").exists():
                    raise ValueError("Controlla prima il testo estratto.")
                if job.get("slug") or list(self.config_lavoro(id).json.glob("*.json")):
                    # Rientro dopo crash: JSON pagato recuperato anche senza DOI.
                    stato, lavoro = "elaborazione", self._recupera
                elif job["stato"] not in ("testo_pronto", "errore", "interrotto"):
                    raise ValueError("La fonte non è pronta per l’elaborazione.")
                else:
                    stato, lavoro = "elaborazione", self._genera
            elif azione == "pubblica":
                if job["stato"] not in ("revisione", "errore", "interrotto", "conflitto", "pubblicato") or not job.get("slug"):
                    raise ValueError("La nota non è pronta.")
                if not self.pronto(job):
                    raise ValueError("Approva o escludi tutte le figure prima di pubblicare.")
                stato = "pubblicazione"
                lavoro = lambda j: self._pubblica(j, sostituisci=sostituisci)
            elif azione == "riprova_figure":
                if not job.get("slug") or job["stato"] == "pubblicato":
                    raise ValueError("Nessuna figura da recuperare.")
                stato, lavoro = "recupero_figure", self._riprova_figure
            else:
                raise ValueError("Azione non disponibile.")
            job.update(stato=stato, errore=None, ultima_azione=azione)
            self.salva(job)
            self.futures[id] = self.worker.submit(self._esegui, id, lavoro)

    def _esegui(self, id, funzione):
        try:
            funzione(self.leggi(id))
        except Exception as exc:
            log.error("Lavoro %s: %s", id, type(exc).__name__)
            with self.lock:
                job = self.leggi(id)
                # Gli errori SDK possono contenere intestazioni o dati della richiesta.
                errore = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "Il passaggio non è riuscito. Controlla connessione e configurazione e riprova: i file già salvati restano disponibili."
                job.update(stato="errore", errore=errore[:1200])
                self.salva(job)

    def _estrai(self, job):
        esito = prepara_ingresso(job["fonte"], self.conf)
        scrivi_json(self.cartella(job["id"]) / "preparato.json", dataclasses.asdict(esito))
        cache = _gia_elaborato(self.conf.json, esito.metadati.doi)
        job.update(stato="testo_pronto", avvisi=esito.avvisi, gia_elaborato=bool(cache))
        self.salva(job)

    def _genera(self, job):
        id = job["id"]
        conf = self.config_lavoro(id)
        preparato = ripristina_preparato(leggi_json(self.cartella(id) / "preparato.json"))
        cache = _gia_elaborato(self.conf.json, preparato.metadati.doi)
        if cache:
            conf.json.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache, conf.json / cache.name)
            for f in leggi_json(cache).get("figure", []):
                if f.get("percorso"):
                    sorgente = figure.percorso_sicuro(self.conf.note, f["percorso"])
                    if sorgente.exists():
                        dest = figure.percorso_sicuro(conf.note, f["percorso"])
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sorgente, dest)
        esito = ingest(job["fonte"], conf, preparato=preparato, estrattore=self.estrattore)
        job.update(slug=esito.slug, stato="revisione", avvisi=esito.avvisi, uso=str(esito.uso))
        self.salva(job)

    def _recupera(self, job):
        conf = self.config_lavoro(job["id"])
        files = sorted(conf.json.glob("*.json"))
        if not files:
            raise ValueError("Non trovo il JSON della nota. Il lavoro non verrà rielaborato automaticamente.")
        job["slug"] = files[0].stem
        data = leggi_json(files[0])
        if data["nota"].get("figure_richieste") and "figure" not in data:
            from src.ingest import _prepara_figure
            esito = ripristina_preparato(leggi_json(self.cartella(job["id"]) / "preparato.json"))
            esito.slug, esito.json = job["slug"], files[0]
            _prepara_figure(esito, data, conf)
        self._rigenera(job)
        job.update(stato="revisione", errore=None)
        self.salva(job)

    def paths(self, job):
        conf = self.config_lavoro(job["id"])
        slug = job.get("slug")
        if not slug or Path(slug).name != slug:
            raise ValueError("La nota non è ancora disponibile.")
        return conf.json / f"{slug}.json", conf.note / f"{slug}.md"

    def _rigenera(self, job):
        j, md = self.paths(job)
        md.write_text(render.render_da_file(j), encoding="utf-8")

    def pronto(self, job):
        j, md = self.paths(job)
        data = leggi_json(j)
        if data["nota"].get("figure_richieste") and "figure" not in data:
            return False
        return md.is_file() and all(f["stato"] in ("approvata", "esclusa") for f in data.get("figure", []))

    def decidi(self, id, numero, scelta):
        with self.lock:
            job = self.leggi(id)
            if job["stato"] in BUSY or job["stato"] == "pubblicato":
                raise ValueError("Attendi la fine del passaggio in corso.")
            if scelta not in ("approvata", "esclusa", "da_verificare"):
                raise ValueError("Scelta non valida.")
            j, md = self.paths(job)
            data = leggi_json(j)
            f = next((f for f in data.get("figure", []) if f["numero"] == numero), None)
            if not f:
                raise ValueError("Figura non trovata.")
            if scelta in ("approvata", "da_verificare"):
                if not f.get("selezione_verificata") or not f.get("percorso"):
                    raise ValueError("Manca un’immagine verificabile. Puoi escluderla oppure riprovare il recupero.")
                img = figure.percorso_sicuro(self.config_lavoro(id).note, f["percorso"])
                if not img.exists() or hashlib.sha256(img.read_bytes()).hexdigest() != f.get("sha256"):
                    raise ValueError("L’immagine è assente o cambiata rispetto all’estrazione.")
            f["stato"] = scelta
            scrivi_json(j, data)
            self._rigenera(job)
            job.update(stato="revisione", errore=None)
            self.salva(job)

    def _riprova_figure(self, job):
        j, md = self.paths(job)
        if leggi_json(j).get("sorgente", {}).get("tipo") != "web":
            raise ValueError("Il recupero automatico è disponibile per le figure web. Per un ritaglio PDF errato, escludi la figura o correggila con il comando src.figure.")
        figure.main([str(j), "--note", str(md.parent), "--riprova"])
        job.update(stato="revisione")
        self.salva(job)

    def _pubblica(self, job, *, sostituisci):
        j, md = self.paths(job)
        vault = self.vault_factory()
        testo = md.read_text(encoding="utf-8")
        remoto = vault.leggi(job["slug"])
        if remoto is not None and remoto != testo and not sostituisci:
            job.update(stato="conflitto", errore="Sul vault esiste già una versione diversa. Puoi aprirla in SilverBullet o sostituirla esplicitamente con questa nota.")
            self.salva(job)
            return
        # Backup di ciò che viene sostituito, comprese eventuali correzioni manuali.
        if remoto is not None and remoto != testo:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (self.cartella(job["id"]) / f"vault-prima-{stamp}.md").write_text(remoto, encoding="utf-8")
        pubblica_file(vault, md, cancella=False, sovrascrivi=sostituisci)
        job["nota_inviata"] = True
        self.salva(job)
        data = leggi_json(j)
        for f in data.get("figure", []):
            if f.get("percorso"):
                source = figure.percorso_sicuro(md.parent, f["percorso"])
                if source.exists():
                    dest = figure.percorso_sicuro(self.conf.note, f["percorso"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() and dest.read_bytes() != source.read_bytes():
                        raise ValueError("Nota inviata; conflitto nella copia locale di un allegato. Il lavoro conserva tutti i file.")
                    shutil.copy2(source, dest)
        self.conf.json.mkdir(parents=True, exist_ok=True)
        dest = self.conf.json / j.name
        if dest.exists() and dest.read_bytes() != j.read_bytes():
            shutil.copy2(dest, self.cartella(job["id"]) / "json-prima-pubblicazione.json")
        scrivi_json(dest, data)
        try:
            pubblica_indice(vault, self.conf.json)
        except Exception:
            job.update(stato="pubblicato", avviso_indice="Nota pubblicata. L’indice non è stato aggiornato: premi «Aggiorna indice» per riprovare.")
        else:
            job.update(stato="pubblicato", avviso_indice=None)
        self.salva(job)

    def vista(self, id):
        with self.lock:
            job = self.leggi(id)
            # Niente percorsi locali, password, stacktrace o contenuto del JSON grezzo.
            view = {k: v for k, v in job.items() if k not in ("fonte",)}
            prepared = self.cartella(id) / "preparato.json"
            if prepared.exists():
                esito = ripristina_preparato(leggi_json(prepared))
                view.update(testo=esito.documento.document_text(), pagine=esito.documento.n_pagine,
                            tabelle=esito.documento.n_tabelle, token=esito.documento.token_stimati,
                            titolo=esito.metadati.titolo, doi=esito.metadati.doi)
            view.update(figure=[], pronto=False, markdown=None)
            if job.get("slug"):
                j, md = self.paths(job)
                data = leggi_json(j)
                for f in data.get("figure", []):
                    f = dict(f)
                    f["immagine"] = f"/api/jobs/{id}/asset/{quote(f['percorso'], safe='/')}" if f.get("percorso") else None
                    view["figure"].append(f)
                view.update(markdown=md.read_text(encoding="utf-8") if md.exists() else "",
                            pronto=self.pronto(job), fonte_web=data.get("sorgente", {}).get("tipo") == "web")
            return view


def crea_app(conf=None, *, root=None, estrattore=None, vault_factory=None):
    conf = conf or Configurazione.carica()
    lavori = Lavori(conf, root, estrattore=estrattore, vault_factory=vault_factory)
    app = Flask(__name__, static_folder="ui", static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=102 * 1024 * 1024, MAX_FORM_PARTS=10,
                      TRUSTED_HOSTS=["127.0.0.1", "localhost"])
    app.extensions["lavori"] = lavori
    token = secrets.token_urlsafe(32)

    @app.before_request
    def proteggi():
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), token):
                abort(403)
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                abort(403)

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {413: "Il file è troppo grande: scegli un PDF fino a 100 MB.",
                    403: "Sessione scaduta o richiesta non consentita. Ricarica la pagina.",
                    404: "Elemento non trovato.", 400: "Richiesta non valida."}
        return jsonify(error=messages.get(exc.code, "Richiesta non consentita.")), exc.code

    @app.errorhandler(Exception)
    def error(exc):
        log.error("Richiesta UI: %s", type(exc).__name__)
        return jsonify(error="Il passaggio non è riuscito. I lavori salvati sono conservati; ricarica la pagina e riprova."), 500

    @app.get("/")
    def index():
        return send_from_directory(Path(__file__).parent / "ui", "index.html")

    @app.get("/api/session")
    def session():
        return jsonify(csrf=token, modello=conf.modello_estrazione or conf.fornitore,
                       vault_url=os.environ.get("VAULT_URL", ""))

    @app.get("/api/jobs")
    def lista():
        return jsonify([{k: j.get(k) for k in ("id", "nome", "stato", "aggiornato")} for j in lavori.elenco()])

    @app.post("/api/jobs")
    def nuovo():
        upload = request.files.get("file")
        if upload:
            if not upload.filename.lower().endswith(".pdf"):
                raise ValueError("Seleziona un file PDF.")
            result = lavori.nuovo(None, Path(upload.filename).name, upload)
        else:
            data = request.get_json(silent=True) or {}
            url = str(data.get("url", "")).strip()
            result = lavori.nuovo(url, url)
        return jsonify(id=result["id"]), 202

    @app.get("/api/jobs/<id>")
    def job(id):
        return jsonify(lavori.vista(id))

    @app.post("/api/jobs/<id>/<azione>")
    def azione(id, azione):
        data = request.get_json(silent=True) or {}
        if azione == "figura":
            lavori.decidi(id, str(data.get("numero")), data.get("scelta"))
        else:
            lavori.avvia(id, azione, sostituisci=data.get("sostituisci") is True)
        return jsonify(ok=True)

    @app.get("/api/jobs/<id>/asset/<path:nome>")
    def asset(id, nome):
        job = lavori.leggi(id)
        j, md = lavori.paths(job)
        allowed = [f.get("percorso") for f in leggi_json(j).get("figure", [])]
        if nome not in allowed or Path(nome).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            abort(404)
        path = figure.percorso_sicuro(md.parent, nome)
        return send_file(path)

    @app.get("/api/jobs/<id>/markdown")
    def download(id):
        _, md = lavori.paths(lavori.leggi(id))
        return send_file(md, as_attachment=True, download_name=md.name, mimetype="text/markdown")

    @app.get("/api/jobs/<id>/preview")
    def preview(id):
        j, md = lavori.paths(lavori.leggi(id))
        testo = md.read_text(encoding="utf-8")
        testo = re.sub(r"<!-- /?paper-pipeline:.*?-->", "", testo)
        parser = MarkdownIt("commonmark", {"html": False}).enable("table")
        tokens = parser.parse(testo)
        allowed = {f.get("percorso") for f in leggi_json(j).get("figure", [])}
        def images(items):
            for item in items:
                if item.type == "image":
                    src = item.attrGet("src")
                    item.attrSet("src", f"/api/jobs/{id}/asset/{quote(src, safe='/')}" if src in allowed else "")
                if item.type == "link_open":
                    item.attrSet("target", "_blank")
                    item.attrSet("rel", "noopener noreferrer")
                if item.children:
                    images(item.children)
        images(tokens)
        return jsonify(html=parser.renderer.render(tokens, parser.options, {}))

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Interfaccia personale Paper Vault sul Mac.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="apri il browser")
    args = parser.parse_args(argv)
    dotenv.load_dotenv(RADICE / ".env", override=False)
    conf = Configurazione.carica()
    # Un solo processo per cartella lavori; previene doppi avvii dal Finder.
    import fcntl
    folder = conf.note / ".webapp"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (folder / "server.lock").open("a+")
    url = f"http://127.0.0.1:{args.port}"
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Paper Vault è già aperto.")
        if args.open:
            webbrowser.open(url)
        return 0
    app = crea_app(conf)
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    print(f"Paper Vault: {url}\nLascia questa finestra aperta. Ctrl+C per arrestare.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Chiusura: attendo eventuali lavori in corso.")
    finally:
        app.extensions["lavori"].worker.shutdown(wait=True)
        server.server_close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
