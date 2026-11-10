"""Local reply safeguards; no model calls or shared per-request state."""

import re


LIMIT_MESSAGE = "Сейчас я не могу обратиться к модели: доступный лимит исчерпан. Попробуй позже."
HUMOR_GUIDANCE = (
    "\nЮмор: сначала пойми текущую ситуацию и конкретную деталь, потом добавь один короткий неожиданный поворот. "
    "Не натягивай каламбур на перевод или созвучие названия турнира, игры или имени; не одушевляй название вместо понимания события. "
    "Не заканчивай каждую шутку ресурс, едой или технический контекст, если это не связано с темой. "
    "Избегай канцелярита, пересказа сообщения и объяснения собственной шутки. "
    "Ролевая манера допустима, но смысл должен оставаться про текущую реплику. "
    "Если смешного наблюдения нет, ответь естественно по существу; не выжимай шутку из каждого сообщения."
)
NO_SOLICITATION = (
    "\nНе выпрашивай ресурс, подарки, деньги или донаты, даже в шутку и при обиде. "
    "Не связывай ответы, доступ к модели или прощение с подношениями. "
    "ресурс допустим как часть образа, а не как просьба что-то принести. "
    "Способы поддержки или примирения объясняй только по прямому вопросу, спокойно и без давления."
)
_DEMAND = re.compile(
    r"\b(?:дай(?:те)?|давай(?:те)?|дайте|верни(?:те)?|принеси(?:те)?|принесите|пришли|пришлите|"
    r"подари(?:те)?|подарите|подкинь|подкиньте|скинь|скиньте|закинь|закиньте|"
    r"накорми(?:те)?|угости(?:те)?|угостите|пожертвуй|пожертвуйте|задонать|задонатьте|"
    r"погаси(?:те)?|искупи(?:те)?|заплати(?:те)?|пополни(?:те)?|требую)\b.{0,75}"
    r"(?:ресурс|донат|денег|деньги|подношен|подарк)|"
    r"(?:без\s+(?:ресурс\w*|донат\w*|подношен\w*)).{0,65}(?:не\s+(?:отвеч|прощ)|ответа\s+не)|"
    r"\b(?:донать|донатьте|донатни|задонать|задонатьте)\b|"
    r"ресурс\w*.{0,40}(?:принес[её]шь|пришл[её]шь|подкинешь|подаришь)|"
    r"\b(?:give|send|bring|donate|buy)\b.{0,50}\b(?:hidden_c|donations?|money|gifts?)\b",
    re.I,
)


def without_solicitation(answer, fallback=""):
    """Drop demanding sentences, retaining the useful part and ordinary hidden_c context."""
    if not answer or not _DEMAND.search(str(answer)):
        return answer
    parts = re.split(r"(?<=[.!?])\s+|\n+", str(answer))
    return "\n".join(part for part in parts if part.strip() and not _DEMAND.search(part)).strip() or fallback


def neutral_limit_message(value):
    # A service outage is not a roleplay debt; never keep a demand from an old template.
    text = str(value or "").strip()
    return LIMIT_MESSAGE if not text or _DEMAND.search(text) else text


class UnavailableReply(str):
    """Keep outage replies separate from reputation tone through the reply pipeline."""


class StandaloneReply(str):
    """Deliver a notification without attaching it as a Telegram reply."""


class GrudgeReply(str):
    """Keep the local ledger notice separate from the speakable answer."""
    def __new__(cls, notice, body="", platform="telegram"):
        gap = "  |  " if platform == "twitch" else "\n\n"
        result = super().__new__(cls, str(notice) + (gap + str(body) if body else ""))
        result.notice, result.body, result.platform = str(notice), body, platform
        return result


class TransitionReply(str):
    """A local threshold notice, optionally joined to the already selected answer."""
    def __new__(cls, notice, body="", platform="telegram", claimed=False, message_keys=()):
        gap = " · " if platform == "twitch" else "\n\n"
        result = super().__new__(cls, str(notice) + (gap + str(body) if body else ""))
        result.body, result.claimed = body, claimed
        result.message_keys = tuple(message_keys)
        return result


def quota_reply(store):
    return UnavailableReply(neutral_limit_message(store.setting("ai_limit_message", LIMIT_MESSAGE)))
