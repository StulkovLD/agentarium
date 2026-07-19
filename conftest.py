"""Корень репозитория и `core/` на sys.path: тесты импортируют `tools`/`agents` и пакет `gateway`.

Ядро (`agentarium`) ставится editable-инсталлом; вспомогательные `tools/`, `agents/` — нет (их корень
это репозиторий), а шлюз (`core/gateway`) — отдельный пакет со своим build-контекстом образа (spec/40),
не часть wheel ядра; на хосте он импортируется как `gateway.*` с `core/` на пути. Только для тестов.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()
for _path in (_ROOT, _ROOT / "core"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
