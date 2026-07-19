"""Шлюз agentarium: FastAPI HTTP-контракт (spec/40) + консюмеры финалов/dlq (spec/40, 50).

Универсальный вход/выход системы: предметных имён типов не содержит — entry и finals из чертежа.
Не часть wheel ядра: отдельный build-контекст образа, бандл contract-модулей — забота Dockerfile.
"""

from .app import build, make_app
from .consumers import GatewayConsumers, extract_text
from .secrets import SecretsError, check_secrets

__all__ = [
    "GatewayConsumers",
    "SecretsError",
    "build",
    "check_secrets",
    "extract_text",
    "make_app",
]
