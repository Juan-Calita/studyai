from pydantic_settings import BaseSettings
from pathlib import Path
import os


class Settings(BaseSettings):
    gemini_api_key: str = ""
    port: int = int(os.environ.get("PORT", 8000))

    data_dir: Path = Path("./data")
    upload_dir: Path = Path("./data/uploads")
    pdf_dir: Path = Path("./data/pdfs")
    anki_dir: Path = Path("./data/anki")
    db_path: Path = Path("./data/studyai.db")

    class Config:
        env_file = ".env"


settings = Settings()
for d in (settings.data_dir, settings.upload_dir, settings.pdf_dir, settings.anki_dir):
    d.mkdir(parents=True, exist_ok=True)
