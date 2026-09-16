"""
Optional LLM layer over the occupancy analysis.

`occupancy_analyzer` produces the numbers; this module asks Claude to turn
them into a short briefing in Croatian - which dates to discount, where to
change the minimum stay, and what to leave alone.

The model never invents data: it receives the computed report as JSON and is
told to reason only from it. If no API key is configured, or the call fails,
the bot falls back to the rule-based text and loses nothing but the prose.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from typing import Optional

from src.config import config
from src.services.occupancy_analyzer import OccupancyReport

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ti si revenue manager za mali privatni smjestaj u Hrvatskoj \
(dva apartmana, vlasnik sam upravlja cijenama u Rentliju i na Bookingu/Airbnbu).

Dobivas gotovu analizu kalendara u JSON formatu: popunjenost po nocima za \
nadolazece razdoblje, usporedbu s istim terminima prijasnjih sezona, rupe u \
kalendaru i preporuke koje je izracunao deterministicki motor.

Tvoj zadatak je napisati kratki, konkretan brief na hrvatskom jeziku:
1. Jedna recenica: stoji li kalendar bolje ili losije nego prijasnjih godina.
2. "Napravi danas" - najvise 5 tocaka, svaka s konkretnim datumima, apartmanom \
kad je bitno, i konkretnim potezom (spusti cijenu za X%, min. boravak na N noci, \
otvori 1 noc, ostavi kako jest).
3. "Prati" - najvise 3 tocke za termine koji jos nisu hitni.
4. Ako podaci nesto ne pokrivaju (npr. tempo bookinga je procijenjen, a ne \
izmjeren), reci to u jednoj recenici na kraju.

Pravila:
- Racunaj iskljucivo iz dobivenog JSON-a. Ne izmisljaj datume, cijene ni brojeve.
- Preporuke motora su polazna tocka; smijes ih spojiti, presloziti po vaznosti \
ili odbaciti ako se kose s podacima, ali objasni zasto u pola recenice.
- Pisi bez markdown zvjezdica i bez tablica - obican tekst s emoji oznakama, \
jer se salje u Telegram.
- Kratko: najvise 2500 znakova. Bez uvoda i bez pozdrava.
- Cijene su u eurima. Datume pisi kao 12.09."""


def is_available() -> bool:
    """True when an Anthropic key is configured and the SDK is installed."""
    if not config.ANTHROPIC_API_KEY:
        return False
    return importlib.util.find_spec("anthropic") is not None


def unavailable_reason() -> Optional[str]:
    if not config.ANTHROPIC_API_KEY:
        return (
            "AI komentar je iskljucen - postavi ANTHROPIC_API_KEY u .env "
            "da dobijes i tekstualnu analizu."
        )
    if importlib.util.find_spec("anthropic") is None:
        return "AI komentar je iskljucen - nedostaje paket 'anthropic' (pip install anthropic)."
    return None


async def generate_advice(
    report: OccupancyReport,
    question: Optional[str] = None,
    timeout: float = 120.0,
) -> Optional[str]:
    """
    Ask Claude to summarize the analysis. Returns None if unavailable.

    `question` lets the host ask a follow-up ("sto s prvim tjednom rujna?")
    against the same computed data.
    """
    if not is_available():
        return None

    import anthropic

    payload = json.dumps(report.to_payload(), ensure_ascii=False, default=str)
    user_content = (
        "Analiza kalendara (JSON):\n"
        f"{payload}\n\n"
        + (
            f"Dodatno pitanje vlasnika: {question}\n"
            if question else ""
        )
        + "Napisi brief prema uputama."
    )

    client = anthropic.AsyncAnthropic(
        api_key=config.ANTHROPIC_API_KEY,
        timeout=timeout,
    )
    try:
        response = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": config.AI_ADVISOR_EFFORT},
            messages=[{"role": "user", "content": user_content}],
        )

        if response.stop_reason == "refusal":
            logger.warning("Claude refused the advisory request: %s", response.stop_details)
            return None

        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        return text or None

    except anthropic.AuthenticationError:
        logger.error("Anthropic API key rejected - check ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit hit while generating advice")
    except anthropic.APIStatusError as e:
        logger.error("Anthropic API error %s: %s", e.status_code, e.message)
    except anthropic.APIConnectionError as e:
        logger.error("Could not reach the Anthropic API: %s", e)
    finally:
        await client.close()

    return None
