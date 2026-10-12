'use strict';
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stateNames = {estrazione:'Lettura della fonte', testo_pronto:'Testo da controllare', elaborazione:'Elaborazione in corso', revisione:'Nota da rivedere', pubblicazione:'Invio al vault', pubblicato:'Pubblicato', errore:'Da riprendere', interrotto:'Interrotto', conflitto:'Versione già presente', recupero_figure:'Recupero figure'};
const busyStates = new Set(['estrazione','elaborazione','pubblicazione','recupero_figure']);
let csrf = '', selected = null, revision = '', model = '', vault = '', pending = false, polling = false;
let listMarkup = '', renderVersion = 0, connectionNotice = false;
const imageDrafts = new Map();
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'X-CSRF-Token': csrf, ...(options.headers || {})}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Richiesta non riuscita. Riprova.');
  return data;
}
function notice(message, connection = false) { connectionNotice = connection; $('notice').textContent = message || ''; $('notice').hidden = !message; }
function steps(current) { document.querySelectorAll('.steps li').forEach((el, i) => { if (i === current) el.setAttribute('aria-current', 'step'); else el.removeAttribute('aria-current'); }); }
function vaultLink(job) { return vault ? `${vault.replace(/\/$/, '')}/${encodeURIComponent(job.slug || 'index')}` : ''; }
function showNew() {
  renderVersion++; selected = null; revision = ''; localStorage.removeItem('paper-vault-job'); notice(''); steps(0);
  $('content').innerHTML = `<section class="panel"><h1>Prepara una nuova nota</h1><p class="muted">Scegli un articolo. Prima di elaborarlo potrai controllare il testo estratto.</p>
    <form id="source-form"><fieldset><legend>Tipo di fonte</legend><label class="choice"><input type="radio" name="source-type" value="file" checked> File PDF</label><label class="choice"><input type="radio" name="source-type" value="url"> Link al full-text</label></fieldset>
    <div id="file-field"><label class="field" for="pdf">Seleziona il PDF</label><input id="pdf" type="file" accept=".pdf,application/pdf" required><p class="hint">Fino a 100 MB. L’originale resta nella sua cartella.</p></div>
    <div id="url-field" hidden><label class="field" for="url">Indirizzo dell’articolo completo</label><input id="url" type="url" placeholder="https://…" disabled><p class="hint">Se il sito limita l’accesso, scarica il PDF dal browser e caricalo qui.</p></div>
    <div class="actions"><button class="primary" type="submit">Estrai il testo</button><p class="hint">Questo passaggio non chiama il modello.</p></div></form></section>`;
  document.querySelectorAll('[name="source-type"]').forEach(input => input.addEventListener('change', () => {
    const useUrl = input.value === 'url'; $('file-field').hidden = useUrl; $('url-field').hidden = !useUrl;
    $('pdf').disabled = useUrl; $('pdf').required = !useUrl; $('url').disabled = !useUrl; $('url').required = useUrl;
  }));
  $('source-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (pending) return; pending = true; const button = event.submitter; button.disabled = true; button.textContent = 'Caricamento…'; notice('');
    try {
      let options;
      if (!$('pdf').disabled) { const form = new FormData(); form.append('file', $('pdf').files[0]); options = {method:'POST', body:form}; }
      else options = {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:$('url').value})};
      const result = await api('/api/jobs', options); await choose(result.id);
    } catch(error) { notice(error.message); button.disabled = false; button.textContent = 'Estrai il testo'; }
    finally { pending = false; }
  });
  refreshList();
}
async function choose(id) { selected = id; revision = ''; renderVersion++; $('content').innerHTML = '<p role="status">Apertura del lavoro…</p>'; localStorage.setItem('paper-vault-job', id); notice(''); await refresh(); }
async function refreshList() {
  const jobs = await api('/api/jobs');
  const markup = jobs.length ? jobs.map(j => `<button class="job-link" data-id="${j.id}" aria-current="${j.id === selected}">${esc(j.nome)}<small>${esc(stateNames[j.stato] || j.stato)}</small></button>`).join('') : '<p class="hint">Qui ritroverai gli articoli caricati, anche dopo aver chiuso il browser.</p>';
  if (markup === listMarkup) return;
  listMarkup = markup;
  const focused = document.activeElement?.dataset?.id;
  $('jobs').innerHTML = markup;
  document.querySelectorAll('.job-link').forEach(b => { b.addEventListener('click', () => { if (!pending) choose(b.dataset.id).catch(e => notice(e.message)); }); if (b.dataset.id === focused) b.focus(); });
}
async function action(jobId, name, data = {}) {
  if (pending || selected !== jobId) return;
  if (name === 'immagine_url') imageDrafts.set(`${jobId}:${data.numero}`, data.url);
  pending = true; notice('');
  document.querySelectorAll('#content button').forEach(b => b.disabled = true);
  try { await api(`/api/jobs/${jobId}/${name}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}); if (name === 'immagine_url') imageDrafts.delete(`${jobId}:${data.numero}`); revision = ''; await refresh(); }
  catch(error) { notice(error.message); revision = ''; await refresh(); }
  finally { pending = false; }
}
function warning(job) { return job.avvisi?.length ? `<details><summary>Avvisi della fonte (${job.avvisi.length})</summary><ul>${job.avvisi.map(a => `<li>${esc(a)}</li>`).join('')}</ul></details>` : ''; }
function sourceText(job) {
  return `<h2>${esc(job.titolo || 'Testo estratto')}</h2><div class="metadata"><span>${job.pagine} ${job.pagine === 1 ? 'pagina / documento' : 'pagine'}</span><span>${job.tabelle} tabelle</span><span>Circa ${Number(job.token).toLocaleString('it-IT')} token</span>${job.doi ? `<span>DOI: ${esc(job.doi)}</span>` : ''}</div><pre class="extract" tabindex="0" aria-label="Testo estratto completo">${esc(job.testo)}</pre>`;
}
function renderJob(job) {
  const version = ++renderVersion;
  const busy = busyStates.has(job.stato);
  const hasNote = Boolean(job.slug);
  steps(job.stato === 'pubblicato' || job.stato === 'pubblicazione' ? 3 : hasNote ? 2 : job.testo ? 1 : 0);
  let html = `<p class="hint saved-name">${esc(job.nome)}</p>`;
  if (job.errore) html += `<div class="warning" role="alert">${esc(job.errore)}</div>`;
  if (busy) {
    const copy = job.stato === 'estrazione' ? 'Estraggo testo, tabelle e metadati bibliografici.' : job.stato === 'elaborazione' ? 'Il modello sta preparando la nota. Gli articoli lunghi possono richiedere diversi minuti.' : job.stato === 'pubblicazione' ? 'Carico le immagini e la nota, poi verifico il contenuto sul vault.' : job.ultima_azione === 'immagine_url' ? 'Scarico l’immagine dal link e preparo l’anteprima, senza chiamare il modello.' : 'Riprovo il recupero degli originali senza chiamare il modello.';
    html += `<section class="panel busy" aria-busy="true"><h1>${esc(stateNames[job.stato])}</h1><p>${copy}</p><p class="hint">Puoi chiudere questa pagina. Lascia aperto il servizio Paper Vault sul Mac.</p></section>`;
  } else if (job.stato === 'testo_pronto') {
    html += `<section class="panel"><h1>Controlla la fonte</h1><p class="muted">Verifica che il testo sia completo e leggibile, soprattutto tabelle, dosaggi e unità di misura.</p>${sourceText(job)}${warning(job)}
      ${job.gia_elaborato ? '<p>Questo articolo è già stato elaborato: recupererò la nota salvata senza una nuova chiamata al modello.</p>' : ''}
      <div class="actions"><button class="primary" data-action="genera">${job.gia_elaborato ? 'Apri la nota salvata' : 'Genera la nota'}</button><p class="hint">${job.gia_elaborato ? 'La fonte già salvata mantiene i suoi riferimenti originali.' : `Invia questo testo a ${esc(model)}. Questo passaggio usa l’API a pagamento.`}</p></div></section>`;
  } else if (hasNote) {
    if (job.stato === 'pubblicato') {
      html += `<section class="success"><h1>Nota pubblicata</h1><p>La nota e le immagini sono state caricate e verificate sul vault.</p>${vault ? `<a class="button primary" href="${esc(vaultLink(job))}" target="_blank" rel="noopener noreferrer">Apri la nota in SilverBullet ↗</a>` : ''}</section>`;
      if (job.avviso_indice) html += `<div class="warning">${esc(job.avviso_indice)}<div class="actions"><button data-action="pubblica">Aggiorna indice</button></div></div>`;
    } else {
      const remaining = job.figure.filter(f => !['approvata','esclusa'].includes(f.stato)).length;
      html += `<h1>Rivedi la nota</h1><p class="muted">${job.figure.length ? `${remaining} ${remaining === 1 ? 'figura' : 'figure'} da decidere. Controlla completezza e corrispondenza con il passaggio della fonte.` : 'Nessuna figura selezionata. Controlla la nota prima di pubblicarla.'}</p>`;
      for (const f of job.figure) {
        const manualAttempt = job.immagine_manuale?.numero === f.numero ? job.immagine_manuale : null;
        const manualCheck = f.immagine && f.origine_immagine === 'url_manuale' && !f.selezione_verificata;
        const inputId = `image-url-${f.numero}`;
        html += `<article class="figure"><header><h2>Figura ${esc(f.numero)}</h2><span class="status ${f.stato === 'approvata' ? 'ok' : ''}">${esc({approvata:'Approvata',esclusa:'Esclusa',da_verificare:'Da verificare',non_disponibile:'Non disponibile'}[f.stato])}</span></header><p class="hint">Nella sezione: ${esc(f.sezione_nota)}</p><blockquote>${esc(f.passaggio_fonte)}</blockquote>
          ${f.immagine ? `<a href="${esc(f.immagine)}" target="_blank" rel="noopener"><img src="${esc(f.immagine)}" alt="Figura ${esc(f.numero)}: originale da verificare" loading="lazy"></a><p class="hint">Apri l’immagine per ingrandirla.</p>` : ''}
          <p class="caption">${esc(f.didascalia || '')}</p>${f.problema && f.stato === 'non_disponibile' ? `<p class="warning">${esc(f.problema)}</p>` : ''}
          ${f.origine_immagine === 'url_manuale' ? '<p class="hint">Immagine recuperata dal link manuale. Controllala prima di approvarla.</p>' : ''}
          ${manualCheck ? `<div class="warning"><p>Il richiamo non è stato verificato automaticamente: ${esc(f.problema_selezione || '')}</p><label class="manual-check"><input type="checkbox" id="confirm-${esc(f.numero)}"> Ho verificato nell’articolo che questa figura corrisponde al passaggio citato e alla sezione della nota.</label></div>` : ''}
          <details class="manual-image" ${!f.immagine || manualAttempt ? 'open' : ''}><summary>${f.immagine ? 'Usa un’altra immagine tramite link' : 'Inserisci il link dell’immagine'}</summary>
            <form data-image-form="${esc(f.numero)}"><label class="field" for="${esc(inputId)}">Link diretto alla figura ${esc(f.numero)}</label>
              <input id="${esc(inputId)}" name="image-url" type="url" required maxlength="8192" placeholder="https://…/figura.png" value="${esc(imageDrafts.get(`${job.id}:${f.numero}`) ?? manualAttempt?.url ?? f.url_manuale ?? '')}" aria-describedby="hint-${esc(inputId)}">
              <p class="hint" id="hint-${esc(inputId)}">Apri l’immagine sul sito dell’editore e copia il suo indirizzo. PNG, JPEG o WebP, fino a 25 MB. Il link viene scaricato sul Mac; poi potrai vedere l’anteprima e approvarla.</p>
              ${manualAttempt && job.errore ? `<p class="warning" role="alert">${esc(job.errore)} Controlla il link diretto e riprova.</p>` : ''}
              <button type="submit">Carica anteprima</button>
            </form>
          </details>
          <div class="actions"><button class="${f.stato === 'approvata' ? '' : 'primary'}" data-figure="${esc(f.numero)}" data-sha256="${esc(f.sha256 || '')}" data-choice="approvata" ${!f.immagine || f.stato === 'approvata' ? 'disabled' : ''}>${f.stato === 'approvata' ? 'Approvata' : 'Approva figura'}</button><button data-figure="${esc(f.numero)}" data-choice="esclusa" ${f.stato === 'esclusa' ? 'disabled' : ''}>${f.stato === 'esclusa' ? 'Esclusa' : 'Escludi'}</button></div></article>`;
      }
      if (job.fonte_web && job.figure.some(f => f.stato === 'non_disponibile')) html += '<button data-action="riprova_figure">Riprova le immagini mancanti</button>';
      if (!job.pronto && !job.figure.length) html += '<p class="warning">La preparazione delle figure è stata interrotta.</p><button data-action="genera">Riprendi la preparazione</button>';
      if (job.stato === 'conflitto') html += `<div class="warning"><p>La sostituzione aggiorna l’intero testo sul vault. La versione precedente sarà conservata sul Mac.</p><button id="replace-note">Sostituisci la nota sul vault</button>${vault ? ` <a href="${esc(vaultLink(job))}" target="_blank" rel="noopener noreferrer">Apri la versione esistente ↗</a>` : ''}</div>`;
    }
    html += `<section class="review-note"><h2>Anteprima della nota</h2><div id="preview" class="note-preview">Caricamento dell’anteprima…</div><div class="actions"><a class="button" href="/api/jobs/${job.id}/markdown">Scarica il Markdown</a></div></section>`;
    if (job.stato !== 'pubblicato') html += `<div class="publish-row"><button class="primary" data-action="pubblica" ${!job.pronto ? 'disabled' : ''}>Pubblica su SilverBullet</button><p class="hint">${job.pronto ? 'Invia la nota e le immagini approvate alla VPS.' : 'Approva o escludi tutte le figure per continuare.'}</p></div>`;
    html += warning(job);
  } else {
    html += '<h1>Riprendi il lavoro</h1>';
    if (job.testo) html += `${sourceText(job)}<div class="actions"><button class="primary" data-action="genera">Riprendi la preparazione della nota</button><p class="hint">Se il JSON è già salvato, viene recuperato. Se la risposta del modello è andata persa, una nuova richiesta può comportare un costo.</p></div>`;
    else html += '<button class="primary" data-action="estrai">Riprova la lettura della fonte</button><p class="hint">Se il sito limita l’accesso, crea un nuovo lavoro caricando il PDF.</p>';
  }
  $('content').innerHTML = html;
  $('announcer').textContent = stateNames[job.stato] || job.stato;
  document.querySelectorAll('[data-action]').forEach(b => b.addEventListener('click', () => action(job.id, b.dataset.action)));
  document.querySelectorAll('[data-figure]').forEach(b => b.addEventListener('click', () => action(job.id, 'figura', {numero:b.dataset.figure, scelta:b.dataset.choice, sha256:b.dataset.sha256, conferma_richiamo:$(`confirm-${b.dataset.figure}`)?.checked === true})));
  document.querySelectorAll('[data-image-form]').forEach(form => {
    form.elements.namedItem('image-url').addEventListener('input', event => imageDrafts.set(`${job.id}:${form.dataset.imageForm}`, event.target.value));
    form.addEventListener('submit', event => {
    event.preventDefault();
    action(job.id, 'immagine_url', {numero:form.dataset.imageForm, url:form.elements.namedItem('image-url').value.trim()});
    });
  });
  $('replace-note')?.addEventListener('click', () => action(job.id, 'pubblica', {sostituisci:true}));
  if ($('preview')) api(`/api/jobs/${job.id}/preview`).then(data => { if (selected === job.id && version === renderVersion && $('preview')) $('preview').innerHTML = data.html; }).catch(e => notice(e.message));
}
async function refresh() {
  await refreshList();
  if (connectionNotice) notice('');
  const id = selected;
  if (!id) return;
  const job = await api(`/api/jobs/${id}`);
  if (selected !== id) return;
  if (revision !== job.aggiornato) { revision = job.aggiornato; renderJob(job); }
}
async function start() {
  try {
    const session = await api('/api/session'); csrf = session.csrf; model = session.modello; vault = session.vault_url;
    if (/^https?:\/\//.test(vault)) { $('vault-link').href = vault; $('vault-link').hidden = false; } else vault = '';
    if (matchMedia('(max-width: 760px)').matches) $('history').open = false;
    $('new-job').addEventListener('click', () => { if (!pending) showNew(); });
    const last = localStorage.getItem('paper-vault-job');
    if (last) { try { await choose(last); } catch { showNew(); } } else showNew();
    setInterval(async () => { if (polling || pending) return; polling = true; try { await refresh(); } catch { notice('Il servizio sul Mac non risponde. Riapri Paper Vault, poi ricarica questa pagina.', true); } finally { polling = false; } }, 2000);
  } catch(error) { notice(error.message); }
}
start();
