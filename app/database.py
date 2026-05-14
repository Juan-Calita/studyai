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
        CREATE TABLE IF NOT EXISTS sessoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            total_partes INTEGER NOT NULL DEFAULT 1,
            partes_prontas INTEGER NOT NULL DEFAULT 0,
            status TEXT DEFAULT 'processando',
            pdf_path TEXT,
            anki_path TEXT,
            resumo TEXT,
            transcricao TEXT,
            erro TEXT,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        );
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
            sessao_id INTEGER REFERENCES sessoes(id),
            numero_parte INTEGER DEFAULT 1,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aula_id INTEGER REFERENCES aulas(id),
            sessao_id INTEGER REFERENCES sessoes(id),
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
    # Migrations para banco existente
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Adiciona colunas novas em tabelas existentes sem recriar."""
    existing_aulas = {row[1] for row in conn.execute("PRAGMA table_info(aulas)")}
    if "sessao_id" not in existing_aulas:
        conn.execute("ALTER TABLE aulas ADD COLUMN sessao_id INTEGER REFERENCES sessoes(id)")
    if "numero_parte" not in existing_aulas:
        conn.execute("ALTER TABLE aulas ADD COLUMN numero_parte INTEGER DEFAULT 1")

    existing_fc = {row[1] for row in conn.execute("PRAGMA table_info(flashcards)")}
    if "sessao_id" not in existing_fc:
        conn.execute("ALTER TABLE flashcards ADD COLUMN sessao_id INTEGER REFERENCES sessoes(id)")
