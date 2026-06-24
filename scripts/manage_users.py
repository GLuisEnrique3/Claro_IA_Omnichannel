import sys
import json
import hashlib
from pathlib import Path

USERS_FILE = Path(__file__).parent.parent / "app" / "users.json"


def _load() -> dict:
    if not USERS_FILE.exists():
        return {}
    return json.loads(USERS_FILE.read_text(encoding="utf-8"))


def _save(users: dict) -> None:
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def cmd_add(username: str, password: str) -> None:
    users = _load()
    users[username] = _hash(password)
    _save(users)
    print(f"✅ Usuario '{username}' agregado.")


def cmd_remove(username: str) -> None:
    users = _load()
    if username not in users:
        print(f"⚠  Usuario '{username}' no existe.")
        return
    del users[username]
    _save(users)
    print(f"🗑  Usuario '{username}' eliminado.")


def cmd_list() -> None:
    users = _load()
    if not users:
        print("Sin usuarios registrados.")
        return
    for u in users:
        print(f"  • {u}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "add" and len(args) == 3:
        cmd_add(args[1], args[2])
    elif cmd == "remove" and len(args) == 2:
        cmd_remove(args[1])
    elif cmd == "list":
        cmd_list()
    else:
        print(__doc__)
