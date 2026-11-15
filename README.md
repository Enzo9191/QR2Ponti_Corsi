# Corsi Due Ponti Sporting Club

Orari dei corsi del Due Ponti Sporting Club in formato JSON, ricavati ogni giorno dal
planning ufficiale (PDF) pubblicato su <https://www.duepontisportingclub.it/#corsi>.

- **Dati:** [`corsi.json`](corsi.json) sul branch `master`
  (`https://raw.githubusercontent.com/Enzo9191/QR2Ponti_Corsi/master/corsi.json`)
- **Aggiornamento:** GitHub Actions ogni giorno alle 04:17 UTC ([workflow](.github/workflows/aggiorna-corsi.yml)).
  Il file cambia solo quando cambia il PDF sul sito.
- **Lettura del PDF:** [`scripts/aggiorna_corsi.py`](scripts/aggiorna_corsi.py), parsing deterministico
  delle tabelle (nessuna AI).

## Stato e codici di errore

`corsi.json` contiene sempre l'ultima versione valida delle lezioni. Se la lettura fallisce,
`status.ok` diventa `false` e `status.code` indica il motivo:

| Codice | Significato |
|---|---|
| `CORSI-E01-SITO-NON-RAGGIUNGIBILE` | Il sito del club non risponde |
| `CORSI-E02-PDF-NON-TROVATO-SUL-SITO` | Nella sezione corsi non c'è più il link al PDF del planning |
| `CORSI-E03-DOWNLOAD-PDF-FALLITO` | Il PDF non si scarica |
| `CORSI-E04-PDF-NON-LEGGIBILE` | Il file non è un PDF leggibile |
| `CORSI-E10-TABELLA-MANCANTE-<TABELLA>` | Una tabella (es. `SALA-RAINBOW`) non c'è più o ha cambiato titolo |
| `CORSI-E11-GIORNI-NON-TROVATI-<TABELLA>` | Non trovo l'intestazione LUNEDI…SABATO |
| `CORSI-E12-ORARI-NON-TROVATI-<TABELLA>` | Non trovo le etichette orarie a sinistra |
| `CORSI-E13-TABELLA-INCOMPLETA-<TABELLA>` | Lette troppo poche lezioni in una tabella |
| `CORSI-E20-ORARIO-NON-VALIDO-<TABELLA>` | Una lezione senza orario riconoscibile |
| `CORSI-E21-ORARIO-FUORI-RANGE` | Orario impossibile (es. fine prima dell'inizio) |
| `CORSI-E22-NOME-CORSO-NON-VALIDO` | Nome corso vuoto o anomalo |
| `CORSI-E23-GIORNI-FITNESS-INSUFFICIENTI` | Lezioni fitness solo in pochi giorni |
| `CORSI-E30-CALO-ANOMALO-LEZIONI` | Molte meno lezioni rispetto alla versione precedente |
| `CORSI-E99-ERRORE-IMPREVISTO` | Errore non previsto (dettaglio in `status.message`) |

## Esecuzione locale

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/aggiorna_corsi.py            # scarica dal sito
.venv/bin/python scripts/aggiorna_corsi.py --pdf file.pdf --force   # test su un PDF locale
```
