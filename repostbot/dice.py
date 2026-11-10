"""Small, deterministic-enough D&D dice parser used by the Telegram command."""

from dataclasses import dataclass
import re
import secrets


@dataclass(frozen=True)
class RollSpec:
    count: int = 1
    sides: int = 6
    modifier: int = 0
    target: int | None = None
    advantage: str = ""


@dataclass(frozen=True)
class RollResult:
    spec: RollSpec
    values: tuple[int, ...]
    total: int
    natural: int | None
    outcome: str


_DICE = re.compile(r"^(?:(\d{1,2})\s*)?d\s*(\d{1,4})(?:\s*([+-])\s*(\d{1,4}))?", re.I)
_TARGET = re.compile(r"(?:\b(?:dc|vs|против|сложность|цель)\s*[:=]?\s*)(\d{1,4})", re.I)


def parse_roll(text):
    """Parse ``NdM±K`` with optional DC/advantage words.

    The command prefix and a trailing explanation are accepted.  A trailing
    explanation is intentionally preserved by callers as the scene prompt.
    """
    raw = re.sub(r"^\s*/roll(?:@[A-Za-z0-9_]+)?\s*", "", str(text or ""), flags=re.I)
    match = _DICE.match(raw) or re.match(r"^\s*(\d{1,2})?\s*d\s*(\d{1,4})", raw, re.I)
    if not match:
        # A bare command means Telegram's native d6; everything
        # after it is a scene request (for example, “пусть определит судьбу”).
        return RollSpec(), raw.strip()
    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    sign, amount = match.group(3), match.group(4)
    modifier = (1 if sign == "+" else -1) * int(amount) if sign and amount else 0
    count = max(1, min(20, count))
    sides = max(2, min(1000, sides))
    modifier = max(-1000, min(1000, modifier))
    lowered = raw.casefold()
    advantage = "disadvantage" if re.search(r"(?:disadvantage|помех|с помехой)", lowered) else "advantage" if re.search(r"(?:advantage|преимущ|с преимуществом)", lowered) else ""
    target_match = _TARGET.search(raw)
    target = max(0, min(1000, int(target_match.group(1)))) if target_match else None
    # Text after the dice expression is the scene/context request, not part
    # of the grammar.  Remove only recognized options and keep the rest.
    context = raw[match.end():]
    context = _TARGET.sub("", context)
    context = re.sub(r"\b(?:dc|vs|против|сложность|цель)\b", "", context, flags=re.I)
    context = re.sub(r"\b(?:advantage|disadvantage|преимущество|преимуществом|помеха|помехой)\b", "", context, flags=re.I)
    context = re.sub(r"[,:;]+", " ", context)
    return RollSpec(count, sides, modifier, target, advantage), " ".join(context.split())


def roll(spec, random=secrets.randbelow):
    values = [random(spec.sides) + 1 for _ in range(spec.count)]
    if spec.advantage and spec.count == 1:
        candidates = values + [random(spec.sides) + 1]
        chosen = max(candidates) if spec.advantage == "advantage" else min(candidates)
        values = candidates
        natural = chosen if spec.sides == 20 else None
        total = chosen + spec.modifier
    else:
        natural = values[0] if spec.count == 1 and spec.sides == 20 else None
        total = sum(values) + spec.modifier
    if natural == 20:
        outcome = "Критический успех"
    elif natural == 1:
        outcome = "Критический провал"
    elif spec.target is not None:
        outcome = "Успех" if total >= spec.target else "Провал"
    else:
        outcome = ""
    return RollResult(spec, tuple(values), total, natural, outcome)


def format_result(result):
    spec = result.spec
    notation = f"{spec.count}d{spec.sides}"
    if spec.modifier:
        notation += ("+" if spec.modifier > 0 else "") + str(spec.modifier)
    values = ", ".join(str(value) for value in result.values)
    if spec.advantage and len(result.values) == 2:
        values += " (" + ("преимущество" if spec.advantage == "advantage" else "помеха") + ")"
    body = f"🎲 {notation}: [{values}]"
    if spec.modifier:
        body += f" {'+' if spec.modifier > 0 else '−'} {abs(spec.modifier)}"
    body += f" = {result.total}"
    if spec.target is not None:
        body += f" против DC {spec.target}"
    if result.outcome:
        body += " — " + result.outcome
    return body


def is_native_d6(result):
    return result.spec.count == 1 and result.spec.sides == 6 and result.spec.modifier == 0 and result.spec.target is None and not result.spec.advantage
