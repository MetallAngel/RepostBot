"""Local topic gate shared by every glitch entry point (no model requests)."""

import re


TOPICS=(('technical',r'программ|настрой|компьютер|сервер|процессор|\bкод\b|docker|python|ошибк|не\s+работает'),('fantasy',r'игр|кубик|бросок|d20'),('narrative',r'истори|архив|летопис'))
COMPATIBLE={'technical':{'technical_style','custom'},'fantasy':{'dnd','custom'},'narrative':{'narrative_style','custom'},'general':{'casual_style','technical_style','narrative_style','dnd','formal_style','warm_style','calm_style','custom'},'serious':set()}
SERIOUS = re.compile(
    r"без\s+(?:шуток|сбоя|сбоев|ролей)|серь[её]зн\w*\s+(?:вопрос|проблем)|"
    r"(?:мне|нам)\s+(?:плохо|страшно)|помоги.{0,30}(?:срочно|реально)|"
    r"\bумер(?:ла|ли|ший|еть)?\b|похорон|соболезн|болит|диагноз|лекарств|скорая|самоубий|"
    r"шизофрен|психоз|паническ|(?:я|мне).{0,20}(?:галлюцинац|слышу.{0,10}голоса)|модерац|забан|удали.{0,20}сообщен|жалоб|оскорбля|травл", re.I)


def topic(text):
    text = str(text or "")
    if SERIOUS.search(text):
        return "serious"
    for name, pattern in TOPICS:
        if re.search(pattern, text, re.I):
            return name
    return "general"


def suitable(style, context):
    # Unknown administrator styles have no reliable genre classification.
    # Do not improvise compatibility for them in a themed/serious conversation.
    return style in COMPATIBLE.get(context, set()) or style == "custom" and context == "general"


def line_suitable(style,text):
    """Configured styles never add a prebuilt fictional setting."""
    return topic(text)!='serious' or style=='custom'
