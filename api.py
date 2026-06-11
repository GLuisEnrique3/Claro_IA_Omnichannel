"""
Punto de entrada FastAPI — Claro IA Omnichannel (Google Chat).

Iniciar con:
    uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1

IMPORTANTE: --workers 1 es obligatorio:
    - ChromaDB PersistentClient no admite acceso concurrente multi-proceso.
    - Las sesiones del flujo guiado se persisten en JSON local; múltiples
      workers competirían por el mismo archivo.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.captura_stdout import instalar_proxy
from adapters.google_chat import router as google_chat_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Proxy de stdout para captura por-contexto de los prints del CLI
    instalar_proxy()
    # Pre-cargar el modelo de embeddings: evita el cold start del primer request
    import main as cli
    await asyncio.to_thread(cli._get_embed_model)
    yield


app = FastAPI(
    title="Claro Insurance IA Omnichannel",
    description="Flujo guiado del agente IA de Claro Insurance expuesto a Google Chat",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(google_chat_router, prefix="/google-chat", tags=["Google Chat"])


@app.get("/health", tags=["Health"])
async def health():
    """Verifica que el servidor esté en funcionamiento."""
    return {"status": "ok"}
