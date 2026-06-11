"""
Simulador interactivo de Google Chat — prueba el webhook sin Google Chat real.

Envía al webhook los MISMOS eventos JSON que genera Google Chat
(ADDED_TO_SPACE, MESSAGE, CARD_CLICKED) y renderiza en terminal las
respuestas: texto, cards y botones.

Uso:
    # Terminal 1 — levantar el API sin verificación JWT:
    GOOGLE_CHAT_SKIP_AUTH=1 uvicorn api:app --port 8000 --workers 1

    # Terminal 2 — simular la conversación:
    python scripts/simulador_google_chat.py
    python scripts/simulador_google_chat.py --url https://<cloud-run>/google-chat/webhook

Dentro del simulador:
    - Escribe texto normal        → evento MESSAGE (como escribir en Chat)
    - Escribe !<valor>  (ej: !S)  → evento CARD_CLICKED (como clickear un botón)
    - Ctrl+C o Ctrl+D             → salir del simulador
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

USER = {"name": "users/simulador-local", "displayName": "Tester Local"}
SPACE = {"name": "spaces/simulador-local"}


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _evento_message(texto: str) -> dict:
    return {"type": "MESSAGE", "user": USER, "space": SPACE, "message": {"text": texto}}


def _evento_card_clicked(valor: str) -> dict:
    return {
        "type": "CARD_CLICKED",
        "user": USER,
        "space": SPACE,
        "action": {
            "actionMethodName": "responder",
            "parameters": [{"key": "valor", "value": valor}],
        },
    }


def _render(respuesta: dict) -> None:
    """Renderiza la respuesta del webhook como la mostraría Google Chat."""
    texto = respuesta.get("text", "")
    if texto:
        print(texto)

    for card in respuesta.get("cardsV2", []):
        for seccion in card.get("card", {}).get("sections", []):
            for widget in seccion.get("widgets", []):
                if "textParagraph" in widget:
                    print(widget["textParagraph"]["text"])
                if "buttonList" in widget:
                    print()
                    print("  ┌─ BOTONES " + "─" * 44)
                    for boton in widget["buttonList"]["buttons"]:
                        valor = next(
                            (p["value"] for p in boton["onClick"]["action"]["parameters"]
                             if p["key"] == "valor"),
                            "",
                        )
                        print(f"  │  [{boton['text']}]  →  escribe: !{valor}")
                    print("  └" + "─" * 54)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulador de Google Chat")
    parser.add_argument(
        "--url",
        default="http://localhost:8000/google-chat/webhook",
        help="URL del webhook (default: localhost:8000)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  SIMULADOR GOOGLE CHAT — Claro IA Omnichannel")
    print(f"  Webhook: {args.url}")
    print("  Texto normal = MESSAGE · !valor = click de botón (ej: !S)")
    print("=" * 60)
    print()
    print(">>> evento: ADDED_TO_SPACE (el bot entra al espacio)")
    print()

    try:
        _render(_post(args.url, {"type": "ADDED_TO_SPACE", "user": USER, "space": SPACE}))
    except Exception as exc:
        print(f"❌ No se pudo contactar el webhook: {exc}")
        print("   ¿Está corriendo? → GOOGLE_CHAT_SKIP_AUTH=1 uvicorn api:app --port 8000")
        sys.exit(1)

    while True:
        try:
            print()
            entrada = input("Tú: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[simulador terminado]")
            break

        if not entrada:
            continue

        if entrada.startswith("!"):
            evento = _evento_card_clicked(entrada[1:])
            print(f">>> evento: CARD_CLICKED (valor: {entrada[1:]!r})")
        else:
            evento = _evento_message(entrada)

        print()
        try:
            _render(_post(args.url, evento))
        except Exception as exc:
            print(f"❌ Error del webhook: {exc}")


if __name__ == "__main__":
    main()
