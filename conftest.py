"""Корень репозитория на sys.path: тесты и CLI импортируют пакеты `tools`/`agents` (не установлены).

Ядро (`agentarium`) ставится editable-инсталлом; вспомогательные `tools/` и `agents/` — нет,
поэтому их корень добавляем явно, один раз, здесь. Только для тестового прогона.
"""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
