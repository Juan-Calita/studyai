# StudyAI Cloud ☁️

Pipeline de aulas 100% na nuvem, sem GPU, sem seu PC ligado.

Stack: Gemini (transcrição + LLM + embeddings) + FastAPI + SQLite

## Deploy no Render (grátis)

### Passo 1 — Criar conta no GitHub
Acesse https://github.com e crie uma conta (se já tiver, pule).

### Passo 2 — Criar repositório
1. Clique no botão **"+"** → **"New repository"**
2. Nome: `studyai`
3. Marque **"Private"**
4. Clique **"Create repository"**

### Passo 3 — Upload dos arquivos
Na página do repo que acabou de criar:
1. Clique em **"uploading an existing file"**
2. Arraste TODOS os arquivos desta pasta (NÃO o zip, os arquivos soltos)
3. Clique **"Commit changes"**

### Passo 4 — Criar conta no Render
Acesse https://render.com e faça login com sua conta GitHub.

### Passo 5 — Criar o serviço
1. No dashboard do Render, clique **"New +"** → **"Web Service"**
2. Conecte seu repositório `studyai`
3. Configure:
   - **Name:** studyai
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Em **"Environment Variables"**, adicione:
   - Key: `GEMINI_API_KEY` → Value: sua chave do Gemini
5. Clique **"Create Web Service"**

### Passo 6 — Esperar o deploy (~3 min)
Quando terminar, o Render vai te dar uma URL tipo:
`https://studyai-xxxx.onrender.com`

### Passo 7 — Popular questões
No seu PC (PowerShell), rode:
```
pip install requests
python seed_questoes.py https://studyai-xxxx.onrender.com
```

### Pronto!
Acesse a URL do Render de qualquer lugar. Mande pra sua namorada e usem juntos.

## Notas
- O plano grátis do Render "dorme" após 15min sem uso. A primeira visita demora ~30s pra acordar.
- O SQLite reseta quando o Render re-deploya (questões importadas somem). Para uso sério, migre para PostgreSQL (Render oferece grátis também).
