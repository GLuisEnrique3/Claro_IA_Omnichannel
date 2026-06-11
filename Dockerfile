# =========================================================
# ETAPA 1: Builder — instala deps + descarga modelo embeddings
# =========================================================
FROM python:3.11-slim AS builder
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    --default-timeout=100 --retries=10 \
    -r requirements.txt

# Pre-descargar el modelo de embeddings (evita descarga en runtime)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# =========================================================
# ETAPA 2: Producción
# =========================================================
FROM python:3.11-slim AS runner
WORKDIR /app

# Venv con todas las dependencias
COPY --from=builder /opt/venv /opt/venv
# Modelo de embeddings pre-descargado
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

# Código fuente
COPY core/ ./core/
COPY adapters/ ./adapters/
COPY config/ ./config/
COPY app/ ./app/
COPY main.py .
COPY api.py .

# Artefactos pre-generados (pkl + chroma)
COPY data/ ./data/
COPY chroma_db/ ./chroma_db/

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HF_HUB_OFFLINE=1

EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
