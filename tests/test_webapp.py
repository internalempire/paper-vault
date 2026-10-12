"""Flusso UI reale senza servizi esterni: snapshot, revisioni, upload e ripresa."""
import dataclasses
import io
import json
import threading

import pytest

from src import webapp as web
from src.extract import extract
from src.ingest import Configurazione, Esito
from src.metadata import Metadati
from tests.test_figure import documento, nota, VaultConFile
from tests.test_ingest import EstrattoreFinto


class EstrattoreContato(EstrattoreFinto):
    calls = 0
    def estrai(self, *args, **kwargs):
        self.calls += 1
        return super().estrai(*args, **kwargs)


@pytest.fixture
def ambiente(tmp_path, monkeypatch, nota):
    conf = Configurazione(note=tmp_path / "vault", json=tmp_path / "vault/.json")
    estrattore = EstrattoreContato(nota)
    vault = VaultConFile()
    def prepara(path, config):
        doc = extract(path, ocr=False, tabelle=False)
        return Esito(slug="", documento=doc, metadati=Metadati(doi="10.1234/test", titolo="Review"), documento_sorgente=path)
    monkeypatch.setattr(web, "prepara_ingresso", prepara)
    app = web.crea_app(conf, estrattore=estrattore, vault_factory=lambda: vault)
    app.config['TESTING'] = True
    client = app.test_client()
    csrf = client.get('/api/session').json['csrf']
    headers = {'X-CSRF-Token': csrf}
    jobs = app.extensions['lavori']
    yield client, headers, jobs, estrattore, vault
    jobs.worker.shutdown(wait=True)


def carica(env, documento):
    c, h, jobs, _, _ = env
    response = c.post('/api/jobs', data={'file': (io.BytesIO(documento.percorso.read_bytes()), 'paper.pdf')}, headers=h)
    assert response.status_code == 202, response.json
    id = response.json['id']
    jobs.futures[id].result(timeout=10)
    return id


def azione(env, id, name, **data):
    c, h, jobs, _, _ = env
    if name == 'figura' and data.get('scelta') == 'approvata' and 'sha256' not in data:
        view = c.get(f'/api/jobs/{id}').json
        data['sha256'] = next(f.get('sha256') for f in view['figure'] if f['numero'] == data['numero'])
    response = c.post(f'/api/jobs/{id}/{name}', json=data, headers=h)
    if name != 'figura' and response.status_code == 200:
        jobs.futures[id].result(timeout=10)
    return response


def test_upload_e_refresh_non_chiamano_modello(ambiente, documento):
    id = carica(ambiente, documento)
    c, _, jobs, llm, vault = ambiente
    for _ in range(3):
        result = c.get(f'/api/jobs/{id}').json
        assert result['stato'] == 'testo_pronto'
        assert 'Figure 1' in result['testo']
    assert llm.calls == 0 and not vault.contenuti
    assert (jobs.cartella(id) / 'fonte.pdf').read_bytes() == documento.percorso.read_bytes()


def test_flusso_completo_con_approvazione_e_pubblicazione(ambiente, documento):
    id = carica(ambiente, documento)
    c, _, jobs, llm, vault = ambiente
    before = c.get(f'/api/jobs/{id}').json['testo']
    assert azione(ambiente, id, 'genera').status_code == 200
    view = c.get(f'/api/jobs/{id}').json
    assert view['stato'] == 'revisione', view.get('errore')
    assert llm.calls == 1 and llm.testo_ricevuto == before
    assert not view['pronto'] and view['figure'][0]['immagine']
    assert azione(ambiente, id, 'pubblica').status_code == 409
    assert not vault.contenuti
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata').status_code == 200
    assert c.get(f'/api/jobs/{id}').json['pronto']
    assert '<img' in c.get(f'/api/jobs/{id}/preview').json['html']
    azione(ambiente, id, 'pubblica')
    view = c.get(f'/api/jobs/{id}').json
    assert view['stato'] == 'pubblicato', view.get('errore')
    assert view['slug'] in vault.contenuti and 'index' in vault.contenuti
    assert vault.files and (jobs.conf.json / f"{view['slug']}.json").is_file()
    assert c.get(f'/api/jobs/{id}/markdown').status_code == 200
    assert llm.calls == 1


def test_sorgente_non_riscaricata_dopo_approvazione(ambiente, documento, monkeypatch):
    id = carica(ambiente, documento)
    monkeypatch.setattr(web, 'prepara_ingresso', lambda *a: pytest.fail('seconda estrazione'))
    azione(ambiente, id, 'genera')
    assert ambiente[0].get(f'/api/jobs/{id}').json['stato'] == 'revisione'


def test_click_duplicato_non_duplica_spesa(ambiente, documento, monkeypatch):
    id = carica(ambiente, documento)
    c, h, jobs, llm, _ = ambiente
    started, finish = threading.Event(), threading.Event()
    original = llm.estrai
    def wait(*args, **kwargs):
        started.set()
        finish.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(llm, 'estrai', wait)
    assert c.post(f'/api/jobs/{id}/genera', headers=h).status_code == 200
    assert started.wait(5)
    assert c.post(f'/api/jobs/{id}/genera', headers=h).status_code == 409
    finish.set()
    jobs.futures[id].result(timeout=10)
    assert llm.calls == 1


def test_esclusione_permette_nota_senza_immagine(ambiente, documento):
    id = carica(ambiente, documento)
    azione(ambiente, id, 'genera')
    azione(ambiente, id, 'figura', numero='1', scelta='esclusa')
    azione(ambiente, id, 'pubblica')
    assert ambiente[0].get(f'/api/jobs/{id}').json['stato'] == 'pubblicato'
    assert not ambiente[4].files


def test_approvazione_rifiuta_file_cambiato(ambiente, documento):
    id = carica(ambiente, documento)
    azione(ambiente, id, 'genera')
    jobs = ambiente[2]
    view = jobs.vista(id)
    p = jobs.config_lavoro(id).note / view['figure'][0]['percorso']
    p.write_bytes(b'corrupt')
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata').status_code == 409
    assert not jobs.vista(id)['pronto']


def test_conflitto_remoto_richiede_scelta_e_fa_backup(ambiente, documento):
    id = carica(ambiente, documento)
    c, _, jobs, _, vault = ambiente
    azione(ambiente, id, 'genera')
    azione(ambiente, id, 'figura', numero='1', scelta='approvata')
    slug = jobs.leggi(id)['slug']
    old = '# Nota corretta manualmente\n\nCorrezione da conservare.'
    vault.contenuti[slug] = old
    azione(ambiente, id, 'pubblica')
    assert jobs.leggi(id)['stato'] == 'conflitto'
    assert vault.contenuti[slug] == old
    azione(ambiente, id, 'pubblica', sostituisci=True)
    assert jobs.leggi(id)['stato'] == 'pubblicato'
    assert next(jobs.cartella(id).glob('vault-prima-*.md')).read_text() == old


def test_errore_upload_conserva_nota_e_riprova_senza_llm(ambiente, documento, monkeypatch):
    id = carica(ambiente, documento)
    c, _, jobs, llm, vault = ambiente
    azione(ambiente, id, 'genera')
    azione(ambiente, id, 'figura', numero='1', scelta='esclusa')
    real = vault.crea
    monkeypatch.setattr(vault, 'crea', lambda *a: (_ for _ in ()).throw(RuntimeError('network')))
    azione(ambiente, id, 'pubblica')
    assert jobs.leggi(id)['stato'] == 'errore'
    assert jobs.paths(jobs.leggi(id))[1].exists()
    monkeypatch.setattr(vault, 'crea', real)
    azione(ambiente, id, 'pubblica')
    assert jobs.leggi(id)['stato'] == 'pubblicato' and llm.calls == 1


def test_riprende_json_pagato_anche_senza_doi(ambiente, documento):
    id = carica(ambiente, documento)
    c, _, jobs, llm, _ = ambiente
    p = jobs.cartella(id) / 'preparato.json'
    data = web.leggi_json(p); data['metadati']['doi'] = None; web.scrivi_json(p, data)
    azione(ambiente, id, 'genera')
    j = jobs.leggi(id); j.update(stato='interrotto', slug=None); jobs.salva(j)
    azione(ambiente, id, 'genera')
    assert jobs.leggi(id)['stato'] == 'revisione' and llm.calls == 1


def test_lavori_persistono_e_riavvio_segnala_interruzione(ambiente, documento):
    id = carica(ambiente, documento)
    jobs = ambiente[2]
    j = jobs.leggi(id); j['stato'] = 'elaborazione'; jobs.salva(j)
    second = web.Lavori(jobs.conf)
    try:
        assert second.leggi(id)['stato'] == 'interrotto'
        assert 'Figure 1' in second.vista(id)['testo']
    finally:
        second.worker.shutdown()


def test_indice_fallito_non_dice_nota_non_pubblicata(ambiente, documento, monkeypatch):
    id = carica(ambiente, documento)
    azione(ambiente, id, 'genera')
    azione(ambiente, id, 'figura', numero='1', scelta='esclusa')
    monkeypatch.setattr(web, 'pubblica_indice', lambda *a: (_ for _ in ()).throw(RuntimeError('network')))
    azione(ambiente, id, 'pubblica')
    job = ambiente[2].leggi(id)
    assert job['stato'] == 'pubblicato' and job['avviso_indice']


def test_cache_globale_riusata_in_copia_isolata(ambiente, documento):
    id = carica(ambiente, documento)
    azione(ambiente, id, 'genera'); azione(ambiente, id, 'figura', numero='1', scelta='approvata'); azione(ambiente, id, 'pubblica')
    jobs = ambiente[2]
    originale = next(jobs.conf.json.glob('*.json')).read_bytes()
    second = carica(ambiente, documento)
    assert jobs.leggi(second)['gia_elaborato']
    azione(ambiente, second, 'genera')
    azione(ambiente, second, 'figura', numero='1', scelta='esclusa')
    assert next(jobs.conf.json.glob('*.json')).read_bytes() == originale
    assert ambiente[3].calls == 1


@pytest.mark.parametrize('headers', [{}, {'X-CSRF-Token':'wrong'}])
def test_post_senza_token_non_fa_null(ambiente, headers):
    assert ambiente[0].post('/api/jobs', json={'url':'https://example.org'}, headers=headers).status_code == 403
    assert ambiente[2].elenco() == []


def test_origin_estranea_host_non_fidato(ambiente):
    c,h,*_ = ambiente
    assert c.post('/api/jobs', json={}, headers={**h,'Origin':'https://evil.example'}).status_code == 403
    assert c.get('/api/session', headers={'Host':'evil.example'}).status_code == 400


@pytest.mark.parametrize('name,data', [('bad.html',b'<script>bad</script>'),('bad.pdf',b'not a PDF')])
def test_upload_non_pdf_bloccato(ambiente, name, data):
    c,h,*_ = ambiente
    assert c.post('/api/jobs', data={'file':(io.BytesIO(data),name)}, headers=h).status_code == 409


def test_asset_non_puo_leggere_file_arbitrari(ambiente, documento):
    id = carica(ambiente, documento); azione(ambiente,id,'genera')
    c = ambiente[0]
    assert c.get(f'/api/jobs/{id}/asset/.json/test.json').status_code == 404
    assert c.get(f'/api/jobs/{id}/asset/../../.env').status_code == 404
    assert c.get('/api/jobs/../../.env').status_code == 404


def test_markdown_non_esegue_html_o_carica_immagini_esterne(ambiente, documento):
    id = carica(ambiente,documento); azione(ambiente,id,'genera')
    jobs=ambiente[2]; _,md=jobs.paths(jobs.leggi(id))
    md.write_text('<script>alert(1)</script>\n\n![tracking](https://evil.example/a.png)\n\n[click](javascript:alert(1))')
    html=ambiente[0].get(f'/api/jobs/{id}/preview').json['html']
    assert '<script>' not in html and 'src="https://' not in html and 'href="javascript:' not in html
    assert "object-src 'none'" in ambiente[0].get('/').headers['Content-Security-Policy']


def test_errore_modello_non_espone_segreti(ambiente, documento, monkeypatch):
    id=carica(ambiente,documento)
    monkeypatch.setattr(ambiente[3], 'estrai', lambda *a,**k: (_ for _ in ()).throw(RuntimeError('sk-secret-EXAMPLE')))
    azione(ambiente,id,'genera')
    result=ambiente[0].get(f'/api/jobs/{id}').json
    assert result['stato']=='errore' and 'sk-secret' not in result['errore']


def manuale(env, documento, monkeypatch, *, verificata=True):
    from src import web_article
    from PIL import Image
    id = carica(env, documento)
    azione(env, id, 'genera')
    jobs = env[2]
    j, md = jobs.paths(jobs.leggi(id))
    data = web.leggi_json(j)
    f = data['figure'][0]
    f.update(stato='non_disponibile', selezione_verificata=verificata,
             problema='Immagine non riconosciuta' if verificata else 'Richiamo non verificato')
    for key in ('percorso', 'sha256'):
        f.pop(key, None)
    web.scrivi_json(j, data)
    jobs._rigenera(jobs.leggi(id))
    image = io.BytesIO()
    Image.new('RGB', (160, 100), 'green').save(image, format='PNG')
    monkeypatch.setattr(web_article, 'scarica', lambda url, **kw: (image.getvalue(), url, 'image/png'))
    return id, j, md


def test_link_manuale_anteprima_approvazione_pubblicazione(ambiente, documento, monkeypatch):
    id, j, md = manuale(ambiente, documento, monkeypatch)
    url = 'https://publisher.example/figure-original.png'
    assert azione(ambiente, id, 'immagine_url', numero='1', url=url).status_code == 200
    c, _, jobs, llm, vault = ambiente
    view = c.get(f'/api/jobs/{id}').json
    f = view['figure'][0]
    assert view['stato'] == 'revisione' and f['stato'] == 'da_verificare'
    assert f['url_manuale'] == url and 'pagina_pdf' not in f
    assert c.get(f['immagine']).status_code == 200
    assert not view['pronto'] and '![' not in md.read_text()
    assert azione(ambiente, id, 'pubblica').status_code == 409
    azione(ambiente, id, 'figura', numero='1', scelta='approvata')
    assert '<img' in c.get(f'/api/jobs/{id}/preview').json['html']
    assert f['percorso'] in md.read_text() and 'da link fornito manualmente' in md.read_text()
    azione(ambiente, id, 'pubblica')
    assert c.get(f'/api/jobs/{id}').json['stato'] == 'pubblicato'
    assert vault.files and llm.calls == 1
    assert azione(ambiente, id, 'immagine_url', numero='1', url=url).status_code == 409


def test_link_non_scavalca_verifica_richiamo(ambiente, documento, monkeypatch):
    id, j, md = manuale(ambiente, documento, monkeypatch, verificata=False)
    azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/fig.png')
    assert not web.leggi_json(j)['figure'][0]['selezione_verificata']
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata').status_code == 409
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata', conferma_richiamo=True).status_code == 200
    assert web.leggi_json(j)['figure'][0]['verifica_selezione'] == 'manuale'
    assert 'problema_selezione' in web.leggi_json(j)['figure'][0]


def test_link_errato_conserva_immagine_precedente_e_url(ambiente, documento, monkeypatch):
    from src import web_article
    id, j, md = manuale(ambiente, documento, monkeypatch)
    azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/fig.png')
    azione(ambiente, id, 'figura', numero='1', scelta='approvata')
    before, markdown = j.read_bytes(), md.read_bytes()
    monkeypatch.setattr(web_article, 'scarica', lambda *a, **kw: (b'<html>Not an image</html>', a[0], 'application/octet-stream'))
    azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/figure-page')
    view = ambiente[0].get(f'/api/jobs/{id}').json
    assert view['stato'] == 'errore' and view['immagine_manuale']['url'].endswith('figure-page')
    assert j.read_bytes() == before and md.read_bytes() == markdown
    # Nuova risorsa valida: annulla l'approvazione precedente.
    image = io.BytesIO()
    from PIL import Image
    Image.new('RGB', (160, 100), 'blue').save(image, format='PNG')
    monkeypatch.setattr(web_article, 'scarica', lambda url, **kw: (image.getvalue(), url, 'image/png'))
    azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/new.png')
    assert web.leggi_json(j)['figure'][0]['stato'] == 'da_verificare'
    assert '![' not in md.read_text()


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'javascript:alert(1)', 'https://user:password@example.org/image.png'])
def test_link_manuale_rifiuta_url_non_validi(ambiente, documento, monkeypatch, url):
    id, j, md = manuale(ambiente, documento, monkeypatch)
    assert azione(ambiente, id, 'immagine_url', numero='1', url=url).status_code == 409


def test_link_manuale_blocca_rete_locale(ambiente, documento, monkeypatch):
    from src import web_article
    fetch_originale = web_article.scarica
    id, j, md = manuale(ambiente, documento, monkeypatch)
    monkeypatch.setattr(web_article, 'scarica', fetch_originale)
    azione(ambiente, id, 'immagine_url', numero='1', url='http://127.0.0.1/secret.png')
    view = ambiente[0].get(f'/api/jobs/{id}').json
    assert view['stato'] == 'errore' and 'pubblici' in view['errore']
    assert not view['figure'][0]['immagine']


def test_link_manuale_rifiuta_figura_estranea_o_sezione_assente(ambiente, documento, monkeypatch):
    id, j, md = manuale(ambiente, documento, monkeypatch)
    assert azione(ambiente, id, 'immagine_url', numero='999', url='https://publisher.example/fig.png').status_code == 409
    data = web.leggi_json(j)
    data['figure'][0]['sezione_nota'] = 'Sezione inesistente'
    web.scrivi_json(j, data)
    assert azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/fig.png').status_code == 409


def test_download_manuale_blocca_azioni_concorrenti(ambiente, documento, monkeypatch):
    from src import web_article
    id, j, md = manuale(ambiente, documento, monkeypatch)
    started, finish = threading.Event(), threading.Event()
    download = web_article.scarica
    def lento(*args, **kwargs):
        started.set()
        assert finish.wait(10)
        return download(*args, **kwargs)
    monkeypatch.setattr(web_article, 'scarica', lento)
    c, h, jobs, _, _ = ambiente
    try:
        assert c.post(f'/api/jobs/{id}/immagine_url', json={'numero':'1','url':'https://publisher.example/fig.png'}, headers=h).status_code == 200
        assert started.wait(5)
        assert c.post(f'/api/jobs/{id}/figura', json={'numero':'1','scelta':'esclusa'}, headers=h).status_code == 409
        assert c.post(f'/api/jobs/{id}/immagine_url', json={'numero':'1','url':'https://publisher.example/fig.png'}, headers=h).status_code == 409
    finally:
        finish.set()
        jobs.futures[id].result(timeout=10)


def test_sostituzione_richiede_nuova_conferma_manuale_e_hash(ambiente, documento, monkeypatch):
    from src import web_article
    from PIL import Image
    id, j, md = manuale(ambiente, documento, monkeypatch, verificata=False)
    azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/fig.png')
    old = web.leggi_json(j)['figure'][0]['sha256']
    azione(ambiente, id, 'figura', numero='1', scelta='approvata', conferma_richiamo=True)
    image = io.BytesIO()
    Image.new('RGB', (160, 100), 'red').save(image, format='PNG')
    monkeypatch.setattr(web_article, 'scarica', lambda url, **kw: (image.getvalue(), url, 'image/png'))
    azione(ambiente, id, 'immagine_url', numero='1', url='https://publisher.example/new.png')
    assert not web.leggi_json(j)['figure'][0]['selezione_verificata']
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata', conferma_richiamo=True, sha256=old).status_code == 409
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata').status_code == 409
    assert azione(ambiente, id, 'figura', numero='1', scelta='approvata', conferma_richiamo=True).status_code == 200
