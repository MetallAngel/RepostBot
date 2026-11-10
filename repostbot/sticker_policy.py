"""Local sticker decisions: weak roleplay is not a permanent moderation ban."""

import re


HEAVY = re.compile(
    r"(?<![а-яёa-z])(?:"
    r"(?:пизд|распизд|запизд|напизд|отпизд|спизд)[а-яё]*|"
    r"(?:(?:за|на|от|пере|до|по|раз)?(?:хуй|хуя|хуе|хуё)|охуе|охуё)[а-яё]*|"
    r"(?:[её]бан|за[её]б|на[её]б|вы[её]б|по[её]б|отъ[её]б|пере[её]б|разъ[её]б|у[её]б)[а-яё]*|"
    r"(?:[её]бло|[её]бла|[её]блан|долбо[её]б|бляд)[а-яё]*|"
    r"пид[ао]р[а-яё]*|чурк[а-яё]*|ниггер[а-яё]*|нигер(?:а|ы|ов|ам|ами|е|у|ом)?|"
    r"хохл(?:ы|ов|ам|ами|а|у|ом|е)?)(?![а-яёa-z])", re.I)
HATE = re.compile(r"ксенофоб|расистск|гомофоб|разжиган.{0,25}ненавист|"
                  r"ненавист.{0,40}(?:националь|расов|этнич|религи|ориентац)|"
                  r"враждебн.{0,25}(?:признак|ксенофоб)", re.I)
MILD = re.compile(r"\b(?:сук[аиу]|сукой|сукины|дура[кч][а-яё]*|дурная|дурной|дурацк[а-яё]*|"
                  r"глуп[а-яё]*|идиот[а-яё]*|засран[а-яё]*)\b", re.I)
PLAYFUL = re.compile(r"еретическ|участник|официальный стиль|formal_style|шуточн|шутк|иронич|мем|дружеск", re.I)


def mild_rule(phrase):
    return bool(re.fullmatch(r"сук(?:а|и)?\*?|дур(?:ак|ная|ной)?\*?|идиот\*?|глуп\*?|засранец\*?", str(phrase).strip(), re.I))


def playful_only(text):
    text = str(text or "")
    if HEAVY.search(text) or HATE.search(text) or re.search(r"уби[йт]|убе[йи]|убью|убь[её]|казн|расстрел|сожг|сжеч|смерт|террор|насили|уничтож|расправ|угроз", text, re.I):
        return False
    words = re.findall(r"[а-яёa-z]+", text, re.I)
    return bool(MILD.search(text) and (PLAYFUL.search(text) or len(words) <= 3))


def legacy_word_collision(text):
    """Known ordinary words matched by the old unbounded ебл/бля search."""
    return bool(re.search(r"\b(?:[а-яё]*потребл|истребл|стебл|гребл|колебл|рубл|корабл|сабл|табл)[а-яё]*\b", str(text), re.I))


def rude_hint(text):
    text = re.sub(r"\bбез\s+(?:оскорблен\w*|бран\w*|ругатель\w*|унижен\w*)", "", str(text), flags=re.I)
    return bool(MILD.search(text) or re.search(r"оскорб|униж|бран|матер|ругатель|вульгар|насмеш", text, re.I))
