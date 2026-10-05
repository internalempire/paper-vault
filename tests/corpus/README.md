# Corpus di test

Qui vanno i tre documenti dello HANDOVER §7. **Non sono nel repo**: sono articoli sotto
copyright e vanno depositati a mano.

`ingest.py` **non tocca** i documenti: restano qui, e la nota ne registra il percorso nel
JSON intermedio. Per rielaborarli si rilancia sullo stesso percorso con `--forza`.

| File atteso | Documento | Prima pagina (per `--prima-pagina`) |
|---|---|---|
| `palmer-chetoacidosi.pdf` | Palmer & Clegg, *Pathophysiology of Ketoacidosis: Core Curriculum 2026*, AJKD 2026;88(2):301-314 | `301` |
| `aha-acc-embolia-polmonare.pdf` | Creager et al., *2026 AHA/ACC Guideline for Acute Pulmonary Embolism*, Circulation 2026;153:e977-e1051 | `e977` |
| `aletras-resistenza-diuretica.html` | Aletras et al., *Diuretic resistance in cardiorenal syndrome*, Front Cardiovasc Med 2026;12:1731305 | — (nessuna paginazione) |

Le note di riferimento corrispondenti stanno in `../golden/`. Sono il corpus di regressione:
se una modifica al prompt o al renderer produce note peggiori di quelle, la modifica è sbagliata.

## Verifica dell'estrazione, prima di spendere chiamate API

    python -m src.extract tests/corpus/PIIS0272638626009236.pdf
    python -m src.extract tests/corpus/creager-et-al-2026-*.pdf
    python -m src.extract tests/corpus/aletras-resistenza-diuretica.html --text | less

### Esito atteso (verificato)

| | pagine | token stimati | tabelle | riferimenti |
|---|---|---|---|---|
| Palmer | 14 | ~17.000 | 1 (Tabella 1, stampata di traverso) | pagina, 301–314 |
| AHA/ACC | 75 | ~105.000 | 55 | pagina, e977–e1051 |
| Aletras | — | ~23.000 | 5 | sezione |

I ~105.000 token della linea guida stanno sotto `soglia_token_avviso`: il documento entra in
una chiamata sola e il taglio per sezioni via TOC resta rimandabile (HANDOVER §6.2).

Cosa controllare nel riepilogo:

- **numeri stampati**: devono essere `ricostruiti` per i due PDF, e le etichette devono
  partire da 301 e da e977. Se dicono `NON trovati`, il criterio ricade su `sezione` e la
  nota userà i numeri di sezione: corretto come ripiego, ma sui due PDF è un difetto da
  indagare, non un esito accettabile.
- **criterio riferimenti**: `pagina` per i PDF, `sezione` per l'HTML di Frontiers.
- **tabelle riconosciute**: la Tabella 1 del Palmer (p. 313) è stampata **di traverso** e
  `find_tables()` non la vede: viene ricostruita a parte, riga per riga, dalle coordinate
  raddrizzate. Le tabelle della linea guida sono a griglia e passano da `find_tables()`.
  `pdfplumber` (HANDOVER §8) non è servito.
- **token stimati**: la linea guida deve restare sotto `soglia_token_avviso`, altrimenti il
  taglio per sezioni via TOC smette di essere rimandabile (§6.2).
- Con `--text`, che l'ordine di lettura sulle due colonne sia giusto e che le tabelle non
  compaiano due volte, una formattata e una linearizzata.
