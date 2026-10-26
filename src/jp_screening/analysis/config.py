from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse

from ..core import ScreeningError


@dataclass(frozen=True)
class Config:
    root: Path
    database_url: str
    reader_url: str
    ollama_url: str
    chat_model: str
    embed_model: str
    api_key: str
    auto_import: bool

    @classmethod
    def load(cls, root=None):
        from dotenv import dotenv_values
        root = Path(root or Path(__file__).resolve().parents[3]).resolve()
        values = {**dotenv_values(root / ".env"), **os.environ}
        url = values.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.query or parsed.path:
            raise ScreeningError("OllamaはローカルHTTPの接続先を指定してください。")
        db = values.get("ANALYSIS_DATABASE_URL", "")
        return cls(root, db, values.get("ANALYSIS_READER_URL") or db, url,
                   values.get("OLLAMA_CHAT_MODEL", "llama3.1:8b"),
                   values.get("OLLAMA_EMBED_MODEL", "embeddinggemma:300m"),
                   values.get("JQUANTS_API_KEY", ""),
                   str(values.get("ANALYSIS_AUTO_IMPORT", "false")).lower() == "true")
