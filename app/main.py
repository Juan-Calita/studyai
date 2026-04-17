import shutil
import uuid
import json
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db, get_conn
from app.services.pipeline import processar_aula
from app.services.question_search import buscar_similares
from app.services import embeddings

app = FastAPI(title="StudyAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


# ── AULAS ──

@app.post("/api/aulas")
async def upload_aula(
    titulo: str = Form(...),
    audio: UploadFile = File(...),
):
    ext = Path(audio.filename).suffix or ".mp3"
    audio_path = str(settings.upload_dir / f"{uuid.uuid4().hex}{ext}")
    with open(audio_path, "wb") as f:
        shutil.copyfileobj(audio.file, f)

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO aulas (titulo, audio_path, status) VALUES (?, ?, 'processando')",
        (titulo, audio_path),
    )
    aula_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Processa em thread separada (simples, sem Celery)
    thread = threading.Thread(target=processar_aula, args=(aula_id,), daemon=True)
    thread.start()

    return {"aula_id": aula_id, "status": "processando"}


@app.get("/api/aulas/{aula_id}")
def get_aula(aula_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM aulas WHERE id = ?", (aula_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)

    flashcards = conn.execute(
        "SELECT id, pergunta, resposta FROM flashcards WHERE aula_id = ?", (aula_id,)
    ).fetchall()
    conn.close()

    return {
        "id": row["id"],
        "titulo": row["titulo"],
        "status": row["status"],
        "erro": row["erro"],
        "resumo": row["resumo"],
        "transcricao": row["transcricao"],
        "pdf_url": f"/api/aulas/{aula_id}/pdf" if row["pdf_path"] else None,
        "anki_url": f"/api/aulas/{aula_id}/anki" if row["anki_path"] else None,
        "flashcards": [
            {"id": fc["id"], "pergunta": fc["pergunta"], "resposta": fc["resposta"]}
            for fc in flashcards
        ],
    }


@app.get("/api/aulas/{aula_id}/pdf")
def download_pdf(aula_id: int):
    conn = get_conn()
    row = conn.execute("SELECT titulo, pdf_path FROM aulas WHERE id = ?", (aula_id,)).fetchone()
    conn.close()
    if not row or not row["pdf_path"]:
        raise HTTPException(404)
    return FileResponse(row["pdf_path"], media_type="application/pdf",
                        filename=f"{row['titulo']}.pdf")


@app.get("/api/aulas/{aula_id}/anki")
def download_anki(aula_id: int):
    conn = get_conn()
    row = conn.execute("SELECT titulo, anki_path FROM aulas WHERE id = ?", (aula_id,)).fetchone()
    conn.close()
    if not row or not row["anki_path"]:
        raise HTTPException(404)
    return FileResponse(row["anki_path"], media_type="application/octet-stream",
                        filename=f"{row['titulo']}.apkg")


@app.get("/api/aulas/{aula_id}/questoes-similares")
def questoes_similares(aula_id: int, k: int = 5):
    conn = get_conn()
    row = conn.execute("SELECT resumo, transcricao FROM aulas WHERE id = ?", (aula_id,)).fetchone()
    conn.close()
    if not row or not row["resumo"]:
        raise HTTPException(409, detail="Aula ainda não pronta")
    contexto = f"{row['resumo']}\n\n{(row['transcricao'] or '')[:3000]}"
    return buscar_similares(contexto, k=k)


# ── QUESTÕES ──

@app.post("/api/questoes")
def criar_questao(questao: dict):
    texto_emb = questao["enunciado"] + "\n" + "\n".join(
        f"{k}) {v}" for k, v in questao["alternativas"].items()
    )
    emb = embeddings.embed_one(texto_emb)
    conn = get_conn()
    conn.execute(
        "INSERT INTO questoes (enunciado, alternativas, gabarito, tema, embedding) VALUES (?,?,?,?,?)",
        (questao["enunciado"], json.dumps(questao["alternativas"]),
         questao["gabarito"], questao.get("tema"), json.dumps(emb)),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/questoes/importar-lote")
def importar_lote(questoes: list[dict]):
    textos = [
        q["enunciado"] + "\n" + "\n".join(f"{k}) {v}" for k, v in q["alternativas"].items())
        for q in questoes
    ]
    embs = embeddings.embed(textos)
    conn = get_conn()
    for q, emb in zip(questoes, embs):
        conn.execute(
            "INSERT INTO questoes (enunciado, alternativas, gabarito, tema, embedding) VALUES (?,?,?,?,?)",
            (q["enunciado"], json.dumps(q["alternativas"]),
             q["gabarito"], q.get("tema"), json.dumps(emb)),
        )
    conn.commit()
    conn.close()
    return {"importadas": len(questoes)}


@app.get("/api/questoes")
def listar_questoes():
    conn = get_conn()
    rows = conn.execute("SELECT id, enunciado, alternativas, gabarito, tema FROM questoes").fetchall()
    conn.close()
    return [
        {"id": r["id"], "enunciado": r["enunciado"],
         "alternativas": json.loads(r["alternativas"]),
         "gabarito": r["gabarito"], "tema": r["tema"]}
        for r in rows
    ]


# ── INTERFACE ──

@app.get("/")
def home():
    return FileResponse("app/static/index.html")
