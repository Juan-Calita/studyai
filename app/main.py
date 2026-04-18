import shutil
import uuid
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db, get_conn
from app.services.pipeline import processar_aula

app = FastAPI(title="StudyAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup():
    init_db()


@app.post("/api/aulas")
async def upload_aula(titulo: str = Form(...), audio: UploadFile = File(...)):
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


@app.get("/")
def home():
    return FileResponse("app/static/index.html")
