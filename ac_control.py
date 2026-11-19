#!/usr/bin/env python3
"""Control local de un aire acondicionado via BGH Smart Control Kit (HPA-4911).

No depende de la nube de BGH (dada de baja). Habla directo por UDP con el
switch/base en la red local, usando el protocolo documentado en
https://github.com/firtman/bgh_smart/pull/15

Este modulo sirve para CUALQUIER kit HPA-4911, no uno en particular: la
identidad del equipo (MAC, codec IR, sensor) se guarda en `ac_config.json`
junto a este archivo, generada la primera vez con el panel web (ac_server.py)
o con `python3 ac_control.py setup`.

Uso por terminal (una vez configurado):
    python3 ac_control.py on --mode frio --temp 24 --fan medio
    python3 ac_control.py off
    python3 ac_control.py status
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SRC_MAC = b"\x00" * 6
UDP_CMD_PORT = 20910
UDP_BROADCAST_PORT = 20911
PROVISION_IP = "10.10.100.254"  # IP del switch mientras esta en modo vincular

CONFIG_FILE = Path(__file__).with_name("ac_config.json")

MODES = {
    "off": 0, "apagado": 0,
    "frio": 1, "cool": 1, "frío": 1,
    "calor": 2, "heat": 2,
    "seco": 3, "dry": 3,
    "ventilacion": 4, "fan": 4, "ventilación": 4,
    "auto": 254,
}

FANS = {
    "bajo": 1, "low": 1,
    "medio": 2, "mid": 2, "med": 2,
    "alto": 3, "high": 3,
}

# No-toggle segun la app oficial: estos codecs mandan ON/OFF directo, no
# alternan. El resto se asume toggle (necesita saber el estado real).
NON_TOGGLE_CODECS = {1, 10, 12, 13, 20, 30, 50, 51, 71, 80, 90, 100, 111, 130, 140, 150}

_seq = [1]
_scan_lock = __import__("threading").Lock()
_scan_cache: dict[str, tuple[float, str | None]] = {}
SCAN_COOLDOWN_SECONDS = 5.0
MAX_CONCURRENT_PINGS = 32


def valid_mac_hex(mac: str) -> bool:
    """True si `mac` son 12 caracteres hex (sin separadores)."""
    return bool(re.fullmatch(r"[0-9a-f]{12}", mac))


def _next_seq() -> int:
    _seq[0] = (_seq[0] + 1) % 256
    return _seq[0]


# --- Configuracion persistida (identidad del equipo) -----------------------

def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(**updates: Any) -> dict[str, Any]:
    config = load_config()
    config.update({k: v for k, v in updates.items() if v is not None})
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    return config


def require_mac() -> bytes:
    config = load_config()
    mac_hex = config.get("mac")
    if not mac_hex:
        print(
            "Este equipo todavia no esta configurado. Corre el panel web "
            "(ac_server.py) y segui el asistente de primera configuracion.",
            file=sys.stderr,
        )
        sys.exit(1)
    return bytes.fromhex(mac_hex)


# --- Descubrimiento en la red local -----------------------------------------

def _probe(ip: str, device_mac: bytes, timeout: float = 1.5) -> bool:
    """Confirma que el equipo en `ip` es el que buscamos (MAC de origen en
    la respuesta coincide)."""
    seq = _next_seq()
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, seq, 0x00, 0x00, 162)
    packet = header + bytes([92, 30])
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (ip, UDP_CMD_PORT))
        data, _ = s.recvfrom(2048)
        return len(data) >= 7 and data[1:7] == device_mac
    except OSError:
        return False
    finally:
        s.close()


def _local_subnet_prefix() -> str | None:
    """Prefijo /24 de la red local (ej. '192.168.18'), sin depender de
    ifconfig: abre un socket UDP hacia afuera y mira que IP uso el sistema
    para esa ruta (no se manda ningun dato real a esa IP externa)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ".".join(local_ip.split(".")[:3])


def listen_for_any_device(timeout: float = 6.0) -> list[str]:
    """Escucha broadcasts (status report, comando 253) de CUALQUIER kit
    HPA-4911 ya conectado a esta red. Devuelve las MAC (hex) que se vean."""
    found: set[str] = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    deadline = time.monotonic() + timeout
    try:
        s.bind(("0.0.0.0", UDP_BROADCAST_PORT))
        while time.monotonic() < deadline:
            s.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                break
            if len(data) >= 17 and data[0] == 0x00 and data[16] in (0xFD, 0xF5):
                found.add(data[1:7].hex())
    except OSError:
        pass
    finally:
        s.close()
    return sorted(found)


def _ping_sweep(prefix: str) -> None:
    """Hace ping a toda la subred /24, como maximo MAX_CONCURRENT_PINGS a
    la vez (evita disparar 254 procesos de golpe)."""
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    for start in range(0, len(hosts), MAX_CONCURRENT_PINGS):
        chunk = hosts[start:start + MAX_CONCURRENT_PINGS]
        procs = [
            subprocess.Popen(
                ["ping", "-c", "1", "-W", "200", "-t", "1", host],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for host in chunk
        ]
        for p in procs:
            p.wait()


def _scan_for_device(device_mac: bytes) -> str | None:
    """Barre la subred local con ping y busca en la tabla ARP del sistema
    la IP correspondiente a `device_mac`. Si ya se escaneo hace poco (por
    este u otro pedido en paralelo), devuelve el resultado en cache en vez
    de volver a barrer toda la red."""
    mac_hex = device_mac.hex()
    with _scan_lock:
        cached = _scan_cache.get(mac_hex)
        if cached and (time.monotonic() - cached[0]) < SCAN_COOLDOWN_SECONDS:
            return cached[1]

        result = _do_scan_for_device(device_mac)
        _scan_cache[mac_hex] = (time.monotonic(), result)
        return result


def _do_scan_for_device(device_mac: bytes) -> str | None:
    prefix = _local_subnet_prefix()
    if prefix is None:
        return None
    _ping_sweep(prefix)

    try:
        arp_out = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None

    mac_str = ":".join(device_mac.hex()[i:i + 2] for i in range(0, 12, 2))
    mac_pattern = re.escape(mac_str).replace(r"\:", r"[:.]?")
    match = re.search(
        r"\((\d+\.\d+\.\d+\.\d+)\) at " + mac_pattern, arp_out, re.IGNORECASE
    )
    if match:
        candidate = match.group(1)
        if _probe(candidate, device_mac):
            return candidate
    return None


def resolve_device_ip() -> str:
    """Devuelve una IP donde el equipo configurado responde. Prueba la
    ultima conocida primero; si no responde, escanea la red."""
    device_mac = require_mac()
    config = load_config()
    cached = config.get("last_ip")
    if cached and _probe(cached, device_mac):
        return cached

    found = _scan_for_device(device_mac)
    if found:
        save_config(last_ip=found)
        return found

    print(
        "No encontre el equipo en la red. Revisa que este enchufado y "
        "conectado al WiFi.",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Comandos de control (equipo ya configurado) ----------------------------

def send_hvac(mode: int, fan: int, flags: int, temp_x100: int, timeout: float = 5.0) -> bool | None:
    """Envia un comando HVAC (SetMode, endpoint 1, comando 97). Devuelve
    True si hubo ACK, False si respondio sin confirmar, None si no respondio."""
    device_mac = require_mac()
    ip = resolve_device_ip()
    seq = _next_seq()
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, seq, 0x00, 0x01, 97)
    packet = header + struct.pack("<BBBH", mode, fan, flags, temp_x100)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (ip, UDP_CMD_PORT))
        data, _ = s.recvfrom(2048)
        return data[16] == 0x80
    except socket.timeout:
        return None
    finally:
        s.close()


def send_custom(payload_bytes: list[int], timeout: float = 5.0) -> bytes | None:
    """Envia un comando 'custom' (162) y devuelve la respuesta cruda, o
    None si no hubo respuesta."""
    device_mac = require_mac()
    ip = resolve_device_ip()
    seq = _next_seq()
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, seq, 0x00, 0x00, 162)
    packet = header + bytes(payload_bytes)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (ip, UDP_CMD_PORT))
        data, _ = s.recvfrom(2048)
        return data
    except socket.timeout:
        return None
    finally:
        s.close()


# --- Provisioning de WiFi (equipo en modo vincular, hotspot propio) --------

def discover_provisioning_device(timeout: float = 20.0) -> str | None:
    """Escucha el broadcast del hotspot BGH-... y devuelve la MAC (hex)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        s.bind(("0.0.0.0", UDP_BROADCAST_PORT))
        data, _ = s.recvfrom(1024)
    except OSError:
        return None
    finally:
        s.close()
    if len(data) < 7:
        return None
    return data[1:7].hex()


def provision_wifi(device_mac_hex: str, ssid: str, password: str, timeout: float = 5.0) -> bool:
    """Manda las credenciales de WiFi al equipo en modo vincular. True si
    confirmo recibirlas (no garantiza que logre unirse: depende de que la
    red sea 2.4GHz y la clave sea correcta)."""
    device_mac = bytes.fromhex(device_mac_hex.replace(":", ""))
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, _next_seq(), 0x00, 0x00, 0xA1)
    ssid_bytes = ssid.encode("utf-8")[:33].ljust(33, b"\x00")
    password_bytes = password.encode("utf-8")[:33].ljust(33, b"\x00")
    payload = struct.pack("B33sBB33s", 0x03, ssid_bytes, 0x03, 0x04, password_bytes)
    packet = header + payload

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (PROVISION_IP, UDP_CMD_PORT))
        data, _ = s.recvfrom(1024)
        return len(data) >= 17 and data[16] == 0x80
    except socket.timeout:
        return False
    finally:
        s.close()


# --- Aprendizaje del control remoto (codec IR) ------------------------------
# Secuencia documentada en https://github.com/firtman/bgh_smart/pull/15

def learn_set_mode(device_mac_hex: str, enable: bool, timeout: float = 5.0) -> bool:
    device_mac = bytes.fromhex(device_mac_hex.replace(":", ""))
    ip = _resolve_for_mac(device_mac)
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, _next_seq(), 0x00, 0x00, 162)
    packet = header + bytes([90, 1 if enable else 0])
    return _send_and_ack(packet, ip, timeout)


def learn_listen_codec(device_mac_hex: str, timeout: float = 15.0) -> int | None:
    """Escucha el broadcast de codec detectado (subcomando 95 dentro del
    comando custom 162) mientras el usuario aprieta ENCENDIDO en su control."""
    device_mac = bytes.fromhex(device_mac_hex.replace(":", ""))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    deadline = time.monotonic() + timeout
    try:
        s.bind(("0.0.0.0", UDP_BROADCAST_PORT))
        while time.monotonic() < deadline:
            s.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                return None
            if (
                len(data) >= 19
                and data[1:7] == device_mac
                and data[16] == 162
                and data[17] == 95
            ):
                return data[18]
    except OSError:
        return None
    finally:
        s.close()
    return None


def learn_send_test_beep(device_mac_hex: str, timeout: float = 4.0) -> bool:
    device_mac = bytes.fromhex(device_mac_hex.replace(":", ""))
    ip = _resolve_for_mac(device_mac)
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, _next_seq(), 0x00, 0x01, 97)
    packet = header + struct.pack("<BBBH", 0, 255, 255, 0x8000)
    return _send_and_ack(packet, ip, timeout)


def learn_set_codec(device_mac_hex: str, codec: int, sensor: int, timeout: float = 5.0) -> bool:
    device_mac = bytes.fromhex(device_mac_hex.replace(":", ""))
    ip = _resolve_for_mac(device_mac)
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, _next_seq(), 0x00, 0x00, 162)
    packet = header + bytes([88, codec, sensor])
    return _send_and_ack(packet, ip, timeout)


def learn_calibrate_and_close(device_mac_hex: str, timeout: float = 5.0) -> bool:
    """Comando 100: calibra el cero de vibracion (si aplica) y guarda. Se
    manda siempre al cerrar el asistente, use el sensor que use."""
    device_mac = bytes.fromhex(device_mac_hex.replace(":", ""))
    ip = _resolve_for_mac(device_mac)
    header = struct.pack("B6s6sBBBB", 0x00, SRC_MAC, device_mac, _next_seq(), 0x00, 0x00, 162)
    packet = header + bytes([100])
    return _send_and_ack(packet, ip, timeout)


def _resolve_for_mac(device_mac: bytes) -> str:
    """Como resolve_device_ip(), pero para un MAC que todavia puede no estar
    guardado en la config (durante el asistente de primera vez)."""
    config = load_config()
    cached = config.get("last_ip") if bytes.fromhex(config.get("mac", "")) == device_mac else None
    if cached and _probe(cached, device_mac):
        return cached
    found = _scan_for_device(device_mac)
    if found:
        return found
    raise RuntimeError("No encontre el equipo en la red")


def _send_and_ack(packet: bytes, ip: str, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (ip, UDP_CMD_PORT))
        data, _ = s.recvfrom(2048)
        return len(data) >= 17 and data[16] == 0x80
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


# --- CLI ---------------------------------------------------------------

def cmd_on(mode_name: str, temp: float, fan_name: str) -> None:
    mode = MODES.get(mode_name.lower())
    if mode is None:
        print(f"Modo desconocido: {mode_name}. Opciones: {sorted(set(MODES))}")
        sys.exit(1)
    fan = FANS.get(fan_name.lower())
    if fan is None:
        print(f"Velocidad desconocida: {fan_name}. Opciones: {sorted(set(FANS))}")
        sys.exit(1)
    temp_x100 = int(round(temp * 100))
    ok = send_hvac(mode, fan, 0, temp_x100)
    if ok:
        print(f"OK: enviado {mode_name} {temp}C, ventilador {fan_name}")
    elif ok is None:
        print("El equipo no respondio (timeout).")
    else:
        print("El equipo respondio pero no confirmo (no ACK). Reintenta.")


def cmd_off() -> None:
    ok = send_hvac(0, 255, 0, 0)
    if ok:
        print("OK: enviado apagar")
    elif ok is None:
        print("El equipo no respondio (timeout).")
    else:
        print("El equipo respondio pero no confirmo (no ACK).")


def cmd_status() -> None:
    data = send_custom([92, 30])
    if data is None:
        print("Sin respuesta del equipo.")
        return
    payload = data[17:]
    rssi = payload[1] if len(payload) > 1 else None
    mac_ir = payload[-6:]
    print(f"Equipo MAC: {require_mac().hex()}  (IP actual: {resolve_device_ip()})")
    print(f"RSSI unidad IR: {rssi}")
    print("Unidad IR: SIN VINCULAR" if mac_ir == b"\x00" * 6 else f"Unidad IR: vinculada ({mac_ir.hex()})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Control local de un aire via BGH Smart Control Kit")
    sub = parser.add_subparsers(dest="action", required=True)

    p_on = sub.add_parser("on", help="Prender el aire")
    p_on.add_argument("--mode", default="frio")
    p_on.add_argument("--temp", type=float, default=24.0)
    p_on.add_argument("--fan", default="medio")

    sub.add_parser("off", help="Apagar el aire")
    sub.add_parser("status", help="Ver estado")

    args = parser.parse_args()
    if args.action == "on":
        cmd_on(args.mode, args.temp, args.fan)
    elif args.action == "off":
        cmd_off()
    elif args.action == "status":
        cmd_status()


if __name__ == "__main__":
    main()
