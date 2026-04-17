import sqlite3
from app.config import settings

DB = str(settings.db_path)


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS aulas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            audio_path TEXT,
            pdf_path TEXT,
            anki_path TEXT,
            transcricao TEXT,
            resumo TEXT,
            embedding TEXT,
            status TEXT DEFAULT 'processando',
            erro TEXT,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aula_id INTEGER REFERENCES aulas(id),
            pergunta TEXT NOT NULL,
            resposta TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enunciado TEXT NOT NULL,
            alternativas TEXT,
            gabarito TEXT,
            tema TEXT,
            embedding TEXT
        );
    """)
    conn.commit()
    conn.close()
