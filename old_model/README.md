# Claro Insurance — Omnichannel IA

Sistema de consulta inteligente en lenguaje natural para datos operativos de Claro Insurance. Permite a representantes de agencias, agentes NPN y personal de gerencia consultar contratos, comisiones, pagos y documentos normativos en español, usando modelos de lenguaje generativo y búsqueda semántica.

---

## Arquitectura general

```
Usuario (texto en español)
        │
        ▼
  Reescritura con contexto       ← Gemini Flash Lite
        │
        ▼
  Normalización de intención     ← Gemini Flash Lite
        │
        ▼
  Detección de caso de uso       ← Embeddings + umbral semántico
        │
        ▼
  Extracción de entidades        ← N-gramas + embeddings precalculados
        │
        ▼
  Confirmación con el usuario
        │
        ├──── SQL ──────────────→ Gemini Pro → BigQuery → respuesta formateada
        ├──── RAG ──────────────→ ChromaDB → top 5 docs → Gemini Flash Lite
        └──── MULTIPLE ─────────→ Ejecución paralela SQL+RAG → síntesis
        │
        ▼
  Log asíncrono (JSONL + BigQuery)
```

### Componentes principales

| Componente | Tecnología | Rol |
|---|---|---|
| LLM respuestas | Gemini 2.5 Flash Lite (Vertex AI) | Reescritura, normalización, síntesis |
| LLM SQL | Gemini 2.5 Pro (Vertex AI) | Generación de SQL para BigQuery |
| Base de datos | Google BigQuery | Fuente de datos operativos |
| Vector store | ChromaDB (local persistente) | RAG sobre documentos normativos |
| Embeddings | `all-MiniLM-L6-v2` (SentenceTransformers) | Matching semántico (384 dimensiones) |
| Logs | JSONL diario + tabla BigQuery | Trazabilidad y monitoreo |

---

## Estructura del proyecto

```
Omnichannel - Chroma/
├── main.py                        # Núcleo de la aplicación (~1800 líneas)
├── .env                           # Variables de entorno (no se sube al repo)
├── env_example.txt                # Plantilla de variables de entorno
│
├── config/
│   ├── __init__.py                # Inicialización de Vertex AI y BigQuery
│   ├── chroma_client.py           # Wrapper de ChromaDB
│   └── logger.py                  # Sistema de logging (JSONL + BigQuery)
│
├── data/
│   ├── use_cases.json             # Definición de todos los casos de uso
│   ├── catalog_permissions.json   # Permisos por tipo de usuario
│   ├── filtros_validos.pkl        # Valores de filtros cacheados (de BigQuery)
│   └── filtros_embeddings.pkl     # Embeddings precalculados de filtros
│
├── scripts/
│   ├── precalcular_filtros.py     # Recarga filtros desde BigQuery y los cachea
│   ├── precalcular_embeddings.py  # Indexa PDFs en ChromaDB
│   ├── precalcular_use_cases.py   # Genera embeddings de casos de uso
│   ├── manage_users.py            # Gestión de usuarios del sistema
│   └── delete_collections.py      # Limpieza de colecciones en ChromaDB
│
├── test/
│   ├── revisar_filtros_validos.py        # Verifica filtros cacheados
│   ├── revisar_use_cases_embeddings.py   # Verifica embeddings de casos
│   └── revisar_embeddings.py             # Verifica embeddings de documentos
│
├── pdf/                           # Documentos fuente para RAG (PDFs)
├── chroma_db/                     # Almacenamiento persistente de ChromaDB
├── logs/                          # Logs diarios en formato JSONL
└── notes/                         # Notas internas del proyecto
```

---

## Configuración inicial

### 1. Requisitos previos

- Python 3.10+
- Acceso a Google Cloud Platform con dos cuentas de servicio:
  - Una para **Vertex AI** (modelos Gemini)
  - Una para **BigQuery** (datos operativos)
- Archivos `.json` de credenciales ubicados en `./service/`

### 2. Instalar dependencias

```bash
pip install python-dotenv google-cloud-aiplatform google-auth \
    google-cloud-bigquery chromadb sentence-transformers \
    pypdf numpy python-docx
```

### 3. Configurar variables de entorno

Copia `env_example.txt` como `.env` en la raíz del proyecto y completa los valores:

```env
VERTEX_CREDENTIALS_JSON=./service/claro-gemini-key.json
VERTEX_PROJECT_ID=claroinsurance-dataplatform
VERTEX_LOCATION=us-central1
BQ_CREDENTIALS_JSON=./service/clouddemo-service-account.json
CHROMA_PATH=./chroma_db
```

---

## Precálculo de datos (pasos obligatorios antes del primer uso)

Estos scripts deben ejecutarse **una vez** al iniciar el proyecto y **cada vez que cambien los datos fuente**.

### Paso 1 — Cargar filtros desde BigQuery

Descarga los valores válidos de filtros (carriers, estados, agencias, NPNs, etc.) y los cachea localmente con sus embeddings.

```bash
python scripts/precalcular_filtros.py
```

Genera:
- `data/filtros_validos.pkl`
- `data/filtros_embeddings.pkl`

### Paso 2 — Indexar documentos normativos en ChromaDB

Lee los PDFs del directorio `./pdf/`, los divide en chunks y los almacena en ChromaDB con sus embeddings.

```bash
python scripts/precalcular_embeddings.py
```

Genera colecciones en `./chroma_db/` para búsqueda semántica.

### Paso 3 — Generar embeddings de casos de uso

Codifica los ejemplos semánticos de cada caso de uso definido en `data/use_cases.json`.

```bash
python scripts/precalcular_use_cases.py
```

Genera el índice que permite mapear preguntas del usuario a los casos de uso correctos.

---

## Ejecución

Con los precálculos completos, inicia la aplicación en modo consola:

```bash
python main.py
```

El sistema pedirá identificación del usuario (tipo y datos) y luego entrará en el ciclo de consultas.

---

## Lógica interna detallada

### Identificación de usuario

El sistema reconoce tres tipos de usuario:

| Tipo | Descripción | Catálogos disponibles |
|---|---|---|
| 1 | Representante de Agencia | A, B, C |
| 2 | Agente NPN | A, B, C |
| 3 | Gerencia | A, B, C |

La identificación usa matching semántico (embeddings + umbral de confianza configurable entre 0.65 y 0.95 según el tipo).

### Pipeline de consulta

1. **Reescritura con contexto**: Si la consulta hace referencia a algo mencionado antes ("¿y de ese carrier?"), el LLM la reescribe como una pregunta autocontenida.

2. **Normalización de intención**: Se eliminan entidades concretas (nombres de carriers, fechas, estados, NPNs) y se preserva solo la acción. Salida máxima de 8 palabras. Ej: `"comisiones pagadas a Humana en mayo 2024 en Florida"` → `"comisiones pagadas carrier estado fecha"`.

3. **Detección de caso de uso**: La intención normalizada se compara por similitud coseno contra los embeddings de todos los ejemplos en `use_cases.json`. Se requiere un umbral mínimo de 0.75 para aceptar la detección.

4. **Extracción de entidades**: Descompone la consulta original en N-gramas (1 a 4 palabras) y los compara contra los embeddings de los valores válidos de cada filtro. Cada tipo de filtro tiene su propio umbral de similitud. Casos especiales:
   - **Fechas**: Parser de expresiones en español ("el mes pasado", "mayo 2024", "último trimestre")
   - **NPN**: Extracción por regex de patrones numéricos
   - **Número de póliza**: Regex después de la palabra "póliza"
   - **IDs de oportunidad**: Extracción de cadenas hexadecimales

5. **Confirmación**: El sistema muestra el caso de uso detectado y las entidades encontradas al usuario para validar antes de ejecutar.

6. **Ejecución**:
   - `sql`: Gemini Pro genera una query BigQuery a partir de una plantilla definida en `use_cases.json`, inyectando filtros dinámicos. Si BigQuery falla, reintenta hasta 2 veces. Solo se permiten consultas `SELECT` — cualquier intento de `INSERT`, `UPDATE`, `DELETE`, `DROP`, etc. es bloqueado por regex.
   - `rag`: Codifica la consulta y busca en ChromaDB los 5 fragmentos más relevantes. Gemini Flash Lite sintetiza la respuesta.
   - `multiple`: Ejecuta varios sub-queries en paralelo (via `ThreadPoolExecutor`) y fusiona los resultados en una sola respuesta.

### Sistema de logging

Cada consulta genera un registro JSON con:

- Queries original, reescrita y normalizada
- Caso detectado y score de confianza
- Entidades extraídas (parámetro, valor, score de similitud)
- SQL generada e intentos realizados
- Filas devueltas por BigQuery
- Latencias individuales (normalización, detección, entidades, SQL, BQ, RAG, respuesta) en milisegundos
- Respuesta final del LLM

Los logs se escriben en `./logs/YYYY-MM-DD.jsonl` (una línea JSON por consulta) y se envían de forma asíncrona a la tabla `claroinsurance-dataplatform.claro_IA.model_tracking` en BigQuery.

---

## Catálogos y casos de uso

Los casos de uso están definidos en `data/use_cases.json`. Cada caso incluye:

- `tipo`: `sql`, `rag`, o `multiple`
- `sql`: Plantilla de query con placeholder `{dynamic_filters}`
- `semantic_examples`: 10-20 variaciones en lenguaje natural para el matching
- `parametros`: Lista de filtros SQL que el caso puede recibir
- `descripciones`: Definición de columnas para que el LLM las incluya en la respuesta
- `entity_resolution` y `ending_resolution`: Reglas de formato de respuesta

### Catálogo A — Contratos
Consultas sobre estado de contratos: activos, pendientes, inactivos, detalles, licencias, validaciones.

### Catálogo B — Pagos y Comisiones
Consultas sobre comisiones pagadas, preliquidaciones, reconciliaciones, pagos pendientes por agente o agencia.

### Catálogo C — Documentos Normativos
RAG sobre PDFs institucionales: calendarios, FAQs, instructivos de carriers.

---

## Administración y mantenimiento

### Actualizar filtros (cuando cambien datos en BigQuery)

```bash
python scripts/precalcular_filtros.py
```

### Re-indexar documentos (cuando se agreguen o modifiquen PDFs)

```bash
python scripts/precalcular_embeddings.py
```

### Verificar el estado de los datos precalculados

```bash
python test/revisar_filtros_validos.py
python test/revisar_use_cases_embeddings.py
python test/revisar_embeddings.py
```

### Limpiar colecciones de ChromaDB

```bash
python scripts/delete_collections.py
```

### Gestión de usuarios

```bash
# Agregar usuario
python scripts/manage_users.py add

# Listar usuarios
python scripts/manage_users.py list

# Eliminar usuario
python scripts/manage_users.py remove
```

---

## Agregar nuevos casos de uso

1. Abrir `data/use_cases.json`
2. Agregar un nuevo objeto dentro del catálogo correspondiente (`A`, `B`, o `C`) con los campos: `tipo`, `sql`, `semantic_examples`, `parametros`, `descripciones`, `entity_resolution`, `ending_resolution`
3. Re-ejecutar el precálculo de embeddings de casos de uso:
   ```bash
   python scripts/precalcular_use_cases.py
   ```

No se requiere modificar `main.py` para agregar casos de uso estándar.

---

## Agregar nuevos documentos normativos (RAG)

1. Colocar el PDF en el directorio `./pdf/`
2. Agregar su configuración en `scripts/precalcular_embeddings.py` (nombre de archivo, colección destino, metadatos como carrier y categoría, estrategia de chunking)
3. Re-ejecutar la indexación:
   ```bash
   python scripts/precalcular_embeddings.py
   ```

---

## Variables de entorno requeridas

| Variable | Descripción |
|---|---|
| `VERTEX_CREDENTIALS_JSON` | Ruta al JSON de credenciales para Vertex AI |
| `VERTEX_PROJECT_ID` | ID del proyecto en GCP |
| `VERTEX_LOCATION` | Región de Vertex AI (ej. `us-central1`) |
| `BQ_CREDENTIALS_JSON` | Ruta al JSON de credenciales para BigQuery |
| `CHROMA_PATH` | Directorio de almacenamiento de ChromaDB |
