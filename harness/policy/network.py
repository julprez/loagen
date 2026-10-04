"""Política de red para herramientas web (no para el endpoint Ollama)."""
import ipaddress
import socket
from urllib.parse import urlsplit


def validate_url(url: str, allow_private: bool = False) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('URL HTTP(S) inválida o con credenciales')
    if allow_private:
        return
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError('no se pudo resolver el destino web') from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('destino de red privada, local o reservada bloqueado')
