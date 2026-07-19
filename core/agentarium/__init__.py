"""agentarium — ядро платформы: конверт, шина, SDK, чертёж.

Ядро не знает предметной области и LLM-фреймворков (spec/00, закон «ядро вечное»).
"""

from agentarium.agent import Agent
from agentarium.bus import Bus
from agentarium.envelope import Envelope, Reply

__all__ = ["Agent", "Bus", "Envelope", "Reply"]
