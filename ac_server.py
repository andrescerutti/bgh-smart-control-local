#!/usr/bin/env python3
"""Panel web local para controlar un aire acondicionado (switch BGH Smart
Control Kit HPA-4911), sin nube. Sirve para cualquier kit de este modelo:
la primera vez, el panel incluye un asistente para configurarlo (WiFi +
aprender el control remoto).

Uso:
    python3 ac_server.py
    # Abrir http://localhost:8765 en el navegador

Seguridad: este servidor escucha en toda la red local (0.0.0.0) a proposito,
para poder usarlo desde el celular. Eso significa que CUALQUIERA en tu WiFi
puede prender/apagar el aire o reconfigurarlo — no hay usuario ni contraseña.
Para una casa esto es un riesgo bajo (el peor caso es que alguien en tu
propia red te prenda el aire), pero el servidor igual valida el header Host
para evitar que una pagina externa abierta en tu navegador controle el
equipo sin que lo notes (DNS rebinding / CSRF basico).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ac_control  # noqa: E402

PORT = 8765
WEB_DIR = Path(__file__).parent / "ac_web"
MAC_INPUT_RE = re.compile(r"^[0-9a-fA-F:]{12,17}$")


def _is_trusted_host(host_header: str | None) -> bool:
    """True si el header Host es localhost o una IP privada (LAN), con o
    sin puerto. Bloquea DNS rebinding: una pagina de internet no puede
    hacerse pasar por este servidor aunque resuelva a 127.0.0.1, porque el
    navegador sigue mandando el Host original (el dominio externo)."""
    if not host_header:
        return False
    hostname = host_header.rsplit(":", 1)[0]
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_private
    except ValueError:
        return False


def _origin_matches_host(headers) -> bool:
    """True si no hay header Origin (pedido hecho a mano, no desde un
    navegador), o si el Origin coincide con el Host. Esto es lo que
    realmente frena CSRF: el Host por si solo no alcanza, porque una
    pagina maliciosa en tu misma red puede mandar un pedido con un Host
    valido (la IP de tu Mac) — lo que no puede falsificar es el Origin,
    que pone el navegador y refleja el sitio real que hizo el pedido."""
    origin = headers.get("Origin")
    if origin is None:
        return True
    origin_host = origin.split("://", 1)[-1]
    return origin_host == headers.get("Host")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _valid_mac_or_error(self, raw: str) -> str | None:
        """Normaliza y valida un MAC recibido del cliente. Devuelve el MAC
        en minusculas sin separadores, o None (y ya mando la respuesta de
        error) si es invalido."""
        if not MAC_INPUT_RE.match(raw or ""):
            self._send_json(400, {"ok": False, "error": "MAC invalida"})
            return None
        mac = raw.replace(":", "").lower()
        if not ac_control.valid_mac_hex(mac):
            self._send_json(400, {"ok": False, "error": "MAC invalida"})
            return None
        return mac

    def do_GET(self) -> None:  # noqa: N802
        if not _is_trusted_host(self.headers.get("Host")) or not _origin_matches_host(self.headers):
            self.send_error(403, "Host u Origin no permitido")
            return
        path, _, query = self.path.partition("?")
        if path.startswith("/api/") and self.headers.get("X-Requested-With") != "fetch":
            # Un <img>, un <a> o una navegacion simple no puede poner este
            # header — solo JS con fetch() puede, y si lo hace cross-origin
            # dispara un preflight CORS que este servidor nunca aprueba.
            # Esto es lo que de verdad frena un GET disparado desde una
            # pagina maliciosa (el Origin solo no alcanza para GET).
            self.send_error(403, "Falta header requerido")
            return
        routes = {
            "/": lambda: self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8"),
            "/index.html": lambda: self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8"),
            "/api/status": self._handle_status,
            "/api/config": self._handle_get_config,
            "/api/wifi/discover": self._handle_wifi_discover,
            "/api/setup/discover-lan": self._handle_discover_lan,
            "/api/setup/learn-listen": lambda: self._handle_learn_listen(query),
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if not _is_trusted_host(self.headers.get("Host")) or not _origin_matches_host(self.headers):
            self.send_error(403, "Host u Origin no permitido")
            return
        if self.headers.get("Content-Length") and "application/json" not in (self.headers.get("Content-Type") or ""):
            self.send_error(415, "Se espera Content-Type: application/json")
            return
        body = self._read_json_body()
        routes = {
            "/api/on": self._handle_on,
            "/api/off": self._handle_off,
            "/api/wifi/join": self._handle_wifi_join,
            "/api/setup/use-mac": self._handle_use_mac,
            "/api/setup/learn-start": self._handle_learn_start,
            "/api/setup/learn-stop": self._handle_learn_stop,
            "/api/setup/learn-beep": self._handle_learn_beep,
            "/api/setup/learn-save": self._handle_learn_save,
        }
        handler = routes.get(self.path)
        if handler:
            handler(body)
        else:
            self.send_error(404)

    # --- Config / estado ---------------------------------------------------

    def _handle_get_config(self) -> None:
        self._send_json(200, ac_control.load_config())

    def _handle_status(self) -> None:
        config = ac_control.load_config()
        if not config.get("mac"):
            self._send_json(200, {"configured": False})
            return
        try:
            data = ac_control.send_custom([92, 30])
        except SystemExit:
            data = None
        if data is None:
            self._send_json(200, {"configured": True, "online": False})
            return
        payload = data[17:]
        mac_ir = payload[-6:]
        self._send_json(200, {
            "configured": True,
            "online": True,
            "rssi": payload[1] if len(payload) > 1 else None,
            "ir_linked": mac_ir != b"\x00" * 6,
        })

    # --- Control -------------------------------------------------------------

    def _handle_on(self, body: dict) -> None:
        mode = ac_control.MODES.get(str(body.get("mode", "frio")).lower())
        fan = ac_control.FANS.get(str(body.get("fan", "medio")).lower())
        temp = float(body.get("temp", 24))
        if mode is None or fan is None:
            self._send_json(400, {"ok": False, "error": "modo o ventilador invalido"})
            return
        try:
            ok = ac_control.send_hvac(mode, fan, 0, int(round(temp * 100)))
        except SystemExit:
            ok = None
        self._send_json(200, {"ok": bool(ok)})

    def _handle_off(self, _body: dict) -> None:
        try:
            ok = ac_control.send_hvac(0, 255, 0, 0)
        except SystemExit:
            ok = None
        self._send_json(200, {"ok": bool(ok)})

    # --- WiFi provisioning -----------------------------------------------------

    def _handle_wifi_discover(self) -> None:
        mac = ac_control.discover_provisioning_device(timeout=20.0)
        self._send_json(200, {"found": bool(mac), "mac": mac})

    def _handle_wifi_join(self, body: dict) -> None:
        mac = self._valid_mac_or_error(str(body.get("mac", "")))
        if mac is None:
            return
        ssid = str(body.get("ssid", ""))
        password = str(body.get("password", ""))
        if not ssid:
            self._send_json(400, {"ok": False, "error": "falta ssid"})
            return
        ok = ac_control.provision_wifi(mac, ssid, password)
        self._send_json(200, {"ok": ok})

    # --- Asistente de primera configuracion -------------------------------

    def _handle_discover_lan(self) -> None:
        """Busca equipos ya conectados a esta red (para el caso de un kit
        que ya tiene WiFi pero nunca se configuro el control remoto)."""
        macs = ac_control.listen_for_any_device(timeout=6.0)
        self._send_json(200, {"found": macs})

    def _handle_use_mac(self, body: dict) -> None:
        mac = self._valid_mac_or_error(str(body.get("mac", "")))
        if mac is None:
            return
        ac_control.save_config(mac=mac, last_ip=None)
        self._send_json(200, {"ok": True})

    def _handle_learn_start(self, body: dict) -> None:
        mac = self._valid_mac_or_error(str(body.get("mac", "")))
        if mac is None:
            return
        try:
            ok = ac_control.learn_set_mode(mac, enable=True)
        except RuntimeError as err:
            self._send_json(200, {"ok": False, "error": str(err)})
            return
        self._send_json(200, {"ok": ok})

    def _handle_learn_listen(self, query: str) -> None:
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        raw_mac = params.get("mac", "")
        if not MAC_INPUT_RE.match(raw_mac):
            self._send_json(400, {"codec": None, "error": "MAC invalida"})
            return
        mac = raw_mac.replace(":", "").lower()
        if not ac_control.valid_mac_hex(mac):
            self._send_json(400, {"codec": None, "error": "MAC invalida"})
            return
        codec = ac_control.learn_listen_codec(mac, timeout=15.0)
        self._send_json(200, {"codec": codec})

    def _handle_learn_beep(self, body: dict) -> None:
        mac = self._valid_mac_or_error(str(body.get("mac", "")))
        if mac is None:
            return
        try:
            ok = ac_control.learn_send_test_beep(mac)
        except RuntimeError as err:
            self._send_json(200, {"ok": False, "error": str(err)})
            return
        self._send_json(200, {"ok": ok})

    def _handle_learn_save(self, body: dict) -> None:
        mac = self._valid_mac_or_error(str(body.get("mac", "")))
        if mac is None:
            return
        try:
            codec = int(body.get("codec"))
            sensor = int(body.get("sensor"))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "codec/sensor invalido"})
            return
        try:
            ok_set = ac_control.learn_set_codec(mac, codec, sensor)
            ok_cal = ac_control.learn_calibrate_and_close(mac)
            ok_exit = ac_control.learn_set_mode(mac, enable=False)
        except RuntimeError as err:
            self._send_json(200, {"ok": False, "error": str(err)})
            return
        if ok_set and ok_cal:
            ac_control.save_config(mac=mac, codec=codec, sensor=sensor)
        self._send_json(200, {"ok": bool(ok_set and ok_cal), "exited_learn_mode": ok_exit})

    def _handle_learn_stop(self, body: dict) -> None:
        mac = self._valid_mac_or_error(str(body.get("mac", "")))
        if mac is None:
            return
        try:
            ok = ac_control.learn_set_mode(mac, enable=False)
        except RuntimeError as err:
            self._send_json(200, {"ok": False, "error": str(err)})
            return
        self._send_json(200, {"ok": ok})


def _lan_ip_hint() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "<IP de esta Mac>"
    finally:
        s.close()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Panel disponible en http://localhost:{PORT}")
    print(f"Desde el celular (misma red WiFi): http://{_lan_ip_hint()}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
