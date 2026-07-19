"""agentarium — ядро платформы: конверт, шина, SDK, чертёж.

Ядро не знает предметной области и LLM-фреймворков (spec/00, закон «ядро вечное»).
"""

from agentarium.envelope import Envelope, Reply

__all__ = ["Envelope", "Reply"]
