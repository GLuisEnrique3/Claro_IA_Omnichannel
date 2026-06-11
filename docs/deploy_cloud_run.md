# Runbook — Deploy a Cloud Run (Google Chat webhook)

Guía de despliegue para el equipo de infra. La imagen expone el webhook de
Google Chat (`/google-chat/webhook`) y el health check (`/health`).

## 1. Build de la imagen desde la branch

⚠ **El clone de git NO basta para construir**: los embeddings `data/*.pkl`
están gitignorados (no viajan por git) y el Dockerfile los copia a la imagen.
Sin ellos, TODOS los flujos responden "⚙ en proceso de implementación".
`chroma_db/` sí está versionado y llega con el clone.

Pasos:

```bash
# 1) Obtener el código
git clone <repo> && cd Claro_IA_Omnichannel
git checkout feat/google-chat-guided-flow

# 2) Generar los artefactos (requiere Python 3.11+ y las keys de GCP)

# 2a. Entorno con dependencias (torch CPU, mismo índice que el Dockerfile)
python3 -m venv .venv && source .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple -r requirements.txt

# 2b. Credenciales: crear .env en la raíz apuntando a las MISMAS keys de
#     service account que usa el servicio de Cloud Run:
#       VERTEX_CREDENTIALS_JSON=/ruta/a/vertex-key.json
#       BQ_CREDENTIALS_JSON=/ruta/a/bq-key.json

# 2c. Generar embeddings de casos de uso (sin credenciales, ~2-5 min;
#     descarga el modelo MiniLM la primera vez)
python scripts/precalcular_use_cases.py

# 2d. Generar filtros válidos + sus embeddings (consulta BigQuery — el SA
#     necesita lectura en claro_bi y salesforce_claro, ~2-5 min)
python scripts/precalcular_filtros.py

# 2e. Verificar
ls data/*.pkl
#   data/filtros_embeddings.pkl     (~52 MB)
#   data/filtros_validos.pkl        (~240 KB)
#   data/use_cases_embeddings.pkl   (~2.6 MB)

# NOTA: NO ejecutar scripts/precalcular_embeddings.py — regenera ChromaDB
# desde los PDFs y no hace falta: chroma_db/ ya viene versionado en git.

# 3) Build — el --platform es OBLIGATORIO si la máquina de build es ARM
#    (Mac M-series): Cloud Run solo ejecuta linux/amd64
docker build --platform linux/amd64 -t claro-ia-omnichannel:google-chat .

# 4) Smoke-check de la imagen (no necesita credenciales):
docker run --rm --entrypoint sh claro-ia-omnichannel:google-chat -c '
  ls data/*.pkl > /dev/null && echo "pkl OK" &&
  ls chroma_db/chroma.sqlite3 > /dev/null && echo "chroma OK" &&
  test ! -f data/conversation_history.json && echo "sin datos de usuarios OK" &&
  python -c "import pickle; print(len(pickle.load(open(\"data/use_cases_embeddings.pkl\",\"rb\"))), \"casos de uso\")"'
# Esperado: pkl OK / chroma OK / sin datos de usuarios OK / 34 casos de uso

# 5) Tag + push al registry de la organización
docker tag claro-ia-omnichannel:google-chat <registry>/<repo>/claro-ia-omnichannel:<tag>
docker push <registry>/<repo>/claro-ia-omnichannel:<tag>
```

## 2. Variables de entorno del servicio

| Variable | Obligatoria | Valor / descripción |
|---|---|---|
| `VERTEX_CREDENTIALS_JSON` | Sí | **Ruta de archivo** al JSON del SA para Vertex AI (ver §3) |
| `BQ_CREDENTIALS_JSON` | Sí | **Ruta de archivo** al JSON del SA para BigQuery (ver §3) |
| `GOOGLE_CHAT_AUDIENCE` | Sí | Project Number (numérico) del proyecto GCP de la app de Chat |
| `BQ_FLUSH_SYNC` | Sí → `1` | Tracking a BigQuery dentro del request. **Obligatorio en Cloud Run**: con CPU throttling, el thread daemon de flush muere al enviar la respuesta |
| `VERTEX_PROJECT_ID` | No | Default: `claroinsurance-dataplatform` |
| `VERTEX_LOCATION` | No | Default: `us-central1` |
| `FLOW_DEBUG` | No | `1` muestra SQL y líneas debug en las respuestas de Chat (solo para ventanas de prueba) |
| `GOOGLE_CHAT_SKIP_AUTH` | **NUNCA en Cloud Run** | Desactiva la verificación JWT — solo para desarrollo local |

Notas:
- `JWT_SECRET` y `CORS_ORIGINS` del SETUP antiguo **ya no aplican** (esta
  imagen no incluye la REST API).

## 3. Credenciales (keys de service account)

Las keys **no viajan en la imagen** (`.env` y JSONs excluidos). El código
espera **rutas de archivo** en las env vars — el patrón recomendado es
Secret Manager montado como volumen:

1. Subir las dos keys a Secret Manager (p. ej. `vertex-sa-key`, `bq-sa-key`).
2. En el servicio: montar como volumen, p. ej. en `/secrets/`.
3. Setear `VERTEX_CREDENTIALS_JSON=/secrets/vertex-sa-key` y
   `BQ_CREDENTIALS_JSON=/secrets/bq-sa-key`.

Si el primer deploy ya tenía otro mecanismo equivalente funcionando, basta con
conservarlo — el contrato (rutas en esas dos vars) no cambió.

**Permisos del SA de BigQuery** (verificados el 2026-06-11 para
`iaomnichannelhfranco@claroinsurance-dataplatform.iam.gserviceaccount.com`):
- Lectura: datasets `claro_bi`, `salesforce_claro`, `salesforce_raw`
- Escritura: dataset `claro_IA` (tracking `model_tracking`)

Si el servicio usa OTRO SA, replicar esos permisos antes del deploy.

## 4. Configuración del servicio Cloud Run

| Parámetro | Valor | Por qué |
|---|---|---|
| `--max-instances` | **1 (obligatorio)** | Sesiones del flujo en JSON local + ChromaDB PersistentClient: no soportan múltiples instancias |
| `--memory` | `4Gi` | torch + sentence-transformers + Chroma en memoria |
| `--cpu` | `2` | embeddings sobre CPU |
| `--timeout` | `300` | consultas SQL+LLM tardan 10-30s; multiple aún más |
| `--concurrency` | `10` (default ok) | el lock por sesión serializa por usuario |
| Puerto | el del servicio (la imagen respeta `$PORT`) | |
| CPU allocation | "only during request processing" OK | el flush BQ es síncrono (`BQ_FLUSH_SYNC=1`) |

**Startup probe**: el contenedor precarga el modelo de embeddings ANTES de
aceptar tráfico (lifespan de FastAPI) — el arranque tarda **30-60 s**.
Configurar startup probe a `GET /health` con margen (p. ej. period 10s,
failure threshold 12) y/o habilitar startup CPU boost. Sin esto, Cloud Run
puede matar la instancia antes de que termine de arrancar.

## 5. Conectar Google Chat

En la [configuración de la app de Chat](https://console.cloud.google.com/apis/api/chat.googleapis.com)
→ Connection settings → HTTP endpoint URL:

```
https://<url-del-servicio>/google-chat/webhook
```

(Hoy el webhook apunta vía túnel a la máquina de desarrollo — este deploy
existe precisamente para retirar ese túnel.)

## 6. Verificación post-deploy

1. `curl https://<url>/health` → `{"status":"ok"}` (esperar el arranque).
2. Sin Authorization debe responder **401** en el webhook (JWT activo):
   `curl -X POST https://<url>/google-chat/webhook -d '{}' -H "Content-Type: application/json"`
3. Desde Google Chat: enviar `hola` → header + menú de identificación.
4. Flujo SQL completo: identificarse como `3`, enviar
   `cuantos contratos activos hay con Ambetter en Florida`, confirmar →
   debe responder con cifras (valida lectura BigQuery).
5. Tracking: verificar la fila en
   `claroinsurance-dataplatform.claro_IA.model_tracking`
   (valida escritura BigQuery + `BQ_FLUSH_SYNC`).
6. Si algo falla: los errores de tracking aparecen en Cloud Logging
   (stderr, prefijo `bq_flush_error`); con `FLOW_DEBUG=1` el SQL generado
   se ve en el propio chat.

## 7. Qué requiere rebuild de la imagen

| Cambio | ¿Rebuild? |
|---|---|
| Código (`core/`, `adapters/`, `main.py`, `api.py`, `config/`) | Sí |
| `data/use_cases.json` o ejemplos semánticos | Sí — **regenerar antes** `python scripts/precalcular_use_cases.py` |
| Documentos RAG (words/, pdf/) | Sí — regenerar `chroma_db/` antes |
| Env vars / secrets | No — solo nueva revisión del servicio |
