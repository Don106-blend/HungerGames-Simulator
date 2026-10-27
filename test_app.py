"""Self-check senza Ollama (Direttore finto che rispetta gli schemi):  .venv\\Scripts\\python test_app.py"""
import random
import sys
import tempfile
from pathlib import Path

import app
from pool import ARCHETYPES, POOL


def fake_director(pacifist):
    """Riempie qualsiasi schema: a caso, oppure 'pacifista' (alleanze, zero danni) per provare che i Giochi finiscono."""
    def fill(s, key=""):
        if s["type"] == "object":
            return {k: fill(v, k) for k, v in s["properties"].items()}
        if s["type"] == "array":
            return [fill(s["items"], key) for _ in range(s["minItems"])]
        if "enum" in s:
            return "Alleanza" if pacifist and "Alleanza" in s["enum"] else random.choice(s["enum"])
        if s["type"] == "integer":
            return s["minimum"] if pacifist else random.randint(s["minimum"], s["maximum"])
        if s["type"] == "boolean":
            return not pacifist and random.random() < 0.2
        return f"testo {key}"
    return lambda prompt, schema: fill(schema)


def finish():  # all'alba di ogni giornata l'IA aspetta l'evento: qui si risponde "nessun evento"
    for _ in range(400):
        with app.db() as con:
            g = app.game(con)
        if g["current_phase"] == "FINE":
            break
        if g["current_phase"] in ("ADDESTRAMENTO", "ATTESA") and g["event_day"] != g["day_number"] + 1:
            assert app.answer_event("")
        app.next_step()
    with app.db() as con:
        return app.game(con), app.tributes(con)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    random.seed(7)
    app.DB, app.TICK_DELAY = Path(tempfile.mkdtemp()) / "test.db", 0
    app.init_db()
    assert len(POOL) == 200 and len({p[0].lower() for p in POOL}) == 200 and all(p[1] in ARCHETYPES for p in POOL)
    client = app.app.test_client()
    new = lambda **body: client.post("/api/new", json=body)
    assert client.get("/api/state").get_json()["edition"] is None
    assert new(names=[f"N{i}" for i in range(25)]).status_code == 400
    assert new(names=["Mike Tyson", "mike tyson"]).status_code == 400

    # 2 nomi scritti + 22 dal pool; distretti e scheda di chi è fuori dal pool arrivano in Fase 1
    app.ask = fake_director(False)
    assert new(names=["Mike Tyson", "Mario Rossi"]).status_code == 200
    s = client.get("/api/state").get_json()
    ts = s["tributes"]
    assert s["edition"] == 1 and len(ts) == 24 and len({app.pos(t) for t in ts}) == 24
    assert all(t["district"] is None for t in ts) and next(t for t in ts if t["name"] == "Mario Rossi")["strength"] is None
    app.next_step()
    with app.db() as con:
        ts = app.tributes(con)
    assert all(sum(t["district"] == d for t in ts) == 2 for d in range(1, 13))  # sceglie il Direttore, 2 per distretto
    assert all(t["strength"] and t["odds"] for t in ts)

    app.WANDER = 0  # movimento deterministico
    A, C = {"w": 32, "h": 32, "c": [16, 16], "r0": 16, "rows": ["F" * 32] * 32}, (16, 16)
    hunter = dict(ts[0], id=-1, strength=90, charisma=90, stealth=10, intelligence=10, health=100,
                  current_x=10, current_y=10)
    hider = dict(hunter, id=-2, strength=10, charisma=10, stealth=90, intelligence=90, current_x=12)
    assert app.cheb(app.step(hunter, [hunter, hider], 16, False, A), (12, 10)) == 1  # insegue
    assert app.cheb(app.step(hunter, [hunter, hider], 16, False, A, rush=True), C) == 5  # giorno 1: alla Cornucopia
    assert app.cheb(app.step(hider, [hunter, hider], 16, False, A), (10, 10)) == 3  # fugge
    assert app.cheb(app.step(dict(hider, current_x=0, current_y=0), [], 4, False, A), C) == 15  # rientra in zona

    # arene casuali: 32-48 per lato, piattaforme calpestabili, e da ogni casella si rientra alla Cornucopia
    for _ in range(30):
        A = app.new_arena()
        C = tuple(A["c"])
        assert 32 <= A["w"] <= 64 and 32 <= A["h"] <= 64 and len(A["rows"]) == A["h"]
        assert all(app.walkable(A, (x, y)) for x in range(C[0] - 4, C[0] + 5) for y in range(C[1] - 4, C[1] + 5))
        assert app.zone(A, 0) == A["r0"] and app.zone(A, 5) == 0
        for y in range(A["h"]):
            for x in range(A["w"]):
                if app.walkable(A, (x, y)):
                    t = dict(hider, current_x=x, current_y=y)
                    for _ in range(A["w"] + A["h"]):  # zona chiusa (r = 0): tutti verso il centro
                        t["current_x"], t["current_y"] = app.step(t, [t], 0, False, A)
                    assert app.pos(t) == C, (x, y, A["w"], A["h"])
    app.WANDER = 0.15

    # mappa: alleanze/scontri spariscono dopo 4 ore, i caduti restano per la giornata
    evs = [{"day": 2, "hour": 7, "dead": []}, {"day": 2, "hour": 7, "dead": ["X"]}, {"day": 2, "hour": 12, "dead": []}]
    assert app.recent(evs, 2, 24 + 8) == evs[1:]

    # bagno di sangue: anche con un Direttore pacifista cade almeno metà del gruppo
    app.ask = fake_director(True)
    with app.db() as con:
        A = app.json.loads(app.game(con)["arena"])
    cx, cy = A["c"]
    group = [dict(t, current_x=cx, current_y=cy) for t in ts[:6]]
    assert len(app.encounter(group, group + ts[6:], 1, 9, cx, cy, False, True, A)["dead"]) >= 3

    # fine edizione 1: il vincitore resta nell'albo d'oro e non può tornare
    app.ask = fake_director(False)
    g, ts = finish()
    champ = next(t for t in ts if t["status"] == "ALIVE")
    assert g["current_phase"] == "FINE" and g["winner_id"] == champ["id"]
    assert new(names=[champ["name"]]).status_code == 400
    fallen = {t["name"] for t in ts if t["status"] == "DEAD"}
    assert new(names=[next(iter(fallen))]).status_code == 400  # anche i morti non tornano
    assert new(memorial=True).status_code == 400  # servono 24 vincitori
    app.ask = fake_director(True)  # edizione 2: un Direttore pacifista deve comunque arrivare a un vincitore
    assert new().status_code == 200
    g, ts = finish()
    assert g["id"] == 2 and g["current_phase"] == "FINE" and not ({champ["name"]} | fallen) & {t["name"] for t in ts}
    print(f"edizione 1: vince {champ['name']} · edizione 2 (pacifista): giorno {g['day_number']}")

    with app.db() as con:  # albo d'oro finto fino a 24 campioni
        for i in range(22):
            ed = con.execute("INSERT INTO game_state (current_phase) VALUES ('FINE')").lastrowid
            tid = con.execute("INSERT INTO tributes (edition, name, physical_description, personality_traits, "
                              "intelligence, strength, dexterity, charisma, stealth) "
                              "VALUES (?, ?, 'x', '[\"Campione\"]', 50, 50, 50, 50, 50)", (ed, f"Campione {i}")).lastrowid
            con.execute("UPDATE game_state SET winner_id = ? WHERE id = ?", (tid, ed))
        champions = {w["name"] for w in app.winners(con)}
    assert new(names=["Mike Tyson"], memorial=True).status_code == 400  # nella memoria solo vincitori
    assert new(memorial=True).status_code == 200
    s = client.get("/api/state").get_json()
    assert s["memorial"] == 1 and {t["name"] for t in s["tributes"]} == champions
    assert client.get("/api/editions").get_json()["next"] == s["edition"] + 1
    print("OK")


if __name__ == "__main__":
    main()
