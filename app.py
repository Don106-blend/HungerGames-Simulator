"""Hunger Games diretti da un LLM locale via Ollama: arena 32x32, persistenza SQLite, UI Flask a 3 colonne.

Avvio:  python -m venv .venv  &&  .venv\\Scripts\\pip install -r requirements.txt  &&  .venv\\Scripts\\python app.py
Poi apri http://localhost:5001   (env: OLLAMA_MODEL, default llama3:8b · OLLAMA_URL, default http://localhost:11434)
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
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, request, send_file

from pool import ARCHETYPES, POOL

FROZEN = getattr(sys, "frozen", False)  # avviato come eseguibile PyInstaller
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # dove sta index.html
HOME = Path(sys.executable).parent if FROZEN else ROOT
# l'exe dentro dist/ del progetto condivide il DB del progetto; copiato altrove, usa quello accanto a sé
DB = HOME.parent / "hunger_games.db" if FROZEN and (HOME.parent / "app.py").exists() else HOME / "hunger_games.db"
MODEL = os.environ.get("OLLAMA_MODEL", "llama3:8b")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VISION, WANDER = 5, 0.15   # raggio di visione, probabilità di un passo casuale
MAX_MEETINGS = 1           # incontri al giorno per tributo (bagno di sangue e scontro finale esclusi): meno chiamate LLM
TICK_DELAY = 0.3           # pausa tra un'ora e l'altra, per vedere i movimenti sulla mappa
LOCK = threading.Lock()    # un solo passo di simulazione alla volta
RUN = {"status": ""}       # cosa sta facendo il Direttore (e l'ora di gioco), per la UI
CUR = "(SELECT MAX(id) FROM game_state)"  # l'edizione corrente è l'ultima creata
STATS = ("intelligence", "strength", "dexterity", "charisma", "stealth")

SYSTEM = """Sei il Direttore Supremo degli Hunger Games. Gestisci 24 tributi in un'arena 32x32.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS game_state (           -- una riga per edizione dei Giochi
    id INTEGER PRIMARY KEY,                       -- numero dell'edizione
    day_number INTEGER NOT NULL DEFAULT 0,
    current_phase TEXT NOT NULL DEFAULT 'SELEZIONE',
    logs TEXT NOT NULL DEFAULT '[]',              -- JSON: diario per la UI
    event_history TEXT NOT NULL DEFAULT '[]',     -- JSON: incontri con posizione, esito, caduti
    memorial INTEGER NOT NULL DEFAULT 0 CHECK (memorial IN (0, 1)),  -- edizione della memoria
    winner_id INTEGER REFERENCES tributes (id),
    arena TEXT,                                   -- JSON: arena casuale di questa edizione (dimensioni, forma, biomi)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tributes (             -- tutti i tributi di tutte le edizioni: chi ha giocato e come è finita
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
    current_x INTEGER CHECK (current_x BETWEEN 0 AND 47),  -- arene da 32 a 48 caselle per lato
    current_y INTEGER CHECK (current_y BETWEEN 0 AND 47),
    training_score INTEGER CHECK (training_score BETWEEN 1 AND 12),  -- Fase 2
    odds TEXT                                                         -- Fase 1
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


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode = WAL")  # la UI legge mentre il Direttore scrive
        con.executescript(SCHEMA)


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


def set_state(con, **cols):  # aggiorna l'edizione corrente
    con.execute(f"UPDATE game_state SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = {CUR}", tuple(cols.values()))


def append(con, column, item):  # append atomico a un array JSON dell'edizione corrente
    con.execute(f"UPDATE game_state SET {column} = json_insert({column}, '$[#]', json(?)) WHERE id = {CUR}",
                (json.dumps(item, ensure_ascii=False),))


def log(con, kind, title, text, table=None):
    append(con, "logs", {"kind": kind, "title": title, "text": text, "table": table})


# ---------- arena ----------
BIOMES = {"C": "Cornucopia", "R": "Radura", "F": "Foresta", "M": "Montagne", "P": "Palude", "D": "Deserto",
          "L": "Lago", "G": "Giungla", "T": "Tundra", "V": "Vulcano", "O": "Rovine"}  # "X" = fuori dall'arena


def new_arena():
    """Arena casuale: lati 32-48, bordo ondulato attorno alla Cornucopia, biomi a macchie (Voronoi).
    La forma è stellata rispetto al centro: da ogni casella si rientra verso la Cornucopia, quindi la zona sicura
    che si restringe (distanza di Chebyshev dal centro) funziona con qualsiasi forma."""
    w, h = random.randint(32, 48), random.randint(32, 48)
    cx, cy = w // 2, h // 2
    waves = [(random.randint(2, 6), random.uniform(0, 2 * math.pi), random.uniform(0.03, 0.09)) for _ in range(3)]
    seeds = [(random.randrange(w), random.randrange(h), random.choice("FMPDLGTVO")) for _ in range(random.randint(6, 11))]
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
    r0 = max(max(abs(x - cx), abs(y - cy)) for y in range(h) for x in range(w) if rows[y][x] != "X")
    return {"w": w, "h": h, "c": [cx, cy], "r0": r0, "rows": rows}


def biome(A, x, y):
    return BIOMES[A["rows"][y][x]]


def walkable(A, c):
    return 0 <= c[0] < A["w"] and 0 <= c[1] < A["h"] and A["rows"][c[1]][c[0]] != "X"


def zone(A, day):  # gli Strateghi restringono l'arena ogni giorno: dal giorno 7 resta solo la Cornucopia
    return max(0, A["r0"] - math.ceil(A["r0"] / 6) * (day - 1))


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


def hunter(t):
    return t["strength"] + t["charisma"] > t["stealth"] + t["intelligence"] and t["health"] > 30


def step(t, alive, r, showdown, A, rush=False):
    """Prossima casella: aggressivi verso prede deboli o la Cornucopia, furtivi via dalle minacce e verso i bordi."""
    p, C = pos(t), tuple(A["c"])
    others = [o for o in alive if o is not t]
    inside = cheb(p, C) <= r
    cells = [(p[0] + dx, p[1] + dy) for dx, dy in DIRS.values()]
    cells = [c for c in cells if walkable(A, c) and (not inside or cheb(c, C) <= r)] or [p]
    if not inside:
        goal = lambda c: cheb(c, C)  # fuori dalla zona sicura: rientra
    elif random.random() < WANDER:
        goal = lambda c: 0
    elif showdown or hunter(t):
        prey = [] if rush else [o for o in others if 0 < cheb(p, pos(o)) <= VISION]  # rush: tutti alle armi
        target = pos(min(prey, key=lambda o: cheb(p, pos(o)) + o["strength"] / 50)) if prey else C
        goal = lambda c: cheb(c, target)
    else:
        threats = [pos(o) for o in others if cheb(p, pos(o)) <= VISION]
        goal = (lambda c: -min(cheb(c, o) for o in threats)) if threats else (lambda c: -cheb(c, C))
    return min(cells, key=lambda c: (goal(c), random.random()))


# ---------- Game Director (Ollama, output strutturato) ----------
STR = {"type": "string"}


def obj(**props):
    return {"type": "object", "properties": props, "required": list(props)}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def integer(lo, hi):
    return {"type": "integer", "minimum": lo, "maximum": hi}


def ask(prompt, schema):
    """Interroga il Direttore. `format` = JSON Schema: la risposta è sempre JSON valido con tutte le chiavi."""
    body = json.dumps({"model": MODEL, "stream": False, "format": schema,
                       "options": {"temperature": 0.8, "num_ctx": 8192, "num_predict": 4096},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": prompt}]}).encode()
    for _ in range(3):
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/chat", body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(json.load(r)["message"]["content"])
        except (OSError, ValueError, KeyError) as e:
            err = e
    raise RuntimeError(f"Ollama ({MODEL}) non risponde: {err}")


def label(t):  # chiave univoca negli schemi JSON: id + nome ripulito
    return f"T{t['id']} " + re.sub(r"[^\w .'-]", "", t["name"])


def sheet(t):
    return (f"{label(t)} — Distretto {t['district'] or '?'}, salute {t['health']}/100, uccisioni {t['kills_count']}, "
            f"addestramento {t['training_score'] or '?'}/12; INT {t['intelligence']} FOR {t['strength']} "
            f"DES {t['dexterity']} CAR {t['charisma']} FUR {t['stealth']}; "
            f"indole {'aggressiva' if hunter(t) else 'furtiva'}; "  # la stessa regola che ne guida i movimenti
            f"personalità: {', '.join(json.loads(t['personality_traits']))}; aspetto: {t['physical_description']}")


# ---------- edizioni e fasi di gioco ----------
def archetype(arch):  # (tratti JSON, *stats) di un archetipo, con ±12 casuale sulle stats
    base, traits = ARCHETYPES[arch]
    return (json.dumps([arch, *traits], ensure_ascii=False), *(clamp(b + random.randint(-12, 12), 1, 100) for b in base))


def new_game(names, memorial):
    """Nuova edizione: i nomi scritti più tributi casuali dal pool fino a 24. Restituisce un messaggio d'errore o None."""
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
        A = new_arena()
        C = tuple(A["c"])
        ring = sorted([(x, y) for x in range(C[0] - 4, C[0] + 5) for y in range(C[1] - 4, C[1] + 5)
                       if cheb((x, y), C) == 4], key=lambda c: math.atan2(c[1] - C[1], c[0] - C[0]))  # 32 piattaforme
        spots = random.sample([ring[i * len(ring) // 24] for i in range(24)], 24)
        ed = con.execute("INSERT INTO game_state (memorial, arena) VALUES (?, ?)", (int(memorial), json.dumps(A))).lastrowid
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
            + ", ".join(sorted({BIOMES[b] for row in A["rows"] for b in row if b in "FMPDLGTVO"})))


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


def interview(t):
    question, mood = random.choice(QUESTIONS), random.choice(MOODS)
    RUN["status"] = f"Fase 1: Caesar Flickerman intervista {t['name']}…"
    res = ask(f"FASE 1 — PRE-GIOCHI. Intervista di Caesar Flickerman prima dei Giochi.\n{sheet(t)}\n"
              f"Caesar chiede: «{question}»\nScrivi la risposta di {t['name']} in prima persona (2-3 frasi), con il suo "
              "stile e riferimenti concreti alla sua vita reale, alle sue opere o alle sue frasi celebri; vietate le "
              "formule generiche come 'sono pronto a tutto per vincere'. Poi la reazione del pubblico di Capitol City, "
              f"che risponde con {mood}: 1 frase concreta (un coro, un gesto, una battuta), mai un generico applauso. "
              "Infine la quota scommesse come intero N di 'N a 1', coerente con le statistiche "
              "(favoriti 2-6, medi 7-25, sfavoriti 26-99).",
              obj(risposta=STR, reazione_pubblico=STR, quota=integer(1, 99)))
    t["odds"] = f"{clamp(int(res.get('quota', 20)), 1, 99)}:1"
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
    for t in sorted(ts, key=lambda t: t["district"]):
        interview(t)
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


def encounter(group, alive, day, hour, x, y, showdown, bloodbath, A):
    """≥2 tributi nella stessa casella: il Direttore decide esito, danni, uccisioni e spostamenti."""
    RUN["status"] = f"Giorno {day} · {hour:02d}:00 · il Direttore giudica l'incontro in ({x},{y})…"
    n_alive = sum(t["status"] == "ALIVE" for t in alive)
    labels = {label(t): t for t in group}
    min_dead = len(group) // 2 if bloodbath else int(showdown)  # bagno di sangue: cade almeno metà del gruppo
    esiti = ("Combattimento", "Morte") if min_dead else tuple(ICON)
    rule = (f"BAGNO DI SANGUE ALLA CORNUCOPIA: corsa alle armi, scontri feroci; devono morire almeno {min_dead} tributi."
            if bloodbath else "SCONTRO FINALE: niente alleanze né fughe, almeno un tributo deve morire." if showdown
            else "Combattimento o Morte se almeno uno ha indole aggressiva ed è in forze; Fuga se uno è più debole, "
                 "ferito o furtivo; Alleanza solo tra indoli furtive o per reciproca convenienza.")
    per = obj(danni=integer(0, 100), morto={"type": "boolean"}, ucciso_da=enum(*labels, "nessuno"),
              direzione=enum(*DIRS))
    res = ask(f"FASE 3 — GIORNO {day}, ore {hour:02d}:00 ({'giorno' if 6 <= hour < 20 else 'notte'}). "
              f"Casella ({x},{y}), bioma: {biome(A, x, y)}. Tributi ancora in vita nell'arena: {n_alive}.\n"
              "Nella casella si incontrano:\n" + "\n".join(map(sheet, group)) + f"\n{rule}\n"
              f"Prima analizza in 1-2 frasi rapporti di forza, indole e salute; poi decidi l'esito "
              f"({', '.join(esiti)}) e narralo in 3-5 frasi. Per ogni tributo indica i danni subiti (0-100), se muore, "
              "chi lo uccide ('nessuno' se nessuno) e la direzione in cui si sposta dopo l'evento.",
              obj(analisi=STR, esito=enum(*esiti), narrazione=STR, tributi=obj(**{k: per for k in labels})))
    out, before, dead = res.get("tributi", {}), {t["id"]: t["health"] for t in group}, []
    for k, t in labels.items():
        r = out.get(k, {})
        t["health"] = clamp(t["health"] - int(r.get("danni", 0)), 0, 100)
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
    with db() as con:
        for t in group:
            save(con, t)
        append(con, "event_history", ev)
        log(con, "event", f"G{day} · {hour:02d}:00 · {ev['biome']} ({x},{y}) · {ICON.get(ev['esito'], '')} {ev['esito']}",
            f"👥 {', '.join(ev['names'])}\n{ev['text']}" + (f"\n☠️ Caduti: {', '.join(ev['dead'])}" if dead else ""))
    return ev


def play_day():
    """Una giornata = 24 ore di movimento; collisioni → Direttore; a fine giornata cronaca e riepilogo."""
    with db() as con:
        g = game(con)
        day, A = g["day_number"] + 1, json.loads(g["arena"])
        set_state(con, day_number=day, current_phase="GIOCHI")
        start = tributes(con, alive=True)
    r, seen, met, events = zone(A, day), set(), {}, []
    for tick in range(24):
        hour = (6 + tick) % 24
        RUN["status"], RUN["clock"] = f"Giorno {day} · {hour:02d}:00", (day - 1) * 24 + tick
        with db() as con:
            alive = tributes(con, alive=True)
            if len(alive) < 2:
                break
            showdown, rush = r == 0 or len(alive) == 2, day == 1 and tick < 5  # giorno 1: corsa alla Cornucopia
            for t, (x, y) in zip(alive, [step(t, alive, r, showdown, A, rush) for t in alive]):  # mosse simultanee
                t["current_x"], t["current_y"] = x, y
                save(con, t)
        cells = {}
        for t in alive:
            cells.setdefault(pos(t), []).append(t)
        for (x, y), group in cells.items():
            bloodbath = day == 1 and biome(A, x, y) == "Cornucopia"
            group = [t for t in group if showdown or bloodbath or met.get(t["id"], 0) < MAX_MEETINGS]  # stanchi: nascosti
            key = frozenset(t["id"] for t in group)
            if len(group) > 1 and key not in seen:  # ponytail: stesso gruppo una volta al giorno, niente loop tra alleati
                seen.add(key)
                for t in group:
                    met[t["id"]] = met.get(t["id"], 0) + 1
                events.append(encounter(group, alive, day, hour, x, y, showdown, bloodbath, A))
        time.sleep(TICK_DELAY)
    chronicle(day, r, events, start)


def chronicle(day, r, events, start):
    RUN["status"] = f"Giorno {day} · il Direttore scrive la cronaca…"
    with db() as con:
        alive = tributes(con, alive=True)
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
    elif phase in ("ADDESTRAMENTO", "GIOCHI"):
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
    logs, day, A = json.loads(g["logs"]), g["day_number"], json.loads(g.get("arena") or "null")
    return {"edition": g["id"], "memorial": g["memorial"], "phase": g["current_phase"], "day": day,
            "radius": zone(A, max(day, 1)) if A else 0, "model": MODEL, "busy": LOCK.locked(),
            "status": RUN["status"], "tributes": ts, "arena": A, "since": since, "logs": logs[since:],
            "events": recent(json.loads(g["event_history"]), day, RUN.get("clock", day * 24 - 1))}


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
    if not LOCK.acquire(blocking=False):
        return {"error": "Il Direttore è al lavoro: attendi la fine del passo in corso."}, 409
    try:
        error = new_game(names, data.get("memorial") is True)
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
