# HungerGames-Simulator

Hunger Games diretti da un LLM locale (Ollama), con arena casuale, edizioni e storico.

**Avvio rapido:** avvia [Ollama](https://ollama.com) con almeno un modello (es. `ollama pull llama3:8b`), poi lancia `dist/HungerGames.exe`: il server parte e la pagina si apre da sola su http://localhost:5001. Il gioco usa `llama3:8b` se installato, altrimenti il primo modello disponibile; per sceglierne uno preciso imposta `OLLAMA_MODEL`. Se Ollama non risponde, l'intestazione della pagina lo segnala. Il database `hunger_games.db` viene creato accanto all'exe.

**Da sorgente:** `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `.venv\Scripts\python app.py`.

**Ricostruire l'exe:** `.venv\Scripts\pip install pyinstaller` e poi `.venv\Scripts\pyinstaller --onefile --name HungerGames --add-data "index.html;." app.py`.
