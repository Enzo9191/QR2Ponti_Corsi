#!/usr/bin/env python3
"""
Aggiorna corsi.json leggendo il planning ufficiale (PDF) pubblicato su
https://www.duepontisportingclub.it/#corsi

Nessuna AI: il PDF (esportato da Excel) contiene testo vero con coordinate. Le tabelle
vengono ricostruite dalle posizioni di intestazioni dei giorni, etichette orarie,
testi delle celle e simboli colorati di intensità.

Esito:
  - planning invariato               -> nessuna modifica (exit 0)
  - planning nuovo e letto bene      -> corsi.json aggiornato, status.ok = true (exit 0)
  - errore (sito, PDF, struttura...) -> si mantengono le ultime lezioni valide e
                                        status.ok = false con un codice parlante,
                                        leggibile da chi usa corsi.json (exit 1).

Uso: python scripts/aggiorna_corsi.py [--pdf file.pdf] [--force] [--out corsi.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field

import pdfplumber

SCHEMA_VERSION = 1
SITE_HOME = "https://www.duepontisportingclub.it/"
SITE_CORSI = "https://www.duepontisportingclub.it/#corsi"
USER_AGENT = "Mozilla/5.0 (compatible; DuePonti-Corsi-Bot/1.0)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "corsi.json")
KEEPALIVE_DAYS = 45

DAY_NAMES = {
    "LUNEDI": 1, "MARTEDI": 2, "MERCOLEDI": 3, "GIOVEDI": 4,
    "VENERDI": 5, "SABATO": 6, "DOMENICA": 7, "DOM": 7,
}

CATEGORIES = [
    # id, nome, colore, visibile in "Tutti", riepilogo compatto
    ("fitness", "Fitness", "#00E676", True, False),
    ("reformer", "Pilates Reformer", "#FF6FB5", True, False),
    ("acqua", "Acquafitness", "#4FC3F7", True, False),
    ("cycling", "Group Cycling", "#FFD54F", True, False),
    ("nuoto", "Nuoto Master", "#64B5F6", True, False),
    ("running", "Running", "#FF8A65", True, False),
    ("danza", "Danza", "#CE93D8", True, False),
    ("pesi", "Sala Pesi", "#AED581", False, True),
    ("nuoto_libero", "Nuoto Libero", "#81D4FA", False, True),
    ("agonismo", "Agonismo Nuoto", "#90A4AE", False, False),
]

# Tabelle attese nel PDF: chiave -> (regex titolo, codice per gli errori, minimo lezioni)
TABLES = {
    "rainbow": (r"SALA RAINBOW", "SALA-RAINBOW", 30),
    "star": (r"SALA STAR", "SALA-STAR", 25),
    "running": (r"RUNNING", "RUNNING", 2),
    "cycling": (r"GROUP CYCLING", "GROUP-CYCLING", 5),
    "pesi": (r"SALA PESI", "SALA-PESI", 15),
    "acqua": (r"ACQUAFITNESS", "ACQUAFITNESS", 8),
    "reformer": (r"PILATES REFORMER", "PILATES-REFORMER", 12),
    "nuoto_libero": (r"NUOTO LIBERO\s*-", "NUOTO-LIBERO", 15),
    "master": (r"NUOTO MASTER", "NUOTO-MASTER", 5),
    "agonismo": (r"AGONISMO NUOTO", "AGONISMO-NUOTO", 5),
}

CONTACTS = [
    {"label": "Segreteria Fitness", "value": "06 3339360", "tel": "063339360"},
    {"label": "Segreteria Nuoto", "value": "06 3330266", "tel": "063330266"},
    {"label": "Prenotazione Reformer", "value": "392 483 3107", "tel": "3924833107"},
]

ACRONYMS = {"FTC", "HDP", "TRX", "GAG", "G.A.G.", "DP", "A", "B"}
WATERMARK_RE = re.compile(
    r"La Direzione si riserva il diritto di variare il Planning in qualsiasi momento\s*-\s*rev\s*(\d{1,2}/\d{1,2}/\d{4})"
)
RANGE_RE = re.compile(r"(\d{1,2})\s*[.:,]\s*(\d{2})\s*-\s*(\d{1,2})\s*[.:,]\s*(\d{2})")
LABEL_RE = re.compile(r"^(\d{1,2})\s*[.:,]\s*(\d{2})$")


class CorsiError(Exception):
    """Errore con codice parlante (es. CORSI-E12-ORARI-NON-TROVATI-SALA-STAR)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ============================================================================ download

def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def read_site() -> dict:
    try:
        html = http_get(SITE_HOME).decode("utf-8", errors="replace")
    except Exception as e:
        raise CorsiError("CORSI-E01-SITO-NON-RAGGIUNGIBILE", f"Impossibile scaricare {SITE_HOME}: {e}")

    start = html.find('id="corsi"')
    section = html[start:start + 60000] if start >= 0 else html
    pdf = re.search(r'href="(https://www\.duepontisportingclub\.it/wp-content/uploads/[^"]+PLANNING[^"]*\.pdf)"',
                    section, re.IGNORECASE)
    if not pdf:
        raise CorsiError("CORSI-E02-PDF-NON-TROVATO-SUL-SITO",
                         "Nella sezione #corsi del sito non c'è più il link al PDF del planning.")
    cass = re.search(r'href="(https://www\.duepontisportingclub\.it/wp-content/uploads/[^"]+Cassiantica[^"]*\.pdf)"',
                     section, re.IGNORECASE)

    posters = []
    labels = {
        "corsi-fitness": "Group Cycling · Sala Pesi · Acquafitness",
        "nuoto-libero": "Nuoto",
        "acqua-fitness-dueponti": "Corsi Fitness",
        "pilates-dueponti": "Pilates Reformer",
    }
    img_re = re.compile(r'<img[^>]+src="(https://www\.duepontisportingclub\.it/wp-content/uploads/[^"]+)"')
    for mid, label in labels.items():
        i = html.find(f'id="{mid}"')
        if i < 0:
            continue
        body_start = html.find(">", i) + 1        # salta il tag della modale stessa
        ends = [k for k in (html.find("lqd-modal lity-hide", body_start),
                            html.find("/.lqd-modal-content", body_start)) if k > 0]
        j = min(ends) if ends else body_start + 5000
        urls = list(dict.fromkeys(img_re.findall(html[body_start:j if j > 0 else len(html)])))
        for k, u in enumerate(urls):
            suffix = f" ({k + 1}/{len(urls)})" if len(urls) > 1 else ""
            posters.append({"label": label + suffix, "url": u})

    return {"pdfUrl": pdf.group(1), "cassianticaPdf": cass.group(1) if cass else None, "posters": posters}


# ============================================================================ PDF -> parole

@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    font: str

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def bold(self) -> bool:
        return "Bold" in self.font


@dataclass
class Shape:
    kind: str  # A (triangolo rosso) / M (cerchio verde) / B (quadrato giallo)
    cx: float
    cy: float


@dataclass
class PageData:
    index: int
    words: list[Word]
    shapes: list[Shape]
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)  # x0, x1, top, bottom
    revisions: list[str] = field(default_factory=list)


def _color(c) -> tuple:
    if c is None:
        return ()
    if isinstance(c, (int, float)):
        return (float(c),)
    return tuple(float(x) for x in c)


def _intensity_from_color(col: tuple) -> str | None:
    if len(col) == 3:
        r, g, b = col
        if r > 0.8 and g < 0.3 and b < 0.3:
            return "A"
        if r < 0.3 and g > 0.5 and b < 0.5:
            return "M"
        if r > 0.8 and g > 0.8 and b < 0.4:
            return "B"
    if len(col) == 4:  # CMYK
        c, m, y, k = col
        if m > 0.7 and y > 0.7 and c < 0.3:
            return "A"
        if c > 0.6 and y > 0.6 and m < 0.4:
            return "M"
        if y > 0.7 and c < 0.3 and m < 0.3:
            return "B"
    return None


def load_pages(pdf_bytes: bytes) -> list[PageData]:
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise CorsiError("CORSI-E04-PDF-NON-LEGGIBILE", f"Il file scaricato non è un PDF leggibile: {e}")

    pages = []
    with pdf:
        for idx, page in enumerate(pdf.pages):
            chars = page.chars
            stream = "".join(c["text"] for c in chars)
            drop: set[int] = set()
            revs = []
            for m in WATERMARK_RE.finditer(stream):
                revs.append(m.group(1))
                drop.update(range(m.start(), m.end()))
            keep_ids = {id(c) for i, c in enumerate(chars)
                        if i not in drop and c.get("upright", True) and c["text"].strip()}
            filtered = page.filter(lambda o: o.get("object_type") != "char" or id(o) in keep_ids)
            raw = filtered.extract_words(keep_blank_chars=False, x_tolerance=0.8, y_tolerance=2,
                                         extra_attrs=["size", "fontname"])
            words = merge_phrases([Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], w["size"],
                                        w["fontname"]) for w in raw])

            shapes = []
            for c in page.curves:
                kind = _intensity_from_color(_color(c.get("non_stroking_color")))
                if kind and c.get("fill") and 3 < c["width"] < 14 and 3 < c["height"] < 14:
                    shapes.append(Shape(kind, (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2))
            for r in page.rects:
                kind = _intensity_from_color(_color(r.get("non_stroking_color")))
                if kind == "B" and r.get("fill") and 3 < r["width"] < 12 and 3 < r["height"] < 12:
                    shapes.append(Shape(kind, (r["x0"] + r["x1"]) / 2, (r["top"] + r["bottom"]) / 2))
            # Deduplica (Excel disegna riempimento e bordo come due oggetti)
            uniq = {}
            for s in shapes:
                uniq.setdefault((s.kind, round(s.cx), round(s.cy)), s)
            rects = [(r["x0"], r["x1"], r["top"], r["bottom"]) for r in page.rects if r["width"] > 2]
            pages.append(PageData(idx, words, list(uniq.values()), rects, revs))
    return pages


def merge_phrases(words: list[Word]) -> list[Word]:
    """Unisce le parole sulla stessa riga separate da un normale spazio (< 3pt). Le parole
    lontane (es. istruttore allineato a destra nella cella) restano frasi distinte.
    Le lettere isolate (residui grafici) vengono scartate."""
    words = sorted(words, key=lambda w: (round(w.top), w.x0))
    out: list[Word] = []
    for w in words:
        last = out[-1] if out else None
        if last and abs(last.top - w.top) < 1.5 and 0 <= w.x0 - last.x1 < max(3.0, 0.45 * w.size) \
                and abs(last.size - w.size) < 1.5 and last.bold == w.bold:
            last.text = f"{last.text} {w.text}"
            last.x1 = w.x1
            last.bottom = max(last.bottom, w.bottom)
        else:
            out.append(Word(w.text, w.x0, w.x1, w.top, w.bottom, w.size, w.font))
    return [w for w in out if not (len(w.text) == 1 and w.text.isalpha() and w.size < 6)]


# ============================================================================ geometria tabelle

@dataclass
class Table:
    key: str
    code: str
    page: PageData
    top: float      # y del titolo
    bottom: float   # y del titolo successivo / fine pagina
    columns: list[tuple[int, float, float]] = field(default_factory=list)  # (giorno, x_min, x_max)
    header_y: float = 0.0


def find_tables(pages: list[PageData]) -> dict[str, Table]:
    found: dict[str, Table] = {}
    titles = []  # (page, y, key)
    for p in pages:
        for w in p.words:
            if w.size < 10:
                continue
            for key, (pattern, code, _) in TABLES.items():
                if key not in found and re.search(pattern, w.text, re.IGNORECASE) and not w.text.startswith("*"):
                    found[key] = Table(key, code, p, w.top, 10_000)
                    titles.append((p.index, w.top, key))
    # Fine tabella = titolo successivo sulla stessa pagina
    for key, t in found.items():
        later = [y for (pi, y, k) in titles if pi == t.page.index and y > t.top + 1]
        t.bottom = min(later) if later else 10_000
    for key, (_, code, _) in TABLES.items():
        if key not in found:
            raise CorsiError(f"CORSI-E10-TABELLA-MANCANTE-{code}",
                             f"Nel PDF non c'è più la tabella '{code}' (titolo cambiato o rimosso).")
    for t in found.values():
        _find_columns(t)
    return found


def _find_columns(t: Table) -> None:
    heads = []
    for w in t.page.words:
        if not (t.top < w.top < t.bottom):
            continue
        name = w.text.upper().replace("Ì", "I").strip()
        if name in DAY_NAMES:
            heads.append((w.top, DAY_NAMES[name], w))
    if not heads:
        raise CorsiError(f"CORSI-E11-GIORNI-NON-TROVATI-{t.code}",
                         f"Nella tabella '{t.code}' non trovo l'intestazione LUNEDI…SABATO.")
    first_y = min(h[0] for h in heads)
    row = sorted([h for h in heads if abs(h[0] - first_y) < 4], key=lambda h: h[2].cx)
    days = [d for _, d, _ in row]
    if days[:5] != [1, 2, 3, 4, 5]:
        raise CorsiError(f"CORSI-E11-GIORNI-NON-TROVATI-{t.code}",
                         f"Nella tabella '{t.code}' i giorni non sono nell'ordine atteso: {days}.")
    centers = [h[2].cx for h in row]
    half = (centers[1] - centers[0]) / 2
    t.header_y = max(h[2].bottom for h in row)
    t.columns = []
    for i, (_, day, w) in enumerate(row):
        left = (centers[i - 1] + centers[i]) / 2 if i > 0 else centers[i] - half
        right = (centers[i] + centers[i + 1]) / 2 if i + 1 < len(centers) else centers[i] + half
        t.columns.append((day, left, right))


def column_of(t: Table, x: float) -> int | None:
    for day, left, right in t.columns:
        if left <= x < right:
            return day
    return None


def body_bottom(t: Table) -> float:
    """Fine del corpo tabella: prima nota '* …' lunga o primo testo grande sotto l'intestazione."""
    left = t.columns[0][1] if t.columns else 0
    stops = [w.top for w in t.page.words if t.header_y < w.top < t.bottom and w.x0 > left - 1
             and ((w.text.startswith("*") and len(w.text) > 15) or w.size >= 9)]
    return min(stops) if stops else t.bottom


def body_words(t: Table) -> list[Word]:
    """Parole del corpo tabella, escluse note a piè di tabella e testi più larghi di 2 colonne."""
    bottom = body_bottom(t)
    colw = (t.columns[-1][2] - t.columns[0][1]) / max(1, len(t.columns)) if t.columns else 1e9
    return [w for w in t.page.words if t.header_y + 0.5 < w.top < bottom - 0.5
            and (w.x1 - w.x0) < 2 * colw]


def time_labels(t: Table) -> list[tuple[float, str]]:
    """Etichette orarie nella colonna di sinistra (es. '9' + '.00' su due parole)."""
    left_edge = t.columns[0][1]
    cand = sorted([w for w in body_words(t) if w.x1 <= left_edge + 2], key=lambda w: (round(w.top), w.x0))
    lines: list[list[Word]] = []
    for w in cand:
        if lines and abs(lines[-1][0].cy - w.cy) < 4:
            lines[-1].append(w)
        else:
            lines.append([w])
    out = []
    for ln in lines:
        txt = "".join(w.text for w in sorted(ln, key=lambda w: w.x0)).replace(" ", "")
        m = LABEL_RE.match(txt)
        if m:
            out.append((min(w.top for w in ln), f"{int(m.group(1)):02d}:{m.group(2)}"))
        elif re.fullmatch(r"\d{1,2}", txt):
            out.append((min(w.top for w in ln), f"{int(txt):02d}:00"))
    out.sort()
    return out


def label_bands(t: Table, labels: list[tuple[float, str]]) -> list[tuple[float, float, str]]:
    """Fasce delle righe orarie: i bordi orizzontali delle celle nella colonna degli orari
    (rettangoli sottili e bordi dei rettangoli) delimitano le righe; ogni fascia che contiene
    una sola etichetta appartiene a quell'orario."""
    left_edge = t.columns[0][1]
    thin, edges = set(), set()
    for x0, x1, top, bottom in t.page.rects:
        if x0 < left_edge - 5 and x1 <= left_edge + 12 and (x1 - x0) > 10 and t.header_y < top < t.bottom:
            edges.update((round(top, 1), round(bottom, 1)))
            if bottom - top <= 1.6:
                thin.add(round(top, 1))     # bordo orizzontale di una cella oraria
    ys = sorted(thin if len(thin) >= 4 else edges)
    bands = []
    for a, b in zip(ys, ys[1:]):
        if b - a < 4:
            continue
        inside = [lab for ly, lab in labels if a - 1 <= ly + 3 <= b + 1]
        if len(inside) == 1:
            bands.append((a, b, inside[0]))
    return bands


def label_for_band(bands: list[tuple[float, float, str]], labels, y: float) -> str | None:
    """Orario della riga di un corso. Prima la cella oraria con una sola etichetta che contiene il
    titolo; altrimenti la prima etichetta allineata o più in basso (il titolo sta in cima alla riga,
    l'etichetta è centrata verticalmente)."""
    for top, bot, lab in bands:
        if top - 1 <= y <= bot:
            return lab
    for ly, lab in labels:
        if ly + 3 >= y - 4:
            return lab
    return labels[-1][1] if labels else None


def label_for(labels: list[tuple[float, str]], y: float) -> str | None:
    """Etichetta oraria della riga che contiene y: le righe sono delimitate dai punti medi
    tra etichette consecutive (il nome del corso può stare sopra la baseline dell'etichetta)."""
    if not labels:
        return None
    for i, (ly, lt) in enumerate(labels):
        upper = (ly + labels[i + 1][0]) / 2 if i + 1 < len(labels) else float("inf")
        if y < upper:
            return lt
    return labels[-1][1]


# ============================================================================ testo

def is_upper(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def pretty(name: str) -> str:
    name = re.sub(r"\s+", " ", name.replace("*", "")).strip(" -")
    if not is_upper(name):
        up, rest = split_case(name)
        return (pretty(up) + " " + rest).strip() if up else name
    out = []
    for word in name.split(" "):
        bare = word.strip("()")
        if bare in ACRONYMS or re.fullmatch(r"(?:[A-Z]\.){2,}", bare):
            out.append(word)
        elif word.startswith("("):
            out.append(word.lower())
        else:
            out.append("&".join(seg[:1] + seg[1:].lower() for seg in word.split("&")))
    s = " ".join(out)
    return re.sub(r"\((\w)", lambda m: "(" + m.group(1).lower(), s)


def pretty_person(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\.?:$", ".", name)          # "Francesco D.P:" -> "Francesco D.P."
    return " ".join(p[:1] + p[1:].lower() if is_upper(p) and len(p) > 2 and "." not in p else p
                    for p in name.split(" "))


def split_teacher(raw: str) -> tuple[str | None, str | None]:
    """'Simone P. (Area Funz.)' -> ('Simone P.', 'Area funzionale')."""
    room = None
    m = re.search(r"\(([^)]*)\)", raw)
    if m:
        inner = m.group(1).strip()
        low = inner.lower()
        if "area funz" in low:
            room = "Area funzionale"
        elif "calcio" in low:
            room = "Campo " + inner
        else:
            room = inner[:1].upper() + inner[1:]
        raw = (raw[:m.start()] + raw[m.end():]).strip()
    raw = pretty_person(raw)
    return (raw or None), room


def hhmm(h: str, m: str) -> str:
    return f"{int(h):02d}:{m}"


def lines_of(words: list[Word]) -> list[list[Word]]:
    out: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if out and abs(out[-1][0].top - w.top) < 2.5:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w.x0) for l in out]


# ============================================================================ lezioni

def lesson(day, start, end, name, teacher, cat, room, intensity=None, note=None, booking=False) -> dict:
    return {"d": day, "s": start, "e": end, "n": name, "t": teacher, "c": cat,
            "r": room, "i": intensity, "x": note, "b": booking}


def footnote(t: Table) -> str | None:
    for w in t.page.words:
        if t.top < w.top < t.bottom and w.text.startswith("*") and len(w.text) > 15:
            return w.text.lstrip("* ").strip()
    return None


def split_case(text: str) -> tuple[str, str]:
    """'BODY SCULPT Fat' -> ('BODY SCULPT', 'Fat'); 'HYROXFIT (Area Funz.)' -> ('HYROXFIT', '(Area Funz.)')."""
    parts = text.split(" ")
    i = 0
    while i < len(parts) and (is_upper(parts[i]) or not any(c.isalpha() for c in parts[i])) \
            and not parts[i].startswith("("):
        i += 1
    return " ".join(parts[:i]).strip(), " ".join(parts[i:]).strip()


def assign_intensities(t: Table, blocks: list[dict]) -> None:
    """Ogni simbolo (▲ ● ■) va al corso il cui titolo è subito alla sua destra/in basso."""
    for sh in t.page.shapes:
        if sh.cy < t.header_y + 1:
            continue  # legenda
        best, best_cost = None, 1e9
        for b in blocks:
            dx = b["x"] - sh.cx
            if not (-6 <= dx <= 70):
                continue
            if not (b["y"] - 9 <= sh.cy <= b["y1"] + 5):
                continue
            cost = abs(sh.cy - b["y"]) + 0.25 * max(dx, 0)
            if cost < best_cost:
                best, best_cost = b, cost
        if best is not None and best.get("intensity") is None:
            best["intensity"] = sh.kind


def parse_fitness_room(t: Table, room: str) -> list[dict]:
    """Sala Rainbow / Sala Star: NOME in maiuscolo, istruttore in minuscolo; blocchi Scuola Danza
    con orario esplicito e istruttore in maiuscolo."""
    labels = time_labels(t)
    if len(labels) < 5:
        raise CorsiError(f"CORSI-E12-ORARI-NON-TROVATI-{t.code}",
                         f"Nella tabella '{t.code}' non trovo le etichette orarie (8.00, 9.00, …).")
    bands = label_bands(t, labels)
    note_star = footnote(t)
    first_label_y = labels[0][0] - 12
    all_blocks: list[dict] = []
    for day, left, right in t.columns:
        col = [w for w in body_words(t) if left <= w.cx < right and w.top >= first_label_y]
        blocks: list[dict] = []
        cur: dict | None = None

        def new_block(w: Word, title: str, timed=False, rng=None) -> dict:
            return {"day": day, "range": rng, "title": title, "teacher": [], "y": w.top, "y1": w.bottom,
                    "x": w.x0, "timed": timed, "intensity": None}

        def close():
            nonlocal cur
            if cur and cur["title"]:
                blocks.append(cur)
            cur = None

        for line in lines_of(col):
            text = " ".join(w.text for w in line)
            rng = RANGE_RE.search(text)
            if rng and len(re.sub(r"[\d.:,\s-]", "", text)) == 0:
                close()
                cur = new_block(line[0], "", True, rng)
                continue
            for w in line:
                if w.text.upper() == "NEW":
                    continue
                up, rest = split_case(w.text)
                if cur is None:
                    cur = new_block(w, "")
                gap = w.top - cur["y1"]
                if cur["timed"]:
                    if not cur["title"]:
                        cur["title"] = w.text
                        cur["y"], cur["x"] = w.top, w.x0
                    elif cur["title"].count("(") > cur["title"].count(")") or w.text.startswith("("):
                        cur["title"] += " " + w.text
                    elif not cur["teacher"] and gap < 12:
                        cur["teacher"].append(w.text)
                    else:
                        close()
                        cur = new_block(w, up or w.text)
                        if rest and up:
                            cur["teacher"].append(rest)
                elif up:
                    if cur["title"] and (cur["teacher"] or gap > 9):
                        close()
                        cur = new_block(w, up)
                    else:
                        if not cur["title"]:
                            cur["y"], cur["x"] = w.top, w.x0
                        cur["title"] = (cur["title"] + " " + up).strip()
                    if rest:
                        cur["teacher"].append(rest)
                else:
                    cur["teacher"].append(w.text)
                cur["y1"] = max(cur["y1"], w.bottom)
        close()
        all_blocks.extend(blocks)

    assign_intensities(t, [b for b in all_blocks if not b["timed"] or b["teacher"]])

    result = []
    for b in all_blocks:
        day = b["day"]
        name = b["title"]
        raw_teacher = re.sub(r"\s+", " ", " ".join(b["teacher"])).strip()
        # Sottotitolo sotto il nome (es. BODY SCULPT / Fat Burning / Luigi P)
        m = re.match(r"^(Fat\s+Burning)\b\s*(.*)$", raw_teacher)
        if m:
            name += " " + m.group(1)
            raw_teacher = m.group(2)
        teacher, room_override = split_teacher(raw_teacher) if raw_teacher else (None, None)
        booking = "*" in name
        if b["range"]:
            r = b["range"]
            start, end = hhmm(r.group(1), r.group(2)), hhmm(r.group(3), r.group(4))
        else:
            start, end = label_for_band(bands, labels, b["y"]), None
        if not start:
            raise CorsiError(f"CORSI-E20-ORARIO-NON-VALIDO-{t.code}",
                             f"Lezione '{name}' ({t.code}, giorno {day}) senza orario riconoscibile.")
        is_dance = b["timed"] and (not raw_teacher or is_upper(raw_teacher))
        result.append(lesson(
            day, start, end, pretty(name), teacher, "danza" if is_dance else "fitness",
            room_override or room,
            None if is_dance else b["intensity"],
            (note_star[:1].upper() + note_star[1:].lower()) if booking and note_star else None,
            booking,
        ))
    return result


def parse_label_grid(t: Table, mode: str) -> list[dict]:
    """Tabelle a righe orarie senza orari nelle celle: Acquafitness, Group Cycling, Running."""
    labels = time_labels(t)
    if len(labels) < 2:
        raise CorsiError(f"CORSI-E12-ORARI-NON-TROVATI-{t.code}",
                         f"Nella tabella '{t.code}' non trovo le etichette orarie.")
    bounds = []
    for i, (y, lab) in enumerate(labels):
        y_top = (labels[i - 1][0] + y) / 2 + 3 if i > 0 else y - 8
        y_bot = (y + labels[i + 1][0]) / 2 + 3 if i + 1 < len(labels) else y + 30
        bounds.append((y_top, y_bot, lab))
    note = footnote(t)
    title_booking = "*" in next((w.text for w in t.page.words if abs(w.top - t.top) < 3 and w.size >= 10), "")
    out = []
    for day, left, right in t.columns:
        for y_top, y_bot, lab in bounds:
            cell = [w for w in body_words(t) if left <= w.cx < right and y_top <= w.top < y_bot]
            if not cell:
                continue
            lns = [" ".join(w.text for w in l) for l in lines_of(cell)]
            if mode == "cycling":
                out.append(lesson(day, lab, None, "Group Cycling", pretty_person(" ".join(lns)), "cycling",
                                  "Palestra outdoor", None,
                                  "Prenotare in segreteria il giorno prima" if title_booking else None,
                                  title_booking))
            elif mode == "running":
                place = pretty(lns[0])
                teacher = pretty_person(" ".join(lns[1:])) if len(lns) > 1 else None
                out.append(lesson(day, lab, None, "Running", teacher, "running", place))
            else:  # acqua
                name_parts = [lns[0]]
                rest = lns[1:]
                while rest and (rest[0].startswith("(") or name_parts[-1].count("(") > name_parts[-1].count(")")):
                    name_parts.append(rest.pop(0))
                name = " ".join(name_parts)
                star = "*" in name
                out.append(lesson(day, lab, None, name.replace("*", "").strip(),
                                  pretty_person(" ".join(rest)) if rest else None, "acqua", "Piscina", None,
                                  (note[:1].upper() + note[1:]) if star and note else None, False))
    return out


def range_blocks(t: Table, day: int, left: float, right: float) -> list[tuple[list[re.Match], list[str], float]]:
    """Divide una colonna in blocchi che iniziano con un intervallo orario 'hh.mm - hh.mm'."""
    col = [w for w in body_words(t) if left <= w.cx < right]
    blocks: list[tuple[list[re.Match], list[str], float]] = []
    last_bottom = None
    for line in lines_of(col):
        text = " ".join(w.text for w in line)
        rng = RANGE_RE.search(text)
        if not rng and last_bottom is not None and line[0].top - last_bottom > 25:
            break   # fine tabella: quello che segue non appartiene alle celle
        last_bottom = max(w.bottom for w in line)
        if rng:
            rest = (text[:rng.start()] + " " + text[rng.end():]).strip()
            if blocks and not blocks[-1][1]:
                blocks[-1][0].append(rng)          # più intervalli consecutivi (es. Nuoto Master)
                if rest:
                    blocks[-1][1].append(rest)
            else:
                blocks.append(([rng], [rest] if rest else [], line[0].top))
        elif blocks:
            blocks[-1][1].append(text)
    return blocks


def parse_pesi(t: Table) -> list[dict]:
    out = []
    for day, left, right in t.columns:
        for rngs, texts, _ in range_blocks(t, day, left, right):
            for r in rngs:
                who = pretty_person(" ".join(texts)) if texts else None
                out.append(lesson(day, hhmm(r.group(1), r.group(2)), hhmm(r.group(3), r.group(4)),
                                  "Trainer in sala pesi", who, "pesi", "Sala Pesi"))
    return out


def parse_reformer(t: Table) -> list[dict]:
    out = []
    for day, left, right in t.columns:
        for rngs, texts, _ in range_blocks(t, day, left, right):
            texts = [x for x in texts if x.strip()]
            novita = any("NEW" in x.split() for x in texts)
            texts = [re.sub(r"\bNEW\b", "", x).strip() for x in texts]
            texts = [x for x in texts if x]
            extra = [x for x in texts if x.startswith("(")]
            main = [x for x in texts if not x.startswith("(")]
            if not main:
                continue
            name = main[0]
            teacher = main[-1] if len(main) > 1 else None
            note = "Prenotazione obbligatoria · max 9 persone"
            if extra:
                note += " · " + extra[0].strip("()")
            if novita:
                note += " · Novità"
            r = rngs[0]
            out.append(lesson(day, hhmm(r.group(1), r.group(2)), hhmm(r.group(3), r.group(4)), pretty(name),
                              pretty_person(teacher) if teacher else None, "reformer", "Studio Pilates (terrazza)",
                              None, note, True))
    return out


def parse_nuoto_libero(t: Table) -> list[dict]:
    out = []
    for day, left, right in t.columns:
        for rngs, texts, _ in range_blocks(t, day, left, right):
            joined = " ".join(texts)
            if "no nuoto" in joined.lower():
                continue
            m = re.search(r"(\d+)\s*corsi[ae]", joined, re.IGNORECASE)
            lanes = int(m.group(1)) if m else None
            teacher_txt = re.sub(r"(\d+)\s*corsi[ae]", "", joined, flags=re.IGNORECASE).replace("*", "").strip()
            controlled = bool(teacher_txt)
            r = rngs[0]
            note = (f"{lanes} {'corsia' if lanes == 1 else 'corsie'}" if lanes else "") + \
                (" · istruttore a disposizione" if controlled else "")
            out.append(lesson(day, hhmm(r.group(1), r.group(2)), hhmm(r.group(3), r.group(4)),
                              "Nuoto libero controllato" if controlled else "Nuoto libero",
                              pretty_person(teacher_txt) if controlled else None, "nuoto_libero", "Vasca grande",
                              None, note.strip(" ·") or None))
    return out


def parse_master(t: Table) -> list[dict]:
    out = []
    for day, left, right in t.columns:
        for rngs, texts, _ in range_blocks(t, day, left, right):
            room = "Vasca grande"
            teacher = None
            for x in texts:
                if is_upper(x):
                    room = pretty(x)
                else:
                    teacher = pretty_person(x)
            for r in rngs:
                out.append(lesson(day, hhmm(r.group(1), r.group(2)), hhmm(r.group(3), r.group(4)),
                                  "Allenamento Nuoto Master", teacher, "nuoto", room))
    return out


def parse_agonismo(t: Table) -> list[dict]:
    out = []
    names = {"ESORD. A E CAT": "Esordienti A e Categoria", "ESORDIENTI B": "Esordienti B"}
    for day, left, right in t.columns:
        for rngs, texts, _ in range_blocks(t, day, left, right):
            if not texts:
                continue
            raw = texts[0]
            name = names.get(raw.upper().strip(), pretty(raw))
            teacher = pretty_person(texts[-1]) if len(texts) > 1 else None
            r = rngs[0]
            out.append(lesson(day, hhmm(r.group(1), r.group(2)), hhmm(r.group(3), r.group(4)), name, teacher,
                              "agonismo", "Vasca grande"))
    return out


# ============================================================================ descrizioni e info

def parse_descriptions(pages: list[PageData]) -> dict[str, str]:
    """Descrizioni dei corsi: paragrafi che iniziano con il NOME in grassetto.
    Pagine: quella con 'CORSI ALTA INTENSITA' e il riquadro 'TROVA IL TUO REFORMER'."""
    desc: dict[str, str] = {}
    for p in pages:
        texts = " ".join(w.text.upper() for w in p.words)
        if "CORSI ALTA INTENSIT" in texts:
            start_y = 0.0
        elif "TROVA IL TUO REFORMER" in texts:
            start_y = next(w.top for w in p.words if "TROVA IL TUO REFORMER" in w.text.upper())
        else:
            continue
        words = sorted([w for w in p.words if w.top > start_y + 1], key=lambda w: (round(w.top), w.x0))
        current, buf = None, []
        for w in words:
            key_like = w.bold and is_upper(w.text) and w.x0 < 70 and len(w.text) < 45 \
                and "INTENSIT" not in w.text.upper()
            if key_like:
                if current and buf:
                    desc[current] = " ".join(buf)
                current, buf = w.text.strip(" —-"), []
            elif current is not None:
                if w.bold and is_upper(w.text) and not buf:
                    continue          # es. "CORE & LIGHT LEGS — POWERHOUSE"
                if "Italic" in w.font:
                    if current and buf:
                        desc[current] = " ".join(buf)
                    current, buf = None, []
                    continue          # sottotitoli in corsivo del box Reformer
                buf.append(w.text)
        if current and buf:
            desc[current] = " ".join(buf)
    clean = {}
    for k, v in desc.items():
        key = re.sub(r"\s+", " ", k).strip().upper()
        key = re.sub(r"\s*—.*$", "", key).strip()          # "CORE & LIGHT LEGS — POWERHOUSE"
        v = re.sub(r"\s+", " ", v).strip().lstrip(":;,. ").strip()
        if len(v) > 20:
            clean[key] = v
    aliases = {"KICK BOXE FIT": "KICK BOXING FIT", "GAG PILATES": "G.A.G. PILATES", "POSTURAL": "POSTURAL PILATES",
               "AKY PUMP": "AKI PUMP", "H.D.P.": "HDP", "DP DANCE": "BALLANDO CON STEFANO ORADEI",
               "TOTAL FUSION": "TOTAL BODY FUSION"}
    for a, b in aliases.items():
        if a in clean and b not in clean:
            clean[b] = clean[a]
    return clean


def parse_reformer_info(t: Table) -> str | None:
    lines = []
    for w in t.page.words:
        u = w.text.upper()
        if any(k in u for k in ("POSTI LIMITATI", "DURATA", "LEZIONE SINGOLA", "PACCHETTO", "PRENOTAZIONE OBBLIGATORIA")):
            lines.append((w.top, w.text.strip()))
    if not lines:
        return None
    return "\n".join(t[:1] + t[1:].lower() if is_upper(t) else t for _, t in sorted(lines))


# ============================================================================ validazione

def validate(lessons: list[dict], previous: list[dict] | None) -> None:
    for l in lessons:
        for k in ("s", "e"):
            v = l[k]
            if v is None:
                continue
            m = re.fullmatch(r"(\d{2}):(\d{2})", v)
            if not m or not (5 <= int(m.group(1)) <= 23) or int(m.group(2)) > 59:
                raise CorsiError("CORSI-E21-ORARIO-FUORI-RANGE",
                                 f"Orario non valido '{v}' per '{l['n']}' (giorno {l['d']}).")
        if l["e"] and l["e"] <= l["s"]:
            raise CorsiError("CORSI-E21-ORARIO-FUORI-RANGE",
                             f"Fine prima dell'inizio per '{l['n']}' (giorno {l['d']}, {l['s']}-{l['e']}).")
        if not l["n"] or len(l["n"]) > 60:
            raise CorsiError("CORSI-E22-NOME-CORSO-NON-VALIDO", f"Nome corso non valido: {l['n']!r}.")
    fitness_days = {l["d"] for l in lessons if l["c"] == "fitness"}
    if len(fitness_days) < 5:
        raise CorsiError("CORSI-E23-GIORNI-FITNESS-INSUFFICIENTI",
                         f"Lezioni fitness trovate solo nei giorni {sorted(fitness_days)}.")
    if previous:
        if len(lessons) < 0.6 * len(previous):
            raise CorsiError("CORSI-E30-CALO-ANOMALO-LEZIONI",
                             f"Lette {len(lessons)} lezioni contro {len(previous)} della versione precedente.")


# ============================================================================ main

def parse_pdf(pdf_bytes: bytes) -> tuple[list[dict], dict[str, str], dict[str, str], str]:
    pages = load_pages(pdf_bytes)
    tables = find_tables(pages)
    lessons: list[dict] = []
    per_table = {
        "rainbow": parse_fitness_room(tables["rainbow"], "Sala Rainbow"),
        "star": parse_fitness_room(tables["star"], "Sala Star"),
        "running": parse_label_grid(tables["running"], "running"),
        "cycling": parse_label_grid(tables["cycling"], "cycling"),
        "acqua": parse_label_grid(tables["acqua"], "acqua"),
        "pesi": parse_pesi(tables["pesi"]),
        "reformer": parse_reformer(tables["reformer"]),
        "nuoto_libero": parse_nuoto_libero(tables["nuoto_libero"]),
        "master": parse_master(tables["master"]),
        "agonismo": parse_agonismo(tables["agonismo"]),
    }
    for key, items in per_table.items():
        _, code, minimum = TABLES[key]
        if len(items) < minimum:
            raise CorsiError(f"CORSI-E13-TABELLA-INCOMPLETA-{code}",
                             f"Nella tabella '{code}' lette solo {len(items)} lezioni (minimo atteso {minimum}).")
        lessons.extend(items)

    info = {}
    ri = parse_reformer_info(tables["reformer"])
    if ri:
        info["reformer"] = ri
    info["club"] = ("Orari circolo: Lun–Ven 06:30–22:30 · Weekend 06:30–19:30\n"
                    "Aperto 360 giorni l'anno (chiuso Natale, 1° gennaio, Santo Stefano, Pasqua e Ferragosto)")

    revs = []
    for p in pages[:6]:
        for r in p.revisions:
            try:
                revs.append(dt.datetime.strptime(r, "%d/%m/%Y").date())
            except ValueError:
                pass
    revision = f"Planning rev. {max(revs):%d/%m/%Y}" if revs else "Planning Due Ponti SC"
    lessons.sort(key=lambda l: (l["d"], l["s"], l["c"], l["n"]))
    return lessons, parse_descriptions(pages), info, revision


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="usa un PDF locale invece di scaricarlo (test)")
    ap.add_argument("--force", action="store_true", help="rielabora anche se il PDF non è cambiato")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    previous = None
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            previous = json.load(f)

    data = dict(previous) if previous else {}
    try:
        if args.pdf:
            site = {"pdfUrl": args.pdf, "cassianticaPdf": None, "posters": []}
            with open(args.pdf, "rb") as f:
                pdf_bytes = f.read()
        else:
            site = read_site()
            try:
                pdf_bytes = http_get(site["pdfUrl"], timeout=60)
            except Exception as e:
                raise CorsiError("CORSI-E03-DOWNLOAD-PDF-FALLITO", f"Download di {site['pdfUrl']} fallito: {e}")
        sha = hashlib.sha256(pdf_bytes).hexdigest()

        prev_ok = bool(previous and previous.get("status", {}).get("ok"))
        prev_sha = (previous or {}).get("source", {}).get("pdfSha256")
        if prev_ok and prev_sha == sha and not args.force:
            last = dt.datetime.fromisoformat(previous.get("generatedAt", now_iso()))
            if (dt.datetime.now(dt.timezone.utc) - last).days < KEEPALIVE_DAYS:
                print("Planning invariato: nessun aggiornamento.")
                return 0
            print("Planning invariato: aggiornamento di mantenimento (keepalive).")

        lessons, descriptions, info, revision = parse_pdf(pdf_bytes)
        validate(lessons, (previous or {}).get("lessons") if prev_ok and prev_sha != sha else None)

        data = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": now_iso(),
            "status": {"ok": True, "code": None, "message": None, "since": None, "checkedAt": now_iso()},
            "revision": revision,
            "site": SITE_CORSI,
            "source": {"pdfUrl": site["pdfUrl"], "pdfSha256": sha},
            "planningPdf": site["pdfUrl"],
            "cassianticaPdf": site.get("cassianticaPdf") or (previous or {}).get("cassianticaPdf"),
            "posters": site.get("posters") or (previous or {}).get("posters", []),
            "categories": [{"id": i, "name": n, "color": c, "inAll": a, "compact": cp}
                           for i, n, c, a, cp in CATEGORIES],
            "descriptions": descriptions,
            "info": info,
            "contacts": CONTACTS,
            "lessons": lessons,
        }
        write(args.out, data)
        print(f"OK: {len(lessons)} lezioni · {revision}")
        return 0

    except CorsiError as e:
        return fail(args.out, data, e.code, e.message)
    except Exception as e:  # noqa: BLE001
        return fail(args.out, data, "CORSI-E99-ERRORE-IMPREVISTO", f"{type(e).__name__}: {e}")


def fail(out: str, data: dict, code: str, message: str) -> int:
    """Mantiene le ultime lezioni valide e segnala l'errore in status."""
    prev_status = data.get("status") or {}
    if prev_status.get("ok") is False and prev_status.get("code") == code:
        print(f"ERRORE {code} (già segnalato dal {prev_status.get('since')}): {message}", file=sys.stderr)
        return 1
    data["schemaVersion"] = SCHEMA_VERSION
    data["status"] = {
        "ok": False,
        "code": code,
        "message": message,
        "since": prev_status.get("since") if prev_status.get("code") == code else now_iso(),
        "checkedAt": now_iso(),
    }
    data.setdefault("lessons", [])
    data.setdefault("categories", [{"id": i, "name": n, "color": c, "inAll": a, "compact": cp}
                                   for i, n, c, a, cp in CATEGORIES])
    write(out, data)
    print(f"ERRORE {code}: {message}", file=sys.stderr)
    return 1


def write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
