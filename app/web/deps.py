"""Подключение слоёв B1 (репозитории) и B2 (анализ, графики) по контракту.

Модули параллельных слоёв собираются одновременно с веб-слоем, поэтому имена
импортуются строго по docs/contract.md §7.
"""

from __future__ import annotations

from app.config import CHART_EMPTY_TEXT

_EMPTY_CHART = f'<p class="chart-empty">{CHART_EMPTY_TEXT}</p>'

from app.db import repositories  # noqa: E402
import app.analysis.quality_index as quality_index  # noqa: E402
import app.analysis.pareto as pareto  # noqa: E402
import app.analysis.trends as trends  # noqa: E402
import app.analysis.spc as spc  # noqa: E402
import app.analysis.charts as charts  # noqa: E402


# Direct imports per contract - modules must exist
# (names kept as expected by callers)
