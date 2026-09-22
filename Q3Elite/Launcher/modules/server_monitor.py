import json
import socket
import time
from pathlib import Path

def split_address(address):
    value = str(address or "").strip()
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host.strip(), int(port)
    return value, 27960

def query_server(address, timeout=1.25):
    host, port = split_address(address)
    result = {
        "address": f"{host}:{port}", "online": False, "ping": None,
        "hostname": "", "mapname": "", "clients": 0, "maxclients": 0,
        "gametype": "", "players": [], "error": ""
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    started = time.perf_counter()
    try:
        sock.sendto(b"\xff\xff\xff\xffgetstatus\n", (host, port))
        data, _ = sock.recvfrom(65535)
        elapsed = (time.perf_counter() - started) * 1000.0
        result["ping"] = max(1, int(round(elapsed)))
        text = data.decode("latin-1", errors="replace")
        if text.startswith("\xff\xff\xff\xff"):
            text = text[4:]
        lines = text.replace("\r", "").split("\n")
        if lines and lines[0].lower().startswith("statusresponse"):
            lines = lines[1:]
        info = lines[0] if lines else ""
        parts = info.split("\\")
        values = {}
        for i in range(1, len(parts) - 1, 2):
            values[parts[i]] = parts[i + 1]
        result["hostname"] = values.get("sv_hostname", values.get("hostname", ""))
        result["mapname"] = values.get("mapname", "")
        result["maxclients"] = int(values.get("sv_maxclients", values.get("maxclients", 0)) or 0)
        result["gametype"] = values.get("g_gametype", "")
        players = []
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            # Quake 3 status: score ping "name"
            try:
                score_s, ping_s, rest = line.split(" ", 2)
                name = rest.strip()
                if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
                    name = name[1:-1]
                players.append({"name": name, "score": int(score_s), "ping": int(ping_s)})
            except Exception:
                continue
        result["players"] = players
        result["clients"] = len(players)
        result["online"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        sock.close()
    return result

def load_servers(path):
    path = Path(path)
    custom = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                custom = [x for x in raw if isinstance(x, dict) and x.get("address")]
        except Exception:
            custom = []
    seen = set()
    result = []
    for item in custom:
        addr = str(item.get("address", "")).strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        result.append(dict(item))
    return result

def save_custom_servers(path, servers):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    output = []
    for item in servers:
        address = str(item.get("address", "")).strip()
        if not address:
            continue
        output.append({
            "name": str(item.get("name", "")).strip(),
            "address": address,
        })
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
