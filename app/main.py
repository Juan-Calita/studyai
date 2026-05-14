import shutil
import uuid
import threading
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db, get_conn
from app.services.pipeline import processar_aula
from app.services.compilar_sessao import compilar_sessao

app = FastAPI(title="StudyAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _safe_filename(titulo: str, ext: str) -> str:
    """Sanitiza titulo para uso como nome de arquivo."""
    # Remove caracteres problematicos
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in titulo)
    safe = safe.strip()[:100] or "aula"
    return f"{safe}.{ext}"


def _force_download_response(file_path: str, filename: str, media_type: str) -> FileResponse:
    """Forca download via Content-Disposition: attachment."""
    # RFC 5987: codifica o filename pra suportar acentos/UTF-8
    encoded = urllib.parse.quote(filename)
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=filename,
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded}",
            "X-Content-Type-Options": "nosniff",
        }
    )


@app.on_event("startup")
def startup():
    init_db()


@app.post("/api/aulas")
async def upload_aula(
    titulo: str = Form(...),
    audio: UploadFile = File(...),
    sessao_id: int = Form(None),
    numero_parte: int = Form(1),
):
    ext = Path(audio.filename).suffix or ".mp3"
    audio_path = str(settings.upload_dir / f"{uuid.uuid4().hex}{ext}")
    with open(audio_path, "wb") as f:
        shutil.copyfileobj(audio.file, f)

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO aulas (titulo, audio_path, status, sessao_id, numero_parte) VALUES (?, ?, 'processando', ?, ?)",
        (titulo, audio_path, sessao_id, numero_parte),
    )
    aula_id = cur.lastrowid
    conn.commit()
    conn.close()

    thread = threading.Thread(target=processar_aula, args=(aula_id,), daemon=True)
    thread.start()

    return {"aula_id": aula_id, "status": "processando"}


# ────────────────────────────── SESSÕES ──────────────────────────────

@app.post("/api/sessoes")
async def criar_sessao(titulo: str = Form(...), total_partes: int = Form(...)):
    if total_partes < 2 or total_partes > 20:
        raise HTTPException(400, "total_partes deve ser entre 2 e 20")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO sessoes (titulo, total_partes, status) VALUES (?, ?, 'aguardando')",
        (titulo, total_partes),
    )
    sessao_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"sessao_id": sessao_id, "status": "aguardando"}


@app.get("/api/sessoes/{sessao_id}")
def get_sessao(sessao_id: int):
    conn = get_conn()
    sessao = conn.execute("SELECT * FROM sessoes WHERE id=?", (sessao_id,)).fetchone()
    if not sessao:
        conn.close()
        raise HTTPException(404)

    partes = conn.execute(
        "SELECT id, titulo, numero_parte, status, erro FROM aulas WHERE sessao_id=? ORDER BY numero_parte ASC",
        (sessao_id,)
    ).fetchall()

    flashcards = conn.execute(
        "SELECT id, pergunta, resposta FROM flashcards WHERE sessao_id=?",
        (sessao_id,)
    ).fetchall()
    conn.close()

    return {
        "id": sessao["id"],
        "titulo": sessao["titulo"],
        "total_partes": sessao["total_partes"],
        "partes_prontas": sessao["partes_prontas"],
        "status": sessao["status"],
        "erro": sessao["erro"],
        "resumo": sessao["resumo"],
        "transcricao": sessao["transcricao"],
        "pdf_url": f"/api/sessoes/{sessao_id}/pdf" if sessao["pdf_path"] else None,
        "anki_url": f"/api/sessoes/{sessao_id}/anki" if sessao["anki_path"] else None,
        "partes": [
            {"id": p["id"], "numero_parte": p["numero_parte"], "status": p["status"], "erro": p["erro"]}
            for p in partes
        ],
        "flashcards": [
            {"id": fc["id"], "pergunta": fc["pergunta"], "resposta": fc["resposta"]}
            for fc in flashcards
        ],
    }


@app.post("/api/sessoes/{sessao_id}/compilar")
def compilar_sessao_manual(sessao_id: int):
    conn = get_conn()
    sessao = conn.execute("SELECT * FROM sessoes WHERE id=?", (sessao_id,)).fetchone()
    conn.close()
    if not sessao:
        raise HTTPException(404)
    if sessao["status"] in ("compilando", "gerando_resumo", "gerando_arquivos", "pronto"):
        return {"message": "Compilação já em andamento ou concluída", "status": sessao["status"]}
    thread = threading.Thread(target=compilar_sessao, args=(sessao_id,), daemon=True)
    thread.start()
    return {"message": "Compilação iniciada", "status": "compilando"}


@app.get("/api/sessoes/{sessao_id}/pdf")
def download_sessao_pdf(sessao_id: int):
    conn = get_conn()
    row = conn.execute("SELECT titulo, pdf_path FROM sessoes WHERE id=?", (sessao_id,)).fetchone()
    conn.close()
    if not row or not row["pdf_path"]:
        raise HTTPException(404)
    filename = _safe_filename(row["titulo"], "pdf")
    return _force_download_response(row["pdf_path"], filename, "application/pdf")


@app.get("/api/sessoes/{sessao_id}/anki")
def download_sessao_anki(sessao_id: int):
    conn = get_conn()
    row = conn.execute("SELECT titulo, anki_path FROM sessoes WHERE id=?", (sessao_id,)).fetchone()
    conn.close()
    if not row or not row["anki_path"]:
        raise HTTPException(404)
    filename = _safe_filename(row["titulo"], "apkg")
    return _force_download_response(row["anki_path"], filename, "application/octet-stream")


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
    filename = _safe_filename(row["titulo"], "pdf")
    return _force_download_response(row["pdf_path"], filename, "application/pdf")


@app.get("/api/aulas/{aula_id}/anki")
def download_anki(aula_id: int):
    conn = get_conn()
    row = conn.execute("SELECT titulo, anki_path FROM aulas WHERE id = ?", (aula_id,)).fetchone()
    conn.close()
    if not row or not row["anki_path"]:
        raise HTTPException(404)
    filename = _safe_filename(row["titulo"], "apkg")
    return _force_download_response(row["anki_path"], filename, "application/octet-stream")


@app.get("/")
def home():
    return FileResponse("app/static/index.html")
