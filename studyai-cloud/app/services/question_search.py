import json
import numpy as np
from app.database import get_conn
from app.services.embeddings import embed_one


def buscar_similares(contexto: str, k: int = 5) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM questoes WHERE embedding IS NOT NULL").fetchall()
    conn.close()

    if not rows:
        return []

    emb_query = np.array(embed_one(contexto))
    norm_q = np.linalg.norm(emb_query)

    resultados = []
    for row in rows:
        emb_r = np.array(json.loads(row["embedding"]))
        norm_r = np.linalg.norm(emb_r)
        if norm_q > 0 and norm_r > 0:
            sim = float(np.dot(emb_query, emb_r) / (norm_q * norm_r))
        else:
            sim = 0.0
        resultados.append({
            "id": row["id"],
            "enunciado": row["enunciado"],
            "alternativas": json.loads(row["alternativas"]),
            "gabarito": row["gabarito"],
            "tema": row["tema"],
            "similaridade": round(sim, 3),
        })

    resultados.sort(key=lambda x: x["similaridade"], reverse=True)
    return resultados[:k]
