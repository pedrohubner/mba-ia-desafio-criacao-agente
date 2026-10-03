import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

DADOS_DIR = RAIZ / "dados"

VAR_DIR = Path(os.getenv("AURORA_VAR_DIR") or RAIZ / "var")
CONDOMINIO_DB = VAR_DIR / "condominio.db"
SESSOES_DB = VAR_DIR / "sessoes.db"

APP_NAME = "aurora"

MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.5-flash-lite"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or "gemini-3.5-flash-lite"
