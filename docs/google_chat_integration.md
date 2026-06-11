# Integración Google Chat — Flujo Guiado del CLI

Expone el flujo guiado interactivo de `main.py` (CLI) como webhook de Google
Chat, replicando **exactamente** los mismos pasos, textos y respuestas.

## Arquitectura

```
Google Chat ──HTTP POST──▶ /google-chat/webhook (api.py + adapters/google_chat.py)
                                    │
                                    ▼
                       core/guided_flow.py  ← máquina de estados
                       (réplica de la orquestación input()/print() del CLI)
                                    │
                  importa y reutiliza las funciones de main.py
                  (detectar_caso_de_uso, extraer_entidades,
                   ejecutar_consulta / ejecutar_rag / ejecutar_multiple, ...)
                                    │
            los print() internos se capturan via core/captura_stdout.py
            → la respuesta de Chat es byte-a-byte lo que imprime el CLI
```

`main.py` **no se modifica**: la máquina de estados re-implementa únicamente la
orquestación basada en `input()`, e importa todo lo demás. Las funciones del
CLI que imprimen directamente se ejecutan bajo un proxy de stdout por-contexto
(`core/captura_stdout.py`) que captura su salida sin duplicar textos.

### Render por canal (bloques semánticos)

Cada paso del flujo retorna `Bloque`s con dos representaciones:

- **`texto`** — réplica exacta del CLI (paridad verificable por tests).
- **`kind` + `data`** — versión estructurada que cada canal renderiza a su manera.

`adapters/chat_render.py` convierte los bloques al formato nativo de Chat:
títulos en `*negrita*`, sin cajas `╔═╗` ni separadores `━━━`, prompts de
terminal (`Opción:`, `Su consulta:`) eliminados, mensajes de progreso
(`Analizando...`, `Procesando...`) omitidos (en chat llegan junto con el
resultado), y documentos RAG como card aparte con fuente/similitud/extracto.

**Debug**: las líneas `#--DEBUG` y el SQL generado están ocultos por defecto.
Con `FLOW_DEBUG=1` vuelven a mostrarse (útil en el ambiente de pruebas para
validar contra el CLI sin mirar logs).

### Máquina de estados (`core/guided_flow.py`)

| Estado        | Espera                              | Equivalente CLI                      |
|---------------|-------------------------------------|--------------------------------------|
| `IDENT_TIPO`  | opción 1/2/3                        | `identificar_usuario()` (menú)       |
| `IDENT_VALOR` | nombre de agencia / NPN             | `identificar_usuario()` (valor)      |
| `QUERY`       | consulta en lenguaje natural        | `ciclo_consultas()` (prompt)         |
| `REFORMULAR`  | consulta con filtros requeridos     | bloque `filtros_requeridos`          |
| `CONFIRM`     | S / N / Omitir                      | `_input_sn(con_enter=True)`          |
| `RETRY`       | S/N (¿intentar de nuevo?)           | flujo no identificado                |
| `OTRA`        | S/N (¿otra consulta?)               | fin de cada consulta                 |

Keywords globales en cualquier estado (réplica de `_input()`): `salir`,
`volver`, `nueva sesión`, `instrucciones`, `escalar`, saludos.

### Sesiones y historial

- **Sesiones por usuario**: clave `gchat::{space}::{user}` en
  `data/guided_flow_sessions.json` (thread-safe, escritura atómica, TTL 24 h).
- **Historial conversacional**: sliding window de **10 turnos** por sesión.
  - `historial_reciente` (en la sesión): alimenta `reescribir_consulta()` para
    resolver follow-ups ambiguos ("y en Georgia?") — misma semántica que el CLI.
  - `data/conversation_history.json` (`core/conversation_store.py`): registro
    completo user+assistant de los últimos 10 turnos, TTL 24 h.

> ⚠ La persistencia es JSON en disco (decisión para el ambiente de pruebas).
> En Cloud Run el filesystem es efímero: las sesiones sobreviven mientras viva
> la instancia. Usar `--max-instances=1`. Para producción, migrar a Redis
> implementando otro store con la misma interfaz.

### Formato de respuesta

- Texto ≤ 4000 chars → mensaje de texto simple (límite de Chat: 4096).
- Texto largo (p. ej. instrucciones) → card `textParagraph` troceada.
- Menús y confirmaciones → `cardsV2` con botones (`CARD_CLICKED` envía el
  mismo valor que el usuario escribiría: `1`, `S`, `N`, ...). Escribir el
  valor a mano también funciona — paridad total con el CLI.

## Configuración

### Variables de entorno (además de las del CLI)

| Variable                | Descripción                                                       |
|-------------------------|-------------------------------------------------------------------|
| `GOOGLE_CHAT_AUDIENCE`  | Número de proyecto GCP de la app de Chat (audience del JWT)       |
| `GOOGLE_CHAT_SKIP_AUTH` | `1` = omite verificación JWT (SOLO pruebas locales, nunca en GCP) |
| `FLOW_DEBUG`            | `1` = muestra líneas `#--DEBUG` y SQL generado en las respuestas  |

La verificación JWT usa los certificados de `chat@system.gserviceaccount.com`
(Google Chat NO firma con los certs OAuth2 genéricos).

### Ejecución local

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1
```

`--workers 1` es obligatorio (ChromaDB PersistentClient + sesiones JSON locales).

### Pruebas SIN Google Chat (simulador interactivo)

No se necesita Google Chat para probar: el webhook solo recibe/devuelve JSON
con el formato de eventos de Chat. El simulador envía esos mismos eventos
(`ADDED_TO_SPACE`, `MESSAGE`, `CARD_CLICKED`) y renderiza respuestas y botones:

```bash
# Terminal 1
GOOGLE_CHAT_SKIP_AUTH=1 uvicorn api:app --port 8000 --workers 1

# Terminal 2
python scripts/simulador_google_chat.py
```

Dentro del simulador: texto normal = mensaje; `!valor` = click de botón
(ej. `!S` clickea "Confirmar"). También sirve contra Cloud Run:
`python scripts/simulador_google_chat.py --url https://<cloud-run>/google-chat/webhook`
(requiere `GOOGLE_CHAT_SKIP_AUTH=1` en el servicio mientras se prueba).

Alternativa con curl:

```bash
curl -X POST localhost:8000/google-chat/webhook -H "Content-Type: application/json" -d '{
  "type": "MESSAGE",
  "user": {"name": "users/111", "displayName": "Tester"},
  "space": {"name": "spaces/AAA"},
  "message": {"text": "hola"}
}'
```

### Deploy en Cloud Run

```bash
gcloud run deploy claro-ia-omnichannel \
  --source . \
  --region us-central1 \
  --max-instances 1 \
  --memory 4Gi \
  --timeout 300 \
  --set-env-vars GOOGLE_CHAT_AUDIENCE=<numero-proyecto>
```

En la [configuración de la app de Google Chat](https://console.cloud.google.com/apis/api/chat.googleapis.com):
- **Connection settings** → HTTP endpoint URL → `https://<cloud-run-url>/google-chat/webhook`
- Habilitar **Receive 1:1 messages** y **Join spaces and group conversations**.

### Importante: regenerar embeddings tras cambiar `use_cases.json`

El pickle `data/use_cases_embeddings.pkl` guarda una **copia completa** de cada
pregunta. Si `data/use_cases.json` cambia (p. ej. migración `sql` →
`sql_by_role`) y no se regenera el pickle, tanto el CLI como Google Chat
responden "⚙ Esta opción se encuentra en proceso de implementación":

```bash
python scripts/precalcular_use_cases.py
```

## Tests

```bash
pytest test/test_guided_flow.py test/test_conversation_store.py -v
```

`test_guided_flow.py` verifica la orquestación (estados, textos exactos, orden
de líneas) con las funciones pesadas (LLM/BigQuery/embeddings) mockeadas.

## Divergencias deliberadas con el CLI (mínimas e inevitables)

1. **Enter vacío no existe en chat** → botón "Omitir filtros" (sentinel
   `__OMITIR__`) o escribir `omitir`.
2. **`volver` fuera de la zona de confirmación** crashea el CLI (`VoverError`
   sin capturar); en chat se mapea al menú más cercano.
3. **Primer mensaje**: el CLI arranca solo; en chat el primer mensaje del
   usuario (o el evento `ADDED_TO_SPACE`) dispara header + identificación.
4. **Click en botón** muestra `_Selección: X_` como eco (el click no deja
   rastro visible en el chat, a diferencia del texto escrito).
