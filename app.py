"""Hunger Games diretti da un LLM locale via Ollama: arena casuale, baccelli, oggetti, eventi, persistenza SQLite, UI Flask.

Avvio:  python -m venv .venv  &&  .venv\\Scripts\\pip install -r requirements.txt  &&  .venv\\Scripts\\python app.py
Poi apri http://localhost:5001   (env: OLLAMA_MODEL, default: llama3:8b se installato, altrimenti il primo modello · OLLAMA_URL, default http://localhost:11434)
"""
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from collections import deque
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from flask import Flask, request, send_file

from pool import ARCHETYPES, POOL

FROZEN = getattr(sys, "frozen", False)  # avviato come eseguibile PyInstaller
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # dove sta index.html
HOME = Path(sys.executable).parent if FROZEN else ROOT
# l'exe dentro dist/ del progetto condivide il DB del progetto; copiato altrove, usa quello accanto a sé
DB = HOME.parent / "hunger_games.db" if FROZEN and (HOME.parent / "app.py").exists() else HOME / "hunger_games.db"
MODEL = os.environ.get("OLLAMA_MODEL", "")  # vuoto: si usa ciò che è installato sul PC (vedi resolve_model)
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
ARENA_MIN, ARENA_MAX = 32, 64  # lato minimo e massimo dell'arena
VISION, WANDER = 5, 0.15   # raggio di visione, probabilità di un passo casuale
MAX_MEETINGS = 1           # incontri al giorno per tributo (bagno di sangue e scontro finale esclusi): meno chiamate LLM
TICK_DELAY = 0.3           # pausa tra un'ora e l'altra, per vedere i movimenti sulla mappa
SCENE_CHANCE = 0.2         # con 12 tributi o meno: probabilità che, in un'ora, un tributo da solo abbia una scena
TRAP_CHANCE = 0.3          # ogni giorno, un tributo molto intelligente piazza una trappola con questa probabilità
INTEL_HIGH = 70            # intelligenza da cui si piazzano trappole
PICKUP_CHANCE = 0.5        # chi è su un oggetto a terra, con uno slot libero, lo raccoglie con questa probabilità
SWAP_CHANCE = 0.2          # con i due slot pieni: probabilità di lasciare un oggetto per prenderne un altro
WATER_HEAL = 1             # punti vita recuperati in un'ora in un bioma d'acqua
HEAL_BIOMES = "LWP"        # lago, cascata e palude: specchi d'acqua che curano un po'
MAX_PODS = 40              # baccelli che l'utente può mettere sulla mappa
LOCK = threading.Lock()    # un solo passo di simulazione alla volta
RUN = {"status": ""}       # cosa sta facendo il Direttore (e l'ora di gioco), per la UI
CUR = "(SELECT MAX(id) FROM game_state)"  # l'edizione corrente è l'ultima creata
STATS = ("intelligence", "strength", "dexterity", "charisma", "stealth")

SYSTEM = """Sei il Direttore Supremo degli Hunger Games. Gestisci 24 tributi in un'arena.
Mantieni rigorosa coerenza con la scheda personaggio (personalità, stats) di ciascun tributo.
Fasi di gioco:
- FASE 1 (Pre-Giochi): Assegna 24 tributi a 12 Distretti. Genera sintesi interviste, reazione pubblico, quote scommesse.
- FASE 2 (Addestramento): Calcola/assegna punteggi addestramento (1-12) e statistiche finali in formato tabella/JSON.
- FASE 3 (I Giochi): Narrazione dettagliata turno per turno (giorno/notte). Ogni messaggio copre 1 giornata intera.
  Termina ogni giornata con: tributi rimanenti, lista deceduti del giorno, lista superstiti.
Scrivi sempre in italiano e rispondi solo con JSON conforme allo schema richiesto."""

DISTRICTS = {1: "beni di lusso", 2: "edilizia e Pacificatori", 3: "tecnologia", 4: "pesca", 5: "energia",
             6: "trasporti", 7: "legname", 8: "tessile", 9: "grano", 10: "allevamento", 11: "agricoltura",
             12: "miniere di carbone"}
# domanda e umore del pubblico estratti a caso: un 8B lasciato libero scrive 24 interviste uguali
QUESTIONS = ("Cosa porti nell'arena dalla tua vita di prima?", "Chi temi di più tra gli altri tributi?",
             "Qual è il tuo piano per la Cornucopia?", "Cosa diresti a chi scommette contro di te?",
             "Hai un portafortuna o un rito prima della battaglia?", "Con chi faresti un'alleanza, e perché?",
             "Qual è la tua arma segreta?", "Cosa farai se resterai l'ultimo in vita?",
             "Cosa ti manca di più di casa?", "Cosa pensi davvero di Capitol City?")
MOODS = ("delirio ed entusiasmo", "risate fragorose", "fischi e scherno", "silenzio gelido", "commozione",
         "sgomento", "cori da stadio", "inquietudine", "tenerezza", "applausi ironici", "scommesse frenetiche")
DEFAULT_ITEMS = [("Coltello", "lama corta, affilata e facile da nascondere"), ("Zaino", "pieno di razioni e corde"),
                 ("Arco", "con una manciata di frecce"), ("Borraccia", "piena d'acqua non sicura"),
                 ("Medicinali", "bende e un unguento"), ("Accetta", "pesante, ideale per colpire da vicino"),
                 ("Lanterna", "a batteria, con poca carica"), ("Mappa", "stampata male, con alcune zone cancellate")]

TRIBUTES_DDL = """{head} (
    id INTEGER PRIMARY KEY,
    edition INTEGER NOT NULL REFERENCES game_state (id),
    name TEXT NOT NULL,
    district INTEGER CHECK (district BETWEEN 1 AND 12),             -- lo assegna il Direttore in Fase 1
    physical_description TEXT,
    personality_traits TEXT,                                        -- JSON
    intelligence INTEGER CHECK (intelligence BETWEEN 1 AND 100),    -- stats NULL finché il Direttore non lo profila
    strength INTEGER CHECK (strength BETWEEN 1 AND 100),
    dexterity INTEGER CHECK (dexterity BETWEEN 1 AND 100),
    charisma INTEGER CHECK (charisma BETWEEN 1 AND 100),
    stealth INTEGER CHECK (stealth BETWEEN 1 AND 100),
    health INTEGER NOT NULL DEFAULT 100 CHECK (health BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'ALIVE' CHECK (status IN ('ALIVE', 'DEAD')),
    kills_count INTEGER NOT NULL DEFAULT 0,
    current_x INTEGER CHECK (current_x BETWEEN 0 AND 127),  -- arene fino a 64 caselle per lato
    current_y INTEGER CHECK (current_y BETWEEN 0 AND 127),
    training_score INTEGER CHECK (training_score BETWEEN 1 AND 12),  -- Fase 2
    odds TEXT                                                         -- Fase 1
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS game_state (           -- una riga per edizione dei Giochi
    id INTEGER PRIMARY KEY,                       -- numero dell'edizione
    day_number INTEGER NOT NULL DEFAULT 0,
    current_phase TEXT NOT NULL DEFAULT 'SELEZIONE',
    logs TEXT NOT NULL DEFAULT '[]',              -- JSON: diario per la UI
    event_history TEXT NOT NULL DEFAULT '[]',     -- JSON: incontri con posizione, esito, caduti
    memorial INTEGER NOT NULL DEFAULT 0 CHECK (memorial IN (0, 1)),  -- edizione della memoria
    closures INTEGER NOT NULL DEFAULT 0,          -- passi di chiusura dell'arena (cambiano solo all'alba)
    day_dead INTEGER NOT NULL DEFAULT 0,          -- caduti all'inizio della giornata corrente
    event_day INTEGER NOT NULL DEFAULT 0,         -- giorno per cui l'utente ha già risposto sull'evento
    day_event TEXT,                               -- l'evento scelto per la giornata in corso (NULL = nessuno)
    winner_id INTEGER REFERENCES tributes (id),
    arena TEXT,                                   -- JSON: arena di questa edizione (dimensioni, forma, biomi)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""" + TRIBUTES_DDL.format(head="CREATE TABLE IF NOT EXISTS tributes") + """
CREATE TABLE IF NOT EXISTS alliances (            -- alleanze: una riga per ogni verso (a→b e b→a)
    a INTEGER NOT NULL REFERENCES tributes (id),
    b INTEGER NOT NULL REFERENCES tributes (id),
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS traps (                -- trappole piazzate dai tributi intelligenti: monouso, spariscono al primo scatto
    id INTEGER PRIMARY KEY,
    edition INTEGER NOT NULL REFERENCES game_state (id),
    placer INTEGER NOT NULL REFERENCES tributes (id),
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    hit TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS items (                -- oggetti: a terra in una casella, oppure in uno dei due slot di un tributo
    id INTEGER PRIMARY KEY,
    edition INTEGER NOT NULL REFERENCES game_state (id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    holder INTEGER REFERENCES tributes (id),      -- chi lo porta (NULL = a terra)
    x INTEGER,
    y INTEGER
);
CREATE TABLE IF NOT EXISTS pods (                 -- baccelli messi dall'utente: trappole, entità o oggetti, monouso
    id INTEGER PRIMARY KEY,
    edition INTEGER NOT NULL REFERENCES game_state (id),
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'trap',            -- trap: ferma e scatta · entity: si attiva e si muove · object: rilascia un oggetto
    entity INTEGER NOT NULL DEFAULT 0 CHECK (entity IN (0, 1)),
    hp INTEGER,                                   -- punti vita dell'entità
    state TEXT NOT NULL DEFAULT 'SET' CHECK (state IN ('SET', 'USED')),
    hit TEXT NOT NULL DEFAULT '[]',
    spawned INTEGER NOT NULL DEFAULT 0            -- trappola: ha già fatto nascere la sua creatura
);
CREATE TABLE IF NOT EXISTS entities (             -- creature mobili attive: nascono da un baccello o da un evento
    id INTEGER PRIMARY KEY,
    edition INTEGER NOT NULL REFERENCES game_state (id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    hp INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ALIVE' CHECK (status IN ('ALIVE', 'DEAD')),
    zone TEXT                                     -- se non è NULL, la creatura resta nel bioma con questa lettera
);
"""


# ---------- database ----------
@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def upgrade(con):  # i database creati prima ricevono colonne e tabelle nuove senza perdere dati
    def cols(table):
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for table, col, ddl in (("game_state", "closures", "INTEGER NOT NULL DEFAULT 0"),
                            ("game_state", "day_dead", "INTEGER NOT NULL DEFAULT 0"),
                            ("game_state", "event_day", "INTEGER NOT NULL DEFAULT 0"),
                            ("game_state", "day_event", "TEXT"),
                            ("pods", "kind", "TEXT NOT NULL DEFAULT 'trap'"),
                            ("entities", "zone", "TEXT")):
        if col not in cols(table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    con.execute("UPDATE pods SET kind = 'entity' WHERE entity = 1 AND kind = 'trap'")
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name = 'tributes'").fetchone()[0] or ""
    if "AND 47" in sql:  # la tabella dei tributi aveva il limite delle mappe da 48: la si ricostruisce più larga
        con.execute(TRIBUTES_DDL.format(head="CREATE TABLE tributes_new"))
        con.execute("INSERT INTO tributes_new SELECT * FROM tributes")
        con.execute("DROP TABLE tributes")
        con.execute("ALTER TABLE tributes_new RENAME TO tributes")


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode = WAL")  # la UI legge mentre il Direttore scrive
        con.executescript(SCHEMA)
        upgrade(con)


def game(con):  # l'edizione corrente, vuota se non ne è mai stata creata una
    row = con.execute(f"SELECT * FROM game_state WHERE id = {CUR}").fetchone()
    return dict(row) if row else {"id": None, "memorial": 0, "day_number": 0, "current_phase": None,
                                  "logs": "[]", "event_history": "[]"}


def tributes(con, alive=False):
    where = " AND status = 'ALIVE'" if alive else ""
    return [dict(r) for r in con.execute(f"SELECT * FROM tributes WHERE edition = {CUR}{where} ORDER BY district, id")]


def winners(con):  # albo d'oro: il tributo vincitore di ogni edizione conclusa
    return [dict(r) for r in con.execute("SELECT t.* FROM game_state g JOIN tributes t ON t.id = g.winner_id "
                                         "ORDER BY g.id")]


def save(con, t):
    con.execute("UPDATE tributes SET health = ?, status = ?, kills_count = ?, current_x = ?, current_y = ? WHERE id = ?",
                (t["health"], t["status"], t["kills_count"], t["current_x"], t["current_y"], t["id"]))
    if t["status"] == "DEAD":  # chi muore lascia a terra ciò che portava
        con.execute("UPDATE items SET holder = NULL, x = ?, y = ? WHERE holder = ?", (t["current_x"], t["current_y"], t["id"]))


def save_ent(con, e):
    con.execute("UPDATE entities SET x = ?, y = ?, hp = ?, status = ? WHERE id = ?",
                (e["current_x"], e["current_y"], e["hp"], e["status"], e["id"]))


def entities_alive(con):  # le creature vive, con le stesse chiavi di posizione dei tributi
    return [{**dict(r), "current_x": r["x"], "current_y": r["y"]}
            for r in con.execute(f"SELECT * FROM entities WHERE edition = {CUR} AND status = 'ALIVE'")]


def set_state(con, **cols):  # aggiorna l'edizione corrente
    con.execute(f"UPDATE game_state SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = {CUR}", tuple(cols.values()))


def append(con, column, item):  # append atomico a un array JSON dell'edizione corrente
    con.execute(f"UPDATE game_state SET {column} = json_insert({column}, '$[#]', json(?)) WHERE id = {CUR}",
                (json.dumps(item, ensure_ascii=False),))


def dead_count(con):
    return con.execute(f"SELECT COUNT(*) FROM tributes WHERE edition = {CUR} AND status = 'DEAD'").fetchone()[0]


def closing(con):  # passi di chiusura dell'arena: cambiano solo all'alba, vedi close_arena
    return game(con).get("closures") or 0


def traps_cells(con):  # le trappole dei tributi ancora armate, come [x, y]
    return [[r["x"], r["y"]] for r in con.execute(f"SELECT x, y FROM traps WHERE edition = {CUR}")]


def pods_view(con):  # i baccelli ancora sulla mappa: [x, y, stato]
    return [[r["x"], r["y"], r["state"]] for r in con.execute(f"SELECT x, y, state FROM pods WHERE edition = {CUR}")]


def ents_view(con):  # le creature: [x, y, nome, stato, punti vita]
    return [[r["x"], r["y"], r["name"], r["status"], r["hp"]]
            for r in con.execute(f"SELECT x, y, name, status, hp FROM entities WHERE edition = {CUR}")]


def ground_view(con):  # gli oggetti a terra: [x, y, nome]
    return [[r["x"], r["y"], r["name"]]
            for r in con.execute(f"SELECT x, y, name FROM items WHERE edition = {CUR} AND holder IS NULL")]


def inv_view(con):  # gli oggetti portati, per tributo: {id: [nomi]}
    out = {}
    for r in con.execute(f"SELECT holder, name FROM items WHERE edition = {CUR} AND holder IS NOT NULL"):
        out.setdefault(str(r["holder"]), []).append(r["name"])
    return out


def snapshot(con, marks=None):  # com'è la partita in questo istante: la modalità lenta mostra la mappa del testo letto
    g, ts = game(con), tributes(con)
    A = json.loads(g["arena"]) if g.get("arena") else None
    return {"day": g["day_number"], "radius": zone(A, closing(con)) if A else 0, "marks": marks or {},
            "traps": traps_cells(con), "pods": pods_view(con), "ents": ents_view(con), "ground": ground_view(con),
            "inv": inv_view(con),
            "t": {t["id"]: [t["current_x"], t["current_y"], t["status"], t["health"], t["kills_count"],
                            t["district"], t["odds"]] for t in ts}}


def log(con, kind, title, text, table=None, marks=None, vs=None):
    append(con, "logs", {"kind": kind, "title": title, "text": text, "table": table, "vs": vs,
                         "frame": snapshot(con, marks)})


# ---------- arena ----------
BIOMES = {"C": "Cornucopia", "R": "Radura", "F": "Foresta", "M": "Montagne", "P": "Palude", "D": "Deserto",
          "L": "Lago", "G": "Giungla", "T": "Tundra", "V": "Vulcano", "O": "Rovine", "S": "Savana",
          "B": "Bambuseto", "H": "Scogliera", "E": "Brughiera", "W": "Cascata"}  # "X" = fuori dall'arena
BIOME_HINT = {"Cornucopia": "armi e provviste sparse, luogo di corsa e scontri", "Radura": "campo aperto, visibile da lontano",
              "Foresta": "fitta di alberi, ideale per nascondersi e cacciare", "Montagne": "rocce e vento, ampia vista ma poco riparo",
              "Palude": "fango e acqua stagnante, rallenta chi cammina", "Deserto": "sole e poca acqua",
              "Lago": "acqua aperta, si attraversa solo dove c'è una riva", "Giungla": "umida e fitta, piante e animali pericolosi",
              "Tundra": "gelo e vento, poche risorse", "Vulcano": "cenere, calore e rocce roventi",
              "Rovine": "macerie e rifugi di pietra", "Savana": "erba alta e spazi aperti, per correre e avvistare",
              "Bambuseto": "canne fitte e rumorose, ottimo riparo ma difficile da attraversare",
              "Scogliera": "rocce a picco e superfici scivolose, un passo falso è mortale",
              "Brughiera": "arbusti bassi e terreno irregolare, riparo a metà",
              "Cascata": "acqua che precipita e rapide, si attraversa solo dove c'è una riva"}
WATER = "LW"  # acqua: si attraversa solo dove confina con altra terra (un evento può aggiungerne altra)
COVER, OPEN = "FPGOMBE", "RCDTS"  # nascondigli e terreno aperto


def new_arena():
    """Arena casuale: lati 32-64, bordo ondulato attorno alla Cornucopia, biomi a macchie (Voronoi).
    La forma è stellata rispetto al centro: da ogni casella si rientra verso la Cornucopia, quindi la zona sicura
    che si restringe (distanza di Chebyshev dal centro) funziona con qualsiasi forma."""
    w, h = random.randint(ARENA_MIN, ARENA_MAX), random.randint(ARENA_MIN, ARENA_MAX)
    cx, cy = w // 2, h // 2
    waves = [(random.randint(2, 6), random.uniform(0, 2 * math.pi), random.uniform(0.03, 0.09)) for _ in range(3)]
    seeds = [(random.randrange(w), random.randrange(h), random.choice("FMPDLGTVOSBHEW"))
             for _ in range(random.randint(6, 9 + w * h // 250))]
    rows = []
    for y in range(h):
        row = ""
        for x in range(w):
            dx, dy, d = (x - cx) / (w / 2), (y - cy) / (h / 2), max(abs(x - cx), abs(y - cy))
            edge = min(0.97, 0.82 + sum(a * math.sin(k * math.atan2(dy, dx) + ph) for k, ph, a in waves))
            if math.hypot(dx, dy) > edge:
                row += "X"
            elif d <= 4:  # Cornucopia e radura delle piattaforme
                row += "C" if d <= 1 else "R"
            else:
                row += min(seeds, key=lambda s: (s[0] - x) ** 2 + (s[1] - y) ** 2)[2]
        rows.append(row)
    reach = _field(tuple(rows), (cx, cy), WATER)  # le terre chiuse dall'acqua diventano fuori dall'arena
    rows = ["".join(ch if ch == "X" or (x, y) in reach else "X" for x, ch in enumerate(row)) for y, row in enumerate(rows)]
    r0 = max(max(abs(x - cx), abs(y - cy)) for y in range(h) for x in range(w) if rows[y][x] != "X")
    return {"w": w, "h": h, "c": [cx, cy], "r0": r0, "rows": rows}


def checked_arena(raw):  # la mappa mandata dal browser (anteprima): si ricontrolla e si ricalcola il raggio
    rows = raw.get("rows") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, str) for r in rows):
        raise ValueError("mappa non valida")
    h, w = len(rows), len(rows[0])
    if not (ARENA_MIN <= w <= ARENA_MAX and ARENA_MIN <= h <= ARENA_MAX) or any(
            len(r) != w or any(ch not in BIOMES and ch != "X" for ch in r) for r in rows):
        raise ValueError("mappa non valida")
    cx, cy = w // 2, h // 2
    if rows[cy][cx] != "C":
        raise ValueError("mappa non valida: manca la Cornucopia al centro")
    r0 = max(max(abs(x - cx), abs(y - cy)) for y in range(h) for x in range(w) if rows[y][x] != "X")
    return {"w": w, "h": h, "c": [cx, cy], "r0": r0, "rows": rows}


def checked_pods(raw, A):  # i baccelli scelti dall'utente: dentro la mappa, con titolo, descrizione e punti vita se entità
    if not raw:
        return []
    if A is None:
        raise ValueError("per i baccelli serve prima la mappa")
    if not isinstance(raw, list) or len(raw) > MAX_PODS:
        raise ValueError(f"al massimo {MAX_PODS} baccelli")
    out = []
    for p in raw:
        x, y = (p.get("x"), p.get("y")) if isinstance(p, dict) else (None, None)
        if not (isinstance(x, int) and isinstance(y, int) and walkable(A, (x, y))):
            raise ValueError("un baccello è fuori dalla mappa")
        title, desc = str(p.get("title", "")).strip()[:60], str(p.get("description", "")).strip()[:500]
        if not title or not desc:
            raise ValueError("ogni baccello ha bisogno di un titolo e di una descrizione")
        kind = p.get("kind") if p.get("kind") in ("trap", "entity", "object") else "trap"
        hp = p.get("hp") if kind == "entity" else None
        if kind == "entity" and not (isinstance(hp, int) and 1 <= hp <= 100):
            raise ValueError("un'entità ha bisogno di punti vita tra 1 e 100")
        out.append({"x": x, "y": y, "title": title, "description": desc, "kind": kind, "hp": hp})
    return out


def biome_info(A, ch):  # nome e descrizione di un bioma: quelli base e quelli nati da un evento
    extra = A.get("biomes", {}).get(ch)
    if extra:
        return extra
    return {"name": BIOMES[ch], "hint": BIOME_HINT[BIOMES[ch]]}


def biome(A, x, y):
    return biome_info(A, A["rows"][y][x])["name"]


def biome_note(A, x, y):  # il bioma con ciò che offre: serve al Direttore per raccontare la strategia
    info = biome_info(A, A["rows"][y][x])
    return f"{info['name']} ({info['hint']})"


def biome_key(A, name):  # la lettera del bioma indicato per nome o per lettera, oppure None
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip().lower()
    for ch in list(BIOMES) + list(A.get("biomes", {})):
        if n == ch.lower() or n == biome_info(A, ch)["name"].lower():
            return ch
    return None


def heals(A, c):  # gli specchi d'acqua curano un po' chi ci sta dentro
    ch = A["rows"][c[1]][c[0]]
    return ch in HEAL_BIOMES or bool(A.get("biomes", {}).get(ch, {}).get("acqua"))


def walkable(A, c):  # il lago e la cascata si attraversano solo dove confinano con altra terra: fiumi sì, mare aperto no
    x, y = c
    if not (0 <= x < A["w"] and 0 <= y < A["h"]) or A["rows"][y][x] == "X":
        return False
    water = A.get("water", WATER)
    return A["rows"][y][x] not in water or any(
        0 <= x + dx < A["w"] and 0 <= y + dy < A["h"] and A["rows"][y + dy][x + dx] not in water + "X"
        for dx in (-1, 0, 1) for dy in (-1, 0, 1))


def zone(A, steps):  # ogni passo di chiusura stringe l'arena: con 5 passi resta solo la Cornucopia
    return max(0, A["r0"] - math.ceil(A["r0"] / 5) * steps)


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def pos(t):
    return t["current_x"], t["current_y"]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def clock(e):  # ore trascorse dall'inizio dei Giochi (ogni giornata parte alle 06:00)
    return (e["day"] - 1) * 24 + (e["hour"] - 6) % 24


def recent(events, day, now):  # sulla mappa: i ☠️ della giornata, alleanze/scontri/fughe solo per 4 ore
    return [e for e in events if (e["dead"] and e["day"] == day) or now - clock(e) < 4]


DIRS = {"N": (0, -1), "NE": (1, -1), "E": (1, 0), "SE": (1, 1), "S": (0, 1),
        "SO": (-1, 1), "O": (-1, 0), "NO": (-1, -1), "FERMO": (0, 0)}


@lru_cache(maxsize=8)
def _field(rows, c, water):  # distanza a piedi da ogni casella alla Cornucopia (ricerca in ampiezza: rispetta acqua e bordi)
    A = {"w": len(rows[0]), "h": len(rows), "rows": rows, "water": water}
    dist, queue = {c: 0}, deque([c])
    while queue:
        p = queue.popleft()
        for dx, dy in DIRS.values():
            n = (p[0] + dx, p[1] + dy)
            if n not in dist and walkable(A, n):
                dist[n] = dist[p] + 1
                queue.append(n)
    return dist


def field(A):
    return _field(tuple(A["rows"]), tuple(A["c"]), A.get("water", WATER))


def hunter(t):
    return t["strength"] + t["charisma"] > t["stealth"] + t["intelligence"] and t["health"] > 30


def terrain(A, c, t):  # a parità di distanza: i furtivi cercano i nascondigli, gli aggressivi il terreno aperto
    return 0 if A["rows"][c[1]][c[0]] in (OPEN if hunter(t) else COVER) else 1


def step(t, alive, r, showdown, A, rush=False):
    """Prossima casella: aggressivi verso prede deboli o la Cornucopia, furtivi via dalle minacce e verso i bordi."""
    p, C = pos(t), tuple(A["c"])
    others = [o for o in alive if o is not t]
    inside = cheb(p, C) <= r
    cells = [(p[0] + dx, p[1] + dy) for dx, dy in DIRS.values()]
    cells = [c for c in cells if walkable(A, c) and (not inside or cheb(c, C) <= r)] or [p]
    F = field(A)  # distanze a piedi: le vie passano dai ponti sui fiumi, non in linea d'aria
    if not inside:
        goal = lambda c: F.get(c, 10**6)  # fuori dalla zona sicura: rientra
    elif random.random() < WANDER:
        goal = lambda c: 0
    elif showdown or hunter(t):
        prey = [] if rush else [o for o in others if 0 < cheb(p, pos(o)) <= VISION]  # rush: tutti alle armi
        if prey:
            target = pos(min(prey, key=lambda o: cheb(p, pos(o)) + o["strength"] / 50))
            goal = lambda c: cheb(c, target)
        else:
            goal = lambda c: F.get(c, 10**6)  # verso la Cornucopia
    else:
        threats = [pos(o) for o in others if cheb(p, pos(o)) <= VISION]
        goal = (lambda c: -min(cheb(c, o) for o in threats)) if threats else (lambda c: -F.get(c, 0))
    return min(cells, key=lambda c: (goal(c), terrain(A, c, t), random.random()))


def ent_step(e, alive, A):  # una creatura insegue la preda più vicina nel raggio di visione, altrimenti vaga nel suo bioma
    p = pos(e)
    cells = [(p[0] + dx, p[1] + dy) for dx, dy in DIRS.values()]
    cells = [c for c in cells if walkable(A, c) and (not e.get("zone") or A["rows"][c[1]][c[0]] == e["zone"])] or [p]
    prey = [t for t in alive if t["status"] == "ALIVE" and 0 < cheb(p, pos(t)) <= VISION]
    if prey and random.random() > WANDER:
        target = pos(min(prey, key=lambda t: cheb(p, pos(t))))
        return min(cells, key=lambda c: (cheb(c, target), random.random()))
    return random.choice(cells)


# ---------- Game Director (Ollama, output strutturato) ----------
STR = {"type": "string"}


def obj(**props):
    return {"type": "object", "properties": props, "required": list(props)}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def integer(lo, hi):
    return {"type": "integer", "minimum": lo, "maximum": hi}


EFFECT = obj(tipo=enum("oggetti", "entita", "bioma", "cura", "danno", "resuscita", "dona_oggetto", "nessuno"),
             bioma=STR, nuovo=STR, nome=STR, descrizione=STR, quantita=integer(0, 6), valore=integer(0, 100),
             bersaglio=STR, acqua={"type": "boolean"})  # un effetto dell'evento: il gestore ne sceglie il tipo


def tag(name):  # Ollama aggiunge ":latest" ai modelli pubblicati senza tag
    return name if ":" in name else name + ":latest"


def resolve_model():
    """Modello da usare su questo PC: OLLAMA_MODEL se impostato, altrimenti llama3:8b o il primo installato.
    Restituisce (modello, None) oppure (None, motivo) se Ollama non risponde o non ha modelli."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5) as r:
            installed = [m["name"] for m in json.load(r)["models"]]
    except (OSError, ValueError, KeyError) as e:
        return None, f"Ollama non risponde su {OLLAMA} ({e}): avvialo e riprova"
    if MODEL:
        found = [m for m in installed if tag(m) == tag(MODEL)]
        return (found[0], None) if found else (None, f"il modello «{MODEL}» non è installato: ollama pull {MODEL}")
    if not installed:
        return None, "nessun modello installato in Ollama: ollama pull llama3:8b"
    return next((m for m in installed if tag(m) == "llama3:8b"), installed[0]), None


def ask(prompt, schema):
    """Interroga il Direttore. `format` = JSON Schema: la risposta è sempre JSON valido con tutte le chiavi."""
    model, problem = resolve_model()
    if problem:
        raise RuntimeError(problem)
    body = json.dumps({"model": model, "stream": False, "format": schema,
                       "options": {"temperature": 0.8, "num_ctx": 8192, "num_predict": 4096},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": prompt}]}).encode()
    with db() as con:
        ts = tributes(con)
    for _ in range(3):
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/chat", body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=900) as r:
                return unlabel(json.loads(json.load(r)["message"]["content"]), ts)
        except (OSError, ValueError, KeyError) as e:
            err = e
    raise RuntimeError(f"Ollama ({model}) non risponde: {err}")


def label(t):  # chiave univoca negli schemi JSON: id + nome ripulito
    return f"T{t['id']} " + re.sub(r"[^\w .'-]", "", t["name"])


def unlabel(v, ts):  # l'IA a volte scrive T52 invece del nome: nel testo lo sostituiamo, le etichette dello schema restano
    if isinstance(v, dict):
        return {k: unlabel(x, ts) for k, x in v.items()}
    if isinstance(v, list):
        return [unlabel(x, ts) for x in v]
    if not isinstance(v, str) or v in {label(t) for t in ts}:
        return v
    for t in ts:
        v = re.sub(rf"\bT{t['id']}\s+{re.escape(t['name'])}\b", t["name"], v)
        v = re.sub(rf"\bT{t['id']}\b", t["name"], v)
    return v


def sheet(t):
    return (f"{t['name']} — Distretto {t['district'] or '?'}, salute {t['health']}/100, uccisioni {t['kills_count']}, "
            f"addestramento {t['training_score'] or '?'}/12; INT {t['intelligence']} FOR {t['strength']} "
            f"DES {t['dexterity']} CAR {t['charisma']} FUR {t['stealth']}; "
            f"indole {'aggressiva' if hunter(t) else 'furtiva'}; "  # la stessa regola che ne guida i movimenti
            f"personalità: {', '.join(json.loads(t['personality_traits']))}; aspetto: {t['physical_description']}")


def held(ids):  # gli oggetti portati dai tributi: {id: [nome (descrizione), ...]}
    with db() as con:
        out = {}
        for r in con.execute(f"SELECT holder, name, description FROM items WHERE edition = {CUR} AND holder IS NOT NULL"):
            if r["holder"] in ids:
                out.setdefault(r["holder"], []).append(f"{r['name']} ({r['description']})")
    return out


def inv_text(inv, t):
    return ", ".join(inv.get(t["id"], [])) or "nessuno"


def allied_pairs(group):  # le coppie già alleate fra questi tributi
    ids = {t["id"] for t in group}
    with db() as con:
        return {(r[0], r[1]) for r in con.execute("SELECT a, b FROM alliances") if r[0] in ids and r[1] in ids}


def event_line():  # l'evento scelto per la giornata: entra nei prompt fino a fine giornata
    with db() as con:
        ev = game(con).get("day_event")
    return f"Evento della giornata: «{ev}». " if ev else ""


# ---------- edizioni e fasi di gioco ----------
def archetype(arch):  # (tratti JSON, *stats) di un archetipo, con ±12 casuale sulle stats
    base, traits = ARCHETYPES[arch]
    return (json.dumps([arch, *traits], ensure_ascii=False), *(clamp(b + random.randint(-12, 12), 1, 100) for b in base))


def new_game(names, memorial, A=None, pods=()):
    """Nuova edizione: i nomi scritti più tributi casuali dal pool fino a 24, sulla mappa data o su una nuova.
    Restituisce un messaggio d'errore o None."""
    with db() as con:
        champions = {w["name"].lower(): w for w in winners(con)}  # a parità di nome vale l'ultima vittoria
        if memorial and len(champions) < 24:
            return f"L'edizione della memoria richiede 24 vincitori: finora sono {len(champions)}."
        # chi è morto o ha vinto in un'edizione passata non torna (salvo i vincitori nell'edizione della memoria)
        banned = set(champions) | {r[0].lower() for r in con.execute("SELECT name FROM tributes WHERE status = 'DEAD'")}
        if memorial:
            wrong = [n for n in names if n.lower() not in champions]
        else:
            wrong = [n for n in names if n.lower() in banned]
        if wrong:
            return ("Nell'edizione della memoria giocano solo i vincitori: " if memorial else
                    "Hanno già giocato (morti o vincitori) e non possono tornare nell'arena: ") + ", ".join(wrong)
        typed = {n.lower() for n in names}
        if memorial:
            pool = [w["name"] for w in champions.values()]
        else:
            pool = [p[0] for p in POOL if p[0].lower() not in banned]
        pool = [n for n in pool if n.lower() not in typed]
        if len(names) + len(pool) < 24:
            return f"Nel pool restano solo {len(pool)} tributi disponibili: scrivi tu gli altri nomi."
        names = names + random.sample(pool, 24 - len(names))
        presets = {p[0].lower(): p for p in POOL}
        A = A or new_arena()
        C = tuple(A["c"])
        ring = sorted([(x, y) for x in range(C[0] - 4, C[0] + 5) for y in range(C[1] - 4, C[1] + 5)
                       if cheb((x, y), C) == 4], key=lambda c: math.atan2(c[1] - C[1], c[0] - C[0]))  # 32 piattaforme
        spots = random.sample([ring[i * len(ring) // 24] for i in range(24)], 24)
        ed = con.execute("INSERT INTO game_state (memorial, arena) VALUES (?, ?)", (int(memorial), json.dumps(A))).lastrowid
        for p in pods:
            con.execute("INSERT INTO pods (edition, x, y, title, description, kind, entity, hp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ed, p["x"], p["y"], p["title"], p["description"], p["kind"], int(p["kind"] == "entity"), p["hp"]))
        created = []
        for name, (x, y) in zip(names, spots):
            w, p = champions.get(name.lower()), presets.get(name.lower())
            if memorial:  # i campioni tornano con la scheda della loro vittoria
                row = (w["name"], w["physical_description"], w["personality_traits"], *(w[k] for k in STATS))
            elif p:
                row = (p[0], p[2], *archetype(p[1]))
            else:  # fuori dal pool: lo profila il Direttore in Fase 1
                row = (name, None, None, None, None, None, None, None)
            con.execute("INSERT INTO tributes (edition, name, physical_description, personality_traits, intelligence, "
                        "strength, dexterity, charisma, stealth, current_x, current_y) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ed, *row, x, y))
            created.append(row[0])
        log(con, "info", f"🎟️ Edizione {ed}{' della memoria' if memorial else ''}: i 24 tributi",
            ", ".join(created) + f"\n🗺️ Arena {A['w']}×{A['h']}: "
            + ", ".join(sorted({biome_info(A, b)["name"] for row in A["rows"] for b in row if b not in "CRX"})))
        if pods:
            log(con, "info", "🥚 Baccelli nell'arena", f"{len(pods)} baccelli nascosti: scopriranno da soli chi li tocca.")


def profile(t):
    """Tributo fuori dal pool: se il Direttore lo conosce ne scrive la scheda, altrimenti attributi casuali."""
    RUN["status"] = f"Fase 1: il Direttore studia {t['name']}…"
    stat = integer(1, 100)
    res = ask(f"FASE 1 — PRE-GIOCHI. Un tributo si chiama «{t['name']}». È un personaggio pubblico reale, storico o "
              "contemporaneo, che conosci con certezza? Se sì, descrivi il suo aspetto (1 frase), 3 aggettivi della sua "
              "personalità (una parola ciascuno) e statistiche 1-100 fedeli alla persona reale. "
              "Se non lo conosci, conosciuto = false.",
              obj(conosciuto={"type": "boolean"}, aspetto=STR,
                  tratti={"type": "array", "items": STR, "minItems": 3, "maxItems": 3},
                  intelligenza=stat, forza=stat, destrezza=stat, carisma=stat, furtivita=stat))
    if res.get("conosciuto"):
        t["physical_description"] = res.get("aspetto", "")
        t["personality_traits"] = json.dumps([s.strip() for s in res.get("tratti", [])], ensure_ascii=False)
        stats = [res.get(k, 50) for k in ("intelligenza", "forza", "destrezza", "carisma", "furtivita")]
        note = "Il Direttore lo riconosce e ne scrive la scheda."
    else:
        arch = random.choice(list(ARCHETYPES))
        t["physical_description"] = "Volto sconosciuto alle telecamere di Capitol City."
        t["personality_traits"], *stats = archetype(arch)
        note = f"Sconosciuto al Direttore: attributi casuali (archetipo {arch})."
    t.update(zip(STATS, (clamp(int(v), 1, 100) for v in stats)))
    with db() as con:
        con.execute("UPDATE tributes SET physical_description = ?, personality_traits = ?, intelligence = ?, "
                    "strength = ?, dexterity = ?, charisma = ?, stealth = ? WHERE id = ?",
                    (t["physical_description"], t["personality_traits"], *(t[k] for k in STATS), t["id"]))
        log(con, "info", f"🔎 {t['name']}", note)


def assign_districts(ts):
    """Il Direttore sceglie i distretti; il codice garantisce 2 tributi per distretto (prima scelta, poi alternativa)."""
    RUN["status"] = "Fase 1: il Direttore assegna i distretti…"
    d = integer(1, 12)
    res = ask("FASE 1 — PRE-GIOCHI. Assegna a ogni tributo il Distretto di Panem più adatto alla sua storia e al suo "
              "carattere. Distretti: " + "; ".join(f"{k} {v}" for k, v in DISTRICTS.items()) + ". Ogni distretto "
              "accoglie 2 tributi: per ciascuno indica il motivo (poche parole), una prima scelta e un'alternativa.\n"
              + "\n".join(map(sheet, ts)),
              obj(tributi=obj(**{label(t): obj(motivo=STR, distretto=d, alternativa=d) for t in ts})))
    out, free = res.get("tributi", {}), dict.fromkeys(DISTRICTS, 2)
    for key in ("distretto", "alternativa", None):  # None: chi resta va nei posti liberi
        for t in random.sample(ts, len(ts)):  # chi viene letto prima non è favorito
            if t["district"] is not None:
                continue
            want = out.get(label(t), {}).get(key) if key else next(k for k, n in free.items() if n)
            if free.get(want):
                t["district"] = want
                free[want] -= 1
    with db() as con:
        for t in ts:
            con.execute("UPDATE tributes SET district = ? WHERE id = ?", (t["district"], t["id"]))
        lines = []
        for k, v in DISTRICTS.items():
            picks = [(t, out.get(label(t), {})) for t in ts if t["district"] == k]
            lines.append(f"Distretto {k} ({v}): " + "; ".join(
                f"{t['name']} — " + (r.get("motivo", "") if k in (r.get("distretto"), r.get("alternativa"))
                                     else "posto assegnato d'ufficio") for t, r in picks))
        log(con, "report", "🎟️ La Mietitura: i distretti scelti dal Direttore", "\n".join(lines))


def interview(t, ts, said):
    """said: le risposte già date in intervista, così chi viene dopo può commentarle."""
    question, mood = random.choice(QUESTIONS), random.choice(MOODS)
    RUN["status"] = f"Fase 1: Caesar Flickerman intervista {t['name']}…"
    others = "\n".join(f"- {o['name']} (Distretto {o['district']}): {', '.join(json.loads(o['personality_traits']))}"
                       for o in ts if o is not t)
    res = ask(f"FASE 1 — PRE-GIOCHI. Intervista di Caesar Flickerman prima dei Giochi.\n{sheet(t)}\n"
              f"Gli altri tributi dell'edizione (esistono solo questi, con questi nomi, senza inventarne altri):\n{others}\n"
              f"Risposte già date dagli altri (puoi commentarle o rispondere a qualcuno, se ha senso):\n"
              + ("\n".join(said[-8:]) or "nessuna ancora") + "\n"
              f"Caesar chiede: «{question}»\nScrivi la risposta di {t['name']} in prima persona (2-3 frasi), con il suo "
              "stile e riferimenti concreti alla sua vita reale, alle sue opere o alle sue frasi celebri; vietate le "
              "formule generiche come 'sono pronto a tutto per vincere'. Mai codici come T52: solo nomi. "
              "Poi la reazione del pubblico di Capitol City, "
              f"che risponde con {mood}: 1 frase concreta (un coro, un gesto, una battuta), mai un generico applauso. "
              "Infine la quota scommesse come intero N di 'N a 1', coerente con le statistiche "
              "(favoriti 2-6, medi 7-25, sfavoriti 26-99).",
              obj(risposta=STR, reazione_pubblico=STR, quota=integer(1, 99)))
    t["odds"] = f"{clamp(int(res.get('quota', 20)), 1, 99)}:1"
    said.append(f"{t['name']}: {res.get('risposta', '')}")
    with db() as con:
        con.execute("UPDATE tributes SET odds = ? WHERE id = ?", (t["odds"], t["id"]))
        log(con, "interview", f"🎤 D{t['district']} · {t['name']} — quota {t['odds']}",
            f"Caesar: «{question}»\n{t['name']}: {res.get('risposta', '')}\n👥 {res.get('reazione_pubblico', '')}")


def phase1():
    with db() as con:
        ts = tributes(con)
    for t in ts:
        if t["intelligence"] is None:
            profile(t)
    assign_districts(ts)
    said = []
    for t in sorted(ts, key=lambda t: t["district"]):
        interview(t, ts, said)
    with db() as con:
        set_state(con, current_phase="PRE-GIOCHI")
        log(con, "report", "📊 Fase 1 · Le quote dei bookmaker", "Dal favorito all'outsider.",
            {"cols": ["Quota", "Tributo", "D"],
             "rows": [[t["odds"], t["name"], t["district"]] for t in sorted(ts, key=lambda t: int(t["odds"][:-2]))]})


def phase2():
    RUN["status"] = "Fase 2: sessioni private con gli Strateghi…"
    with db() as con:
        ts = tributes(con)
    res = ask("FASE 2 — ADDESTRAMENTO. Assegna a ogni tributo il punteggio della sessione privata con gli Strateghi "
              "(1-12) coerente con le statistiche (forza, destrezza, intelligenza e furtività contano più del carisma) "
              "e un commento di 1 frase su cosa ha fatto davanti a loro, specifico per il personaggio e mai ripetuto.\n"
              + "\n".join(map(sheet, ts)),
              obj(tributi=obj(**{label(t): obj(commento=STR, punteggio=integer(1, 12)) for t in ts})))
    out, rows, notes = res.get("tributi", {}), [], []
    with db() as con:
        for t in ts:
            r = out.get(label(t), {})
            score = clamp(int(r.get("punteggio", 6)), 1, 12)
            con.execute("UPDATE tributes SET training_score = ? WHERE id = ?", (score, t["id"]))
            rows.append([t["district"], t["name"], score, t["intelligence"], t["strength"], t["dexterity"],
                         t["charisma"], t["stealth"], t["health"]])
            notes.append(f"{t['name']} ({score}/12): {r.get('commento', '')}")
        set_state(con, current_phase="ADDESTRAMENTO")
        log(con, "report", "🏹 Fase 2 · Addestramento: punteggi e statistiche finali", "\n".join(notes),
            {"cols": ["D", "Tributo", "Voto", "INT", "FOR", "DES", "CAR", "FUR", "HP"], "rows": rows})


ICON = {"Alleanza": "🤝", "Combattimento": "⚔️", "Fuga": "💨", "Morte": "☠️"}


def diplomacy_chance(group):  # il carisma apre la strada alle alleanze: sopra 60 il più carismatico può convincere gli altri
    return max(0, max(t["charisma"] for t in group) - 60) / 100


def encounter(group, alive, day, hour, x, y, showdown, bloodbath, A):
    """≥2 tributi nella stessa casella: il Direttore decide esito, danni, cure, uccisioni, spostamenti e oggetti usati."""
    RUN["status"] = f"Giorno {day} · {hour:02d}:00 · il Direttore giudica l'incontro in ({x},{y})…"
    n_alive = sum(t["status"] == "ALIVE" for t in alive)
    labels = {label(t): t for t in group}
    diplomacy = not showdown and not bloodbath and random.random() < diplomacy_chance(group)  # il carisma convince
    min_dead = len(group) // 2 if bloodbath else int(showdown)  # bagno di sangue: cade almeno metà del gruppo
    esiti = ("Alleanza",) if diplomacy else ("Combattimento", "Morte") if min_dead else tuple(ICON)
    rule = (f"BAGNO DI SANGUE ALLA CORNUCOPIA: corsa alle armi, scontri feroci; devono morire almeno {min_dead} tributi."
            if bloodbath else "SCONTRO FINALE: niente alleanze né fughe, almeno un tributo deve morire." if showdown
            else "DIPLOMAZIA: il più carismatico convince gli altri ad allearsi, senza combattimenti né morti." if diplomacy
            else "Combattimento o Morte se almeno uno ha indole aggressiva ed è in forze; Fuga se uno è più debole, "
                 "ferito o furtivo; Alleanza solo tra indoli furtive o per reciproca convenienza, più facile con un carismatico.")
    per = obj(danni=integer(0, 100), morto={"type": "boolean"}, ucciso_da=enum(*labels, "nessuno"),
              direzione=enum(*DIRS), oggetto=STR, cura=integer(0, 100))
    inv = held({t["id"] for t in group})
    res = ask(f"FASE 3 — GIORNO {day}, ore {hour:02d}:00 ({'giorno' if 6 <= hour < 20 else 'notte'}). {event_line()}"
              f"Casella ({x},{y}), bioma: {biome_note(A, x, y)}. Tributi ancora in vita nell'arena: {n_alive}.\n"
              "Nella casella si incontrano:\n" + "\n".join(f"{sheet(t)}; oggetti: {inv_text(inv, t)}" for t in group)
              + f"\n{rule}\n"
              "Prima analizza in 1-2 frasi rapporti di forza, indole, salute e ambiente; poi decidi l'esito "
              f"({', '.join(esiti)}) e narralo in 3-5 frasi, senza mai citare punti danno, percentuali o numeri di salute. "
              "Per ogni tributo indica i danni subiti (0-100), se muore, chi lo uccide ('nessuno' se nessuno), "
              "la direzione in cui si sposta dopo l'evento, l'oggetto che usa ('nessuno' se non ne usa) e le cure che riceve "
              "(0-100, solo se un alleato o un oggetto lo cura nella scena).",
              obj(analisi=STR, esito=enum(*esiti), narrazione=STR, tributi=obj(**{k: per for k in labels})))
    out, before, dead = res.get("tributi", {}), {t["id"]: t["health"] for t in group}, []
    links = allied_pairs(group)
    for k, t in labels.items():
        r = {} if diplomacy else out.get(k, {})
        ally = diplomacy or any((t["id"], o["id"]) in links for o in group if o is not t)
        heal = int(r.get("cura", 0)) if ally or r.get("oggetto", "nessuno") != "nessuno" else 0
        t["health"] = clamp(t["health"] - int(r.get("danni", 0)) + heal, 0, 100)
        if r.get("morto") or t["health"] == 0:
            dead.append(t)
        else:
            dx, dy = DIRS.get(r.get("direzione"), (0, 0))
            if walkable(A, (x + 2 * dx, y + 2 * dy)):  # oltre il bordo dell'arena non si va: resta dov'è
                t["current_x"], t["current_y"] = x + 2 * dx, y + 2 * dy
    while len(dead) < max(min_dead, res.get("esito") == "Morte"):  # l'esito impone dei caduti: cedono i più feriti
        dead.append(min((t for t in group if t not in dead), key=lambda t: t["health"]))
    if len(dead) == n_alive:  # niente strage totale: deve restare un vincitore
        spared = max(dead, key=lambda t: before[t["id"]])
        dead.remove(spared)
        spared["health"] = max(1, spared["health"])
    for t in dead:
        killer = labels.get(out.get(label(t), {}).get("ucciso_da"))
        if killer and killer is not t:
            killer["kills_count"] += 1
        t["health"], t["status"] = 0, "DEAD"
    ev = {"day": day, "hour": hour, "x": x, "y": y, "biome": biome(A, x, y), "esito": res.get("esito", ""),
          "names": [t["name"] for t in group], "dead": [t["name"] for t in dead], "text": res.get("narrazione", "")}
    fight = ev["esito"] in ("Combattimento", "Morte")
    with db() as con:
        for t in group:
            save(con, t)
        append(con, "event_history", ev)
        if ev["esito"] == "Alleanza":  # chi si allea resta legato: le trappole degli alleati non scattano
            vivi = [t for t in group if t["status"] == "ALIVE"]
            for a in vivi:
                for b in vivi:
                    if a is not b:
                        con.execute("INSERT OR IGNORE INTO alliances (a, b) VALUES (?, ?)", (a["id"], b["id"]))
        log(con, "event", f"G{day} · {hour:02d}:00 · {ev['biome']} ({x},{y}) · {ICON.get(ev['esito'], '')} {ev['esito']}",
            f"👥 {', '.join(ev['names'])}\n{ev['text']}" + (f"\n☠️ Caduti: {', '.join(ev['dead'])}" if dead else ""),
            marks={f"{x},{y}": "☠️" if dead else ICON.get(ev["esito"], "•")},
            vs=" vs ".join(ev["names"]) if fight else None)
    return ev


def scene(t, A, day, hour, x, y):
    """Scena da soli: il Direttore racconta la strategia di un tributo che in quest'ora non incontra nessuno."""
    RUN["status"] = f"Giorno {day} · {hour:02d}:00 · il Direttore racconta {t['name']}…"
    inv = held({t["id"]})
    res = ask(f"FASE 3 — GIORNO {day}, ore {hour:02d}:00 ({'giorno' if 6 <= hour < 20 else 'notte'}). {event_line()}"
              f"{t['name']} è da solo nella casella ({x},{y}), bioma: {biome_note(A, x, y)}, senza nessun altro intorno.\n"
              f"{sheet(t)}; oggetti: {inv_text(inv, t)}\n"
              f"Racconta in 2-3 frasi la strategia che {t['name']} segue in questo momento "
              "(procurarsi cibo o acqua, nascondersi, osservare, pianificare, usare un oggetto, ricordare qualcosa), "
              "coerente con la sua personalità. Nessun combattimento. Non citare mai punti danno, percentuali o numeri di salute. "
              "Se usa un oggetto o trova una cura, indica in cura quanti punti vita recupera (0-100), altrimenti 0.",
              obj(narrazione=STR, cura=integer(0, 100)))
    heal = int(res.get("cura", 0))
    with db() as con:
        if heal:
            t["health"] = clamp(t["health"] + heal, 0, 100)
            save(con, t)
        log(con, "scene", f"🧭 G{day} · {hour:02d}:00 · {t['name']} ({biome(A, x, y)})",
            res.get("narrazione", "") + (f"\n🩹 Recupera {heal} punti vita." if heal else ""))


def place_trap(t, A, day, hour, x, y):
    """Perk dell'intelligenza: il Direttore inventa la trappola in base a chi la prepara e all'ambiente in cui si trova."""
    RUN["status"] = f"Giorno {day} · {hour:02d}:00 · {t['name']} prepara una trappola…"
    res = ask(f"FASE 3 — GIORNO {day}, ore {hour:02d}:00. {event_line()}{t['name']} è molto intelligente e prepara una "
              f"trappola nella casella ({x},{y}), bioma: {biome_note(A, x, y)}.\n{sheet(t)}\n"
              "Inventa la trappola usando ciò che offre l'ambiente e le competenze o la personalità del tributo (una "
              "scienziata potrebbe usare materiali radioattivi). Dalle un nome breve e descrivila in 2 frasi.",
              obj(nome=STR, descrizione=STR))
    with db() as con:
        con.execute("INSERT INTO traps (edition, placer, x, y, name, description) VALUES (?, ?, ?, ?, ?, ?)",
                    (game(con)["id"], t["id"], x, y, res.get("nome", ""), res.get("descrizione", "")))
        log(con, "trap", f"🪤 G{day} · {hour:02d}:00 · {biome(A, x, y)} ({x},{y})",
            f"{t['name']} prepara una trappola: {res.get('nome', '')}. {res.get('descrizione', '')}",
            marks={f"{x},{y}": "🪤"})


def spring(tr, v, who, A, day, hour, alive, pod=False):
    """La trappola scatta: il Direttore scrive la scena e decide i danni e le cure. Un baccello può far nascere una creatura, una volta sola.
    Restituisce True se la creatura è nata."""
    crea = pod and not tr["spawned"]
    schema = obj(narrazione=STR, danni=integer(0, 100), morto={"type": "boolean"}, cura=integer(0, 100))
    if crea:
        schema = obj(narrazione=STR, danni=integer(0, 100), morto={"type": "boolean"}, cura=integer(0, 100),
                     crea_creatura={"type": "boolean"}, creatura=obj(nome=STR, punti_vita=integer(1, 100)))
    res = ask(f"FASE 3 — GIORNO {day}, ore {hour:02d}:00 ({'giorno' if 6 <= hour < 20 else 'notte'}). {event_line()}"
              f"{v['name']} entra nella casella ({tr['x']},{tr['y']}) dove c'è {who}: {tr['name']}. {tr['description']}\n"
              f"Bioma: {biome_note(A, tr['x'], tr['y'])}.\n{sheet(v)}\n"
              f"Racconta in 3-5 frasi cosa succede a {v['name']}, coerente con la trappola e con l'ambiente. Decidi i danni "
              "(0-100), la cura (0-100, se la trappola cura) e se muore. "
              + ("Se il baccello fa nascere una creatura (per esempio un cane mutante), indicalo in crea_creatura e "
                 "dalle nome e punti vita. " if crea else "")
              + "Nella narrazione non citare punti danno, percentuali o numeri di salute.",
              schema)
    v["health"] = clamp(v["health"] - int(res.get("danni", 0)) + int(res.get("cura", 0)), 0, 100)
    dies = bool(res.get("morto") or v["health"] == 0) and sum(t["status"] == "ALIVE" for t in alive) > 2  # niente finale anticipato
    if not dies:
        v["health"] = max(1, v["health"])
    killer = None if pod else next((t for t in alive if t["id"] == tr["placer"] and t["status"] == "ALIVE"), None)
    if dies:
        v["health"], v["status"] = 0, "DEAD"
        if killer:
            killer["kills_count"] += 1
    spawned = bool(crea and res.get("crea_creatura"))
    c = res.get("creatura") if isinstance(res.get("creatura"), dict) else {}
    with db() as con:
        save(con, v)
        if dies and killer:
            save(con, killer)
        if spawned:
            con.execute("INSERT INTO entities (edition, name, description, hp, x, y) VALUES (?, ?, ?, ?, ?, ?)",
                        (game(con)["id"], c.get("nome") or "creatura", f"nata da {tr['name']}: {tr['description']}",
                         clamp(int(c.get("punti_vita") or 30), 1, 100), tr["x"], tr["y"]))
        log(con, "trap", f"🪤 G{day} · {hour:02d}:00 · {biome(A, tr['x'], tr['y'])} ({tr['x']},{tr['y']})",
            f"{v['name']} cade nella trappola di {who}: {tr['name']}.\n{res.get('narrazione', '')}"
            + (f"\n👾 Compare: {c.get('nome') or 'una creatura'}" if spawned else "")
            + (f"\n☠️ Caduto: {v['name']}" if dies else ""),
            marks={f"{tr['x']},{tr['y']}": "☠️" if dies else "🪤"})
    return spawned


def spring_traps(alive, A, day, hour):  # chi entra in una trappola di un tributo (non il suo autore né i suoi alleati) ci cade: poi sparisce
    with db() as con:
        traps = [dict(r) for r in con.execute(f"SELECT * FROM traps WHERE edition = {CUR}")]
        allies = {(r[0], r[1]) for r in con.execute("SELECT a, b FROM alliances")}
        names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM tributes")}
    for tr in traps:
        for v in alive:
            if (v["status"] != "ALIVE" or pos(v) != (tr["x"], tr["y"])
                    or v["id"] == tr["placer"] or (tr["placer"], v["id"]) in allies):
                continue
            try:
                spring(tr, v, names[tr["placer"]], A, day, hour, alive)
            except RuntimeError:  # se il Direttore non risponde la trappola resta armata per il passo successivo
                break
            with db() as con:
                con.execute("DELETE FROM traps WHERE id = ?", (tr["id"],))  # monouso: una volta scattata, sparisce
            break


def trigger_pods(alive, A, day, hour):  # un baccello scatta quando qualcuno ci mette piede, poi sparisce (o si attiva)
    with db() as con:
        pods = [dict(r) for r in con.execute(f"SELECT * FROM pods WHERE edition = {CUR} AND state = 'SET'")]
    for pod in pods:
        here = [t for t in alive if t["status"] == "ALIVE" and pos(t) == (pod["x"], pod["y"])]
        if not here:
            continue
        if pod["kind"] == "entity":  # l'entità si attiva: da qui si muove e combatte come gli altri
            with db() as con:
                con.execute("INSERT INTO entities (edition, name, description, hp, x, y) VALUES (?, ?, ?, ?, ?, ?)",
                            (game(con)["id"], pod["title"], pod["description"], pod["hp"], pod["x"], pod["y"]))
                con.execute("UPDATE pods SET state = 'USED' WHERE id = ?", (pod["id"],))
                log(con, "creature", f"👾 G{day} · {hour:02d}:00 · {biome(A, pod['x'], pod['y'])} ({pod['x']},{pod['y']})",
                    f"{pod['title']} si attiva: {pod['description']}", marks={f"{pod['x']},{pod['y']}": "👾"})
        elif pod["kind"] == "object":  # l'oggetto esce dal baccello e resta a terra, da raccogliere
            with db() as con:
                con.execute("INSERT INTO items (edition, name, description, x, y) VALUES (?, ?, ?, ?, ?)",
                            (game(con)["id"], pod["title"], pod["description"], pod["x"], pod["y"]))
                con.execute("UPDATE pods SET state = 'USED' WHERE id = ?", (pod["id"],))
                log(con, "item", f"🎁 G{day} · {hour:02d}:00 · {biome(A, pod['x'], pod['y'])} ({pod['x']},{pod['y']})",
                    f"Dal baccello esce {pod['title']}: {pod['description']}.", marks={f"{pod['x']},{pod['y']}": "🎒"})
        else:  # trappola: scatta con chi ci cade per primo, poi sparisce
            tr = {**pod, "name": pod["title"]}  # il baccello si presenta alla scena con il suo titolo
            for v in here:
                if v["status"] != "ALIVE":
                    continue
                try:
                    spawned = spring(tr, v, "un baccello", A, day, hour, alive, pod=True)
                except RuntimeError:  # se il Direttore non risponde il baccello resta pronto per il passo successivo
                    break
                pod["spawned"] = int(bool(pod["spawned"]) or spawned)
                with db() as con:
                    con.execute("UPDATE pods SET state = 'USED', spawned = ? WHERE id = ?", (pod["spawned"], pod["id"]))
                break


def creature_fight(e, group, A, day, hour):  # la creatura nella casella attacca e viene attaccata: il Direttore decide i danni
    x, y = e["current_x"], e["current_y"]
    RUN["status"] = f"Giorno {day} · {hour:02d}:00 · lo scontro con {e['name']} in ({x},{y})…"
    labels = {label(t): t for t in group}
    inv = held({t["id"] for t in group})
    before = {t["id"]: t["health"] for t in group}
    res = ask(f"FASE 3 — GIORNO {day}, ore {hour:02d}:00 ({'giorno' if 6 <= hour < 20 else 'notte'}). {event_line()}"
              f"Casella ({x},{y}), bioma: {biome_note(A, x, y)}. C'è una creatura: {e['name']} — {e['description']} "
              f"(punti vita {e['hp']}).\nI tributi che la incontrano:\n"
              + "\n".join(f"{sheet(t)}; oggetti: {inv_text(inv, t)}" for t in group) + "\n"
              "Racconta lo scontro in 3-5 frasi: come attaccano, fuggono o si difendono, come reagisce la creatura, "
              "usando gli oggetti che hanno. Decidi quanti danni subisce la creatura (0-100) e, per ogni tributo, i "
              "danni subiti (0-100) e se muore. Nella narrazione non citare punti danno, percentuali o numeri di salute.",
              obj(narrazione=STR, danni_creatura=integer(0, 100),
                  tributi=obj(**{k: obj(danni=integer(0, 100), morto={"type": "boolean"}) for k in labels})))
    out = res.get("tributi", {})
    for k, t in labels.items():
        r = out.get(k, {})
        t["health"] = clamp(t["health"] - int(r.get("danni", 0)), 0, 100)
        if r.get("morto") or t["health"] == 0:
            t["health"], t["status"] = 0, "DEAD"
    e["hp"] = clamp(e["hp"] - int(res.get("danni_creatura", 0)), 0, 100)
    if e["hp"] == 0:
        e["status"] = "DEAD"
    dead = [t for t in group if t["status"] == "DEAD"]
    with db() as con:
        n_alive = con.execute(f"SELECT COUNT(*) FROM tributes WHERE edition = {CUR} AND status = 'ALIVE'").fetchone()[0]
    if dead and len(dead) == n_alive:  # niente strage totale: deve restare un vincitore
        spared = max(dead, key=lambda t: before[t["id"]])
        spared["status"], spared["health"] = "ALIVE", 1
        dead.remove(spared)
    with db() as con:
        for t in group:
            save(con, t)
        save_ent(con, e)
        log(con, "creature", f"👾 G{day} · {hour:02d}:00 · {biome(A, x, y)} ({x},{y}) · {e['name']}",
            res.get("narrazione", "") + (f"\n☠️ Caduti: {', '.join(t['name'] for t in dead)}" if dead else "")
            + (f"\n💥 {e['name']} è abbattuta." if e["status"] == "DEAD" else ""),
            marks={f"{x},{y}": "☠️" if dead or e["status"] == "DEAD" else "👾"})


def pick_items(alive, A, day, hour):  # chi è su un oggetto a terra lo raccoglie, se ha uno slot libero; con gli slot pieni può lasciarne uno
    with db() as con:
        for t in alive:
            if t["status"] != "ALIVE" or random.random() > PICKUP_CHANCE:
                continue
            x, y = pos(t)
            here = [dict(r) for r in con.execute(
                f"SELECT * FROM items WHERE edition = {CUR} AND holder IS NULL AND x = ? AND y = ?", (x, y))]
            if not here:
                continue
            carried = [dict(r) for r in con.execute("SELECT * FROM items WHERE holder = ?", (t["id"],))]
            lost = None
            if len(carried) >= 2:
                if random.random() > SWAP_CHANCE:
                    continue
                drop = random.choice(carried)
                con.execute("UPDATE items SET holder = NULL, x = ?, y = ? WHERE id = ?", (x, y, drop["id"]))
                lost = drop["name"]
            take = random.choice(here)
            con.execute("UPDATE items SET holder = ?, x = NULL, y = NULL WHERE id = ?", (t["id"], take["id"]))
            log(con, "item", f"🎒 G{day} · {hour:02d}:00 · {biome(A, x, y)} ({x},{y})",
                f"{t['name']} prende {take['name']}" + (f", lasciando {lost} a terra" if lost else "") + ".",
                marks={f"{x},{y}": "🎒"})


def stock_cornucopia(A):  # all'alba del primo giorno la Cornucopia è piena di oggetti da contendersi
    RUN["status"] = "Giorno 1 · il Direttore riempie la Cornucopia…"
    try:
        res = ask("FASE 3 — GIORNO 1, alba. Alla Cornucopia sono sparsi oggetti da contendersi nella corsa alle armi. "
                  "Inventa 8 oggetti (armi, cibo, strumenti, medicine, oggetti strani): per ciascuno un nome breve e una "
                  "descrizione di una frase.",
                  obj(oggetti={"type": "array", "items": obj(nome=STR, descrizione=STR), "minItems": 6, "maxItems": 8}))
        oggetti = [(o["nome"], o.get("descrizione", "")) for o in res.get("oggetti", [])
                   if isinstance(o, dict) and o.get("nome")]
    except RuntimeError:
        oggetti = []
    if len(oggetti) < 6:  # se il Direttore non risponde, la Cornucopia ha comunque i suoi oggetti
        oggetti = DEFAULT_ITEMS
    cx, cy = A["c"]
    ring = [(x, y) for x in range(cx - 2, cx + 3) for y in range(cy - 2, cy + 3) if walkable(A, (x, y))]
    with db() as con:
        ed = game(con)["id"]
        for nome, desc in oggetti:
            x, y = random.choice(ring)
            con.execute("INSERT INTO items (edition, name, description, x, y) VALUES (?, ?, ?, ?, ?)", (ed, nome, desc, x, y))
        log(con, "item", "🎒 La Cornucopia", "\n".join(f"{n}: {d}" for n, d in oggetti))


# ---------- il gestore dell'evento ----------
def run_event(day, text, A):  # l'IA traduce ciò che ha chiesto l'utente in effetti sulla partita
    RUN["status"] = f"Giorno {day} · il Direttore applica l'evento…"
    try:
        res = ask(f"FASE 3 — GIORNO {day}, alba. L'utente ha scelto questo evento per tutta la giornata: «{text}». "
                  "Sei il gestore dell'evento: traducilo in effetti sulla partita, usando solo questi tipi: "
                  "oggetti (crea oggetti a terra, nel bioma indicato se c'è), entita (una creatura con punti vita 'valore' "
                  "che resta nel bioma indicato), bioma (il bioma 'bioma' diventa 'nuovo': un bioma già esistente, oppure "
                  "un bioma nuovo con 'nuovo' e 'descrizione'; 'acqua' se è acqua), cura e danno (valore punti ai tributi "
                  "del bioma indicato o al tributo 'bersaglio'; 'tutti' se sono tutti), resuscita (riporta in vita il "
                  "tributo 'bersaglio' con 'valore' punti vita), dona_oggetto (dai al tributo 'bersaglio' un oggetto 'nome' "
                  "con 'descrizione'), nessuno. Ogni effetto colpisce solo il bioma indicato, se c'è. Scrivi anche "
                  "l'annuncio in 2-3 frasi, come un presentatore televisivo, senza dire come finirà.",
                  obj(annuncio=STR, effetti={"type": "array", "items": EFFECT, "minItems": 0, "maxItems": 6}))
        return res.get("annuncio") or text, res.get("effetti") or []
    except RuntimeError:  # senza il gestore, l'evento resta solo annunciato
        return text, []


def spawn_on(A, scope, n):  # n caselle a caso calpestabili, dentro il bioma indicato se c'è
    cells = [(x, y) for y in range(A["h"]) for x in range(A["w"])
             if walkable(A, (x, y)) and (scope is None or A["rows"][y][x] == scope)]
    return [random.choice(cells) for _ in range(n)] if cells else []


def spawn_items(A, ef, scope):  # oggetti a terra, nel bioma indicato se c'è
    n = max(1, min(int(ef.get("quantita") or 1), 6))
    spots = spawn_on(A, scope, n)
    if not spots:
        return None
    nome = (ef.get("nome") or "Oggetto").strip()[:60]
    with db() as con:
        ed = game(con)["id"]
        for x, y in spots:
            con.execute("INSERT INTO items (edition, name, description, x, y) VALUES (?, ?, ?, ?, ?)",
                        (ed, nome, (ef.get("descrizione") or "")[:300], x, y))
    return f"🎒 compaiono {n} «{nome}»" + (f" nel bioma {biome_info(A, scope)['name']}" if scope else "")


def spawn_creature(A, ef, scope):  # una creatura che resta nel bioma indicato
    spots = spawn_on(A, scope, 1)
    if not spots:
        return None
    (x, y), nome = spots[0], (ef.get("nome") or "Creatura").strip()[:60]
    with db() as con:
        con.execute("INSERT INTO entities (edition, name, description, hp, x, y, zone) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (game(con)["id"], nome, (ef.get("descrizione") or "")[:300],
                     clamp(int(ef.get("valore") or 30), 1, 100), x, y, scope))
    return f"👾 compare {nome}" + (f" nel bioma {biome_info(A, scope)['name']}" if scope else "")


def change_biome(A, ef):  # il bioma indicato diventa un altro, anche uno nuovo creato dall'evento
    src = biome_key(A, ef.get("bioma"))
    if src is None or src == "C":  # la Cornucopia resta com'è
        return A, None
    dst = biome_key(A, ef.get("nuovo"))
    if dst is None:
        dst = next(ch for ch in "abcdefghijklmnopqrstuvwxyz" if ch not in A.get("biomes", {}))
        A.setdefault("biomes", {})[dst] = {"name": (ef.get("nuovo") or "Bioma nuovo").strip()[:40],
                                           "hint": (ef.get("descrizione") or "terreno sconosciuto")[:200],
                                           "acqua": bool(ef.get("acqua"))}
    if dst == src:
        return A, None
    if ef.get("acqua") and dst not in A.get("water", WATER):
        A["water"] = A.get("water", WATER) + dst
    if ef.get("acqua") and dst in A.get("biomes", {}):
        A["biomes"][dst]["acqua"] = True
    A["rows"] = [row.replace(src, dst) for row in A["rows"]]
    return A, f"🌍 {biome_info(A, src)['name']} diventa {biome_info(A, dst)['name']}"


def shift_health(A, ef, scope, heal):  # cura o danneggia i tributi nel bioma indicato, o il bersaglio
    who = (ef.get("bersaglio") or "").strip().lower()
    amount = int(ef.get("valore") or 0)
    with db() as con:
        alive = tributes(con, alive=True)
        targets = [t for t in alive if scope is None or A["rows"][t["current_y"]][t["current_x"]] == scope]
        if who and who not in ("tutti", "tutti i tributi"):
            targets = [t for t in targets if t["name"].lower() == who]
        if not targets or not amount:
            return None
        if heal:
            for t in targets:
                t["health"] = min(100, t["health"] + amount)
                save(con, t)
        else:
            for t in targets:
                t["health"] = clamp(t["health"] - amount, 0, 100)
                if t["health"] == 0:
                    t["status"] = "DEAD"
            dead = [t for t in targets if t["status"] == "DEAD"]
            if dead and len(dead) == len(alive):  # niente strage totale: deve restare un vincitore
                spared = max(dead, key=lambda t: t["kills_count"] + t["health"])
                spared["status"], spared["health"] = "ALIVE", 1
            for t in targets:
                save(con, t)
    names = ", ".join(t["name"] for t in targets)
    return (f"❤️ cura {amount} punti a {names}" if heal else f"💥 {amount} danni a {names}")


def revive(ef):  # riporta in vita un tributo morto
    who = (ef.get("bersaglio") or "").strip().lower()
    if not who:
        return None
    with db() as con:
        dead = [dict(r) for r in con.execute(f"SELECT * FROM tributes WHERE edition = {CUR} AND status = 'DEAD'")]
        cand = [t for t in dead if t["name"].lower() == who]
        if not cand:
            return None
        t = cand[0]
        con.execute("UPDATE tributes SET status = 'ALIVE', health = ? WHERE id = ?",
                    (clamp(int(ef.get("valore") or 30), 1, 100), t["id"]))
    return f"✨ {t['name']} torna in vita"


def give_item(ef):  # dona un oggetto a un tributo vivo: con gli slot pieni lo lascia a terra accanto a lui
    who = (ef.get("bersaglio") or "").strip().lower()
    nome = (ef.get("nome") or "Oggetto").strip()[:60]
    with db() as con:
        t = next((t for t in tributes(con, alive=True) if t["name"].lower() == who), None)
        if t is None:
            return None
        ed = game(con)["id"]
        carried = con.execute("SELECT COUNT(*) FROM items WHERE holder = ?", (t["id"],)).fetchone()[0]
        if carried < 2:
            con.execute("INSERT INTO items (edition, name, description, holder) VALUES (?, ?, ?, ?)",
                        (ed, nome, (ef.get("descrizione") or "")[:300], t["id"]))
        else:
            con.execute("INSERT INTO items (edition, name, description, x, y) VALUES (?, ?, ?, ?, ?)",
                        (ed, nome, (ef.get("descrizione") or "")[:300], t["current_x"], t["current_y"]))
    return f"🎁 {t['name']} riceve {nome}"


def apply_effects(effects, A, day):  # ogni effetto cambia la partita: quelli con un bioma valgono solo lì
    notes = []
    for ef in effects[:6]:
        if not isinstance(ef, dict):
            continue
        tipo, scope = ef.get("tipo"), biome_key(A, ef.get("bioma"))
        if tipo == "bioma":
            A, note = change_biome(A, ef)
        elif tipo == "oggetti":
            note = spawn_items(A, ef, scope)
        elif tipo == "entita":
            note = spawn_creature(A, ef, scope)
        elif tipo in ("cura", "danno"):
            note = shift_health(A, ef, scope, tipo == "cura")
        elif tipo == "resuscita":
            note = revive(ef)
        elif tipo == "dona_oggetto":
            note = give_item(ef)
        else:
            note = None
        if note:
            notes.append(note)
    return A, notes


def close_arena(con, day):  # la mappa si stringe solo quando scocca il giorno: al massimo uno step, con 6 caduti o una giornata senza morti
    g, dead = game(con), dead_count(con)
    if day > 1 and (dead >= 6 * (g["closures"] + 1) or dead == g["day_dead"]):
        set_state(con, closures=g["closures"] + 1)
    set_state(con, day_dead=dead)


def answer_event(text):  # l'utente sceglie l'evento della giornata (anche nessuno): la partita riprende
    with db() as con:
        g = game(con)
        if g["current_phase"] not in ("ADDESTRAMENTO", "ATTESA") or g["event_day"] == g["day_number"] + 1:
            return False
        set_state(con, event_day=g["day_number"] + 1, day_event=text.strip() or None)
    return True


def play_day():
    """Una giornata = 24 ore di movimento; collisioni → Direttore; trappole, baccelli, scene e cronaca.
    All'alba l'IA aspetta che l'utente scelga l'evento della giornata (anche nessuno)."""
    with db() as con:
        g = game(con)
        day = g["day_number"] + 1
        if g["event_day"] != day:
            return
        A = json.loads(g["arena"])
        set_state(con, day_number=day, current_phase="GIOCHI")
        close_arena(con, day)
        start = tributes(con, alive=True)
    if g["day_event"]:
        text, effects = run_event(day, g["day_event"], A)
        A, notes = apply_effects(effects, A, day)
        with db() as con:
            set_state(con, arena=json.dumps(A))
            log(con, "event_day", f"📣 Giorno {day} · evento della giornata", "\n".join([text] + [f"🌀 {n}" for n in notes]))
    if day == 1:
        stock_cornucopia(A)
    placements = {}  # ora → chi piazza una trappola quell'ora: una volta al giorno, con probabilità TRAP_CHANCE
    for t in start:
        if t["intelligence"] >= INTEL_HIGH and random.random() < TRAP_CHANCE:
            placements.setdefault(random.randrange(24), []).append(t["id"])
    seen, met, events, scened = set(), {}, [], set()
    for tick in range(24):
        hour = (6 + tick) % 24
        RUN["status"], RUN["clock"] = f"Giorno {day} · {hour:02d}:00", (day - 1) * 24 + tick
        with db() as con:
            alive = tributes(con, alive=True)
            if len(alive) < 2:
                break
            r = zone(A, closing(con))  # la zona cambia solo all'alba, vedi close_arena
            showdown, rush = r == 0 or len(alive) == 2, day == 1 and tick < 5  # giorno 1: corsa alla Cornucopia
            ents = entities_alive(con)
            for t, (x, y) in zip(alive, [step(t, alive, r, showdown, A, rush) for t in alive]):  # mosse simultanee
                t["current_x"], t["current_y"] = x, y
                save(con, t)
            for t in alive:  # gli specchi d'acqua curano un po'
                if heals(A, pos(t)) and t["health"] < 100:
                    t["health"] = min(100, t["health"] + WATER_HEAL)
                    save(con, t)
            for e in ents:
                e["current_x"], e["current_y"] = ent_step(e, alive, A)
                save_ent(con, e)
        trigger_pods(alive, A, day, hour)
        for tid in placements.get(tick, []):  # l'intelligente piazza la trappola dove si trova, se è ancora vivo
            placer = next((t for t in alive if t["id"] == tid and t["status"] == "ALIVE"), None)
            if placer:
                try:
                    place_trap(placer, A, day, hour, *pos(placer))
                except RuntimeError:  # una trappola mancata non ferma la giornata
                    pass
        spring_traps(alive, A, day, hour)
        pick_items(alive, A, day, hour)
        alive = [t for t in alive if t["status"] == "ALIVE"]
        with db() as con:
            ents = entities_alive(con)  # dopo i baccelli: qualche entità può essere appena nata
        ent_at = {}
        for e in ents:
            ent_at.setdefault(pos(e), []).append(e)
        cells = {}
        for t in alive:
            cells.setdefault(pos(t), []).append(t)
        lone = [t for t in alive if len(cells[pos(t)]) == 1 and pos(t) not in ent_at and t["id"] not in scened]
        if len(alive) <= 12 and lone and random.random() < SCENE_CHANCE:
            t = random.choice(lone)
            scened.add(t["id"])
            try:
                scene(t, A, day, hour, *pos(t))
            except RuntimeError:  # una scena mancata non ferma la giornata
                pass
        for (x, y), group in cells.items():
            if (x, y) in ent_at:  # una creatura nella casella: lo scontro è con lei
                e = ent_at[(x, y)][0]
                if e["status"] == "ALIVE":
                    creature_fight(e, group, A, day, hour)
                continue
            bloodbath = day == 1 and biome(A, x, y) == "Cornucopia"
            group = [t for t in group if showdown or bloodbath or met.get(t["id"], 0) < MAX_MEETINGS]  # stanchi: nascosti
            key = frozenset(t["id"] for t in group)
            if len(group) > 1 and key not in seen:  # ponytail: stesso gruppo una volta al giorno, niente loop tra alleati
                seen.add(key)
                for t in group:
                    met[t["id"]] = met.get(t["id"], 0) + 1
                events.append(encounter(group, alive, day, hour, x, y, showdown, bloodbath, A))
        time.sleep(TICK_DELAY)
    with db() as con:
        set_state(con, day_event=None)  # l'evento finisce con la giornata
    chronicle(day, A, events, start)
    with db() as con:
        if game(con)["current_phase"] == "GIOCHI":  # giornata finita: l'IA aspetta l'evento della prossima
            set_state(con, current_phase="ATTESA")


def chronicle(day, A, events, start):
    RUN["status"] = f"Giorno {day} · il Direttore scrive la cronaca…"
    with db() as con:
        alive = tributes(con, alive=True)
        r = zone(A, closing(con))
    survivors = {t["id"] for t in alive}
    fallen = [t for t in start if t["id"] not in survivors]
    names = lambda ts: ", ".join(f"{t['name']} (D{t['district']})" for t in ts) or "nessuno"
    recap = (f"Tributi rimanenti: {len(alive)}\nDeceduti oggi ({len(fallen)}): {names(fallen)}\n"
             f"Superstiti: {names(alive)}")
    facts = "\n".join(f"- {e['hour']:02d}:00, {e['biome']}: {e['esito']} tra {', '.join(e['names'])}"
                      + (f" (morti: {', '.join(e['dead'])})" if e["dead"] else "") + f". {e['text']}" for e in events)
    prose = {"type": "string", "minLength": 200}  # un vero paragrafo: llama3 tendeva a ricopiare il riepilogo
    try:
        res = ask(f"FASE 3 — fine del GIORNO {day}. Racconta questa giornata degli Hunger Games come il cronista di "
                  f"Capitol City.\nZona sicura: {r} caselle attorno alla Cornucopia.\nFatti della giornata, in ordine:\n"
                  f"{facts or '- nessuno scontro: fame, sete e attesa'}\n"
                  f"Caduti di oggi: {names(fallen)}. Tributi ancora in vita: {len(alive)}.\n"
                  + (f"I Giochi sono finiti: celebra il vincitore {alive[0]['name']}.\n" if len(alive) == 1 else "")
                  + "Scrivi due paragrafi narrativi di 4-6 frasi: cronaca_giorno (dall'alba al tramonto: scontri, fughe "
                    "e alleanze) e cronaca_notte (fatti notturni, inno di Panem, volti dei caduti proiettati nel cielo, "
                    "pensieri dei sopravvissuti). Usa solo questi fatti, non inventare altre morti.",
                  obj(cronaca_giorno=prose, cronaca_notte=prose))
        story = f"☀️ GIORNO\n{res.get('cronaca_giorno', '')}\n\n🌙 NOTTE\n{res.get('cronaca_notte', '')}\n\n"
    except RuntimeError as e:  # il riepilogo non dipende dall'LLM: lo scriviamo comunque
        story = f"(Cronaca non disponibile: {e})\n\n"
    with db() as con:
        log(con, "day", f"📅 Giorno {day}", f"{story}━━ RIEPILOGO GIORNO {day} ━━\n{recap}")
        if len(alive) == 1:
            w = alive[0]
            set_state(con, current_phase="FINE", winner_id=w["id"])
            log(con, "win", "🏆 Vincitore dei Giochi",
                f"{w['name']} del Distretto {w['district']}: {w['kills_count']} uccisioni, salute {w['health']}/100.")


def next_step():
    with db() as con:
        phase = game(con)["current_phase"]
    if phase == "SELEZIONE":
        phase1()
    elif phase == "PRE-GIOCHI":
        phase2()
    elif phase in ("ADDESTRAMENTO", "GIOCHI", "ATTESA"):
        play_day()


def run_step():
    try:
        next_step()
    except Exception as e:  # ponytail: un errore a metà giornata la tronca; per riprenderla servirebbe l'ora nel DB
        with db() as con:
            log(con, "error", "⚠️ Errore del Direttore", f"{type(e).__name__}: {e}")
    finally:
        RUN["status"] = ""
        RUN.pop("clock", None)
        LOCK.release()


# ---------- web ----------
app = Flask(__name__)
app.json.sort_keys = False  # la scheda mostra le colonne nell'ordine SQL


@app.get("/")
def index():
    return send_file(ROOT / "index.html")


@app.get("/api/state")
def api_state():
    since = request.args.get("since", 0, type=int)
    with db() as con:
        g, ts = game(con), tributes(con)
        steps, traps = closing(con), traps_cells(con)
        pods, ents, ground, inv = pods_view(con), ents_view(con), ground_view(con), inv_view(con)
    logs, day, A = json.loads(g["logs"]), g["day_number"], json.loads(g.get("arena") or "null")
    model, problem = resolve_model()
    alive = sum(t["status"] == "ALIVE" for t in ts)
    # l'IA aspetta l'evento dell'utente all'alba di ogni giornata; awaiting_at dice a che punto del diario si trova
    awaiting = (g["day_number"] + 1 if g["current_phase"] in ("ADDESTRAMENTO", "ATTESA") and alive >= 2
                and g.get("event_day") != g["day_number"] + 1 else None)
    return {"edition": g["id"], "memorial": g["memorial"], "phase": g["current_phase"], "day": day,
            "radius": zone(A, steps) if A else 0, "model": model, "problem": problem, "busy": LOCK.locked(),
            "started": g.get("created_at"), "awaiting": awaiting, "awaiting_at": len(logs),
            "event": g.get("day_event"), "traps": traps, "pods": pods, "ents": ents, "ground": ground, "inv": inv,
            "status": RUN["status"], "tributes": ts, "arena": A, "since": since, "logs": logs[since:],
            "events": recent(json.loads(g["event_history"]), day, RUN.get("clock", day * 24 - 1))}


@app.get("/api/arena")
def api_arena():  # una mappa in anteprima: l'utente ci mette i baccelli, poi crea l'edizione con questa mappa
    return {"arena": new_arena()}


@app.post("/api/event")
def api_event():  # l'evento della giornata scelto dall'utente; vuoto = nessun evento
    data = request.get_json(silent=True)
    text = data.get("text", "") if isinstance(data, dict) else ""
    if not isinstance(text, str) or len(text) > 500:
        return {"error": "L'evento può essere lungo al massimo 500 caratteri."}, 400
    if not answer_event(text):
        return {"error": "Non c'è un evento da scegliere adesso."}, 409
    return {"ok": True}


@app.get("/api/editions")
def api_editions():
    with db() as con:
        eds = [dict(r) for r in con.execute("SELECT id, memorial, day_number, current_phase, winner_id, created_at "
                                            "FROM game_state ORDER BY id DESC")]
        ts = [dict(r) for r in con.execute("SELECT id, edition, name, district, status, kills_count FROM tributes "
                                           "ORDER BY district, id")]
        champions = len({w["name"].lower() for w in winners(con)})
    for e in eds:
        e["tributes"] = [t for t in ts if t["edition"] == e["id"]]
        e["winner"] = next((t for t in e["tributes"] if t["id"] == e["winner_id"]), None)
    return {"editions": eds, "champions": champions, "next": eds[0]["id"] + 1 if eds else 1}


@app.post("/api/new")
def api_new():
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    raw = data.get("names")
    names = [n.strip() for n in raw if isinstance(n, str) and n.strip()] if isinstance(raw, list) else []
    if len(names) > 24 or len({n.lower() for n in names}) != len(names) or any(len(n) > 60 for n in names):
        return {"error": "Al massimo 24 nomi, tutti diversi e lunghi al più 60 caratteri."}, 400
    try:
        A = checked_arena(data["arena"]) if data.get("arena") else None
        pods = checked_pods(data.get("pods"), A)
    except ValueError as e:
        return {"error": str(e)}, 400
    if not LOCK.acquire(blocking=False):
        return {"error": "Il Direttore è al lavoro: attendi la fine del passo in corso."}, 409
    try:
        error = new_game(names, data.get("memorial") is True, A, pods)
    finally:
        LOCK.release()
    return ({"error": error}, 400) if error else {"ok": True}


@app.post("/api/next")
def api_next():
    if not LOCK.acquire(blocking=False):
        return {"error": "Il Direttore è già al lavoro."}, 409
    threading.Thread(target=run_step, daemon=True).start()
    return {"ok": True}


if __name__ == "__main__":
    init_db()
    if FROZEN:  # l'exe apre da solo la pagina appena il server è su
        import webbrowser
        threading.Timer(1.5, webbrowser.open, ["http://localhost:5001"]).start()
    app.run(port=5001, threaded=True)
