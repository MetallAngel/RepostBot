"""Quest descriptions keep their conditions without exposing point counters."""
from datetime import datetime
from zoneinfo import ZoneInfo


ZONE = ZoneInfo('Europe/Moscow')
ONES = ('ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять')
TEENS = ('десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать', 'пятнадцать',
         'шестнадцать', 'семнадцать', 'восемнадцать', 'девятнадцать')
TENS = ('', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто')
HUNDREDS = ('', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот', 'семьсот', 'восемьсот', 'девятьсот')
ORDINAL_DAYS = ('', 'первого', 'второго', 'третьего', 'четвёртого', 'пятого', 'шестого', 'седьмого',
                'восьмого', 'девятого', 'десятого', 'одиннадцатого', 'двенадцатого', 'тринадцатого',
                'четырнадцатого', 'пятнадцатого', 'шестнадцатого', 'семнадцатого', 'восемнадцатого',
                'девятнадцатого', 'двадцатого', 'двадцать первого', 'двадцать второго', 'двадцать третьего',
                'двадцать четвёртого', 'двадцать пятого', 'двадцать шестого', 'двадцать седьмого',
                'двадцать восьмого', 'двадцать девятого', 'тридцатого', 'тридцать первого')
MONTHS = ('', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря')


def number_words(value, gender='male'):
    number = int(value)
    if number == 1000:
        return 'тысяча'
    if not 0 <= number < 1000:
        return 'много'
    if not number:
        return ONES[0]
    parts = [HUNDREDS[number // 100]] if number >= 100 else []
    tail = number % 100
    if 10 <= tail < 20:
        parts.append(TEENS[tail - 10])
    else:
        if tail >= 20:
            parts.append(TENS[tail // 10])
        unit = tail % 10
        if unit:
            word = ONES[unit]
            if gender == 'female' and unit in (1, 2):
                word = 'одна' if unit == 1 else 'две'
            elif gender == 'neuter' and unit == 1:
                word = 'одно'
            parts.append(word)
    return ' '.join(parts)


def quantity(value, forms, gender='male'):
    value = int(value)
    index = 2 if 11 <= value % 100 <= 14 else 0 if value % 10 == 1 else 1 if 2 <= value % 10 <= 4 else 2
    return number_words(value, gender) + ' ' + forms[index]


def hours_phrase(hours):
    hours = int(hours)
    return quantity(hours // 24, ('день', 'дня', 'дней')) if hours % 24 == 0 else quantity(hours, ('час', 'часа', 'часов'))


def quest_deadline(at):
    moment = datetime.fromtimestamp(at, ZONE)
    clock = quantity(moment.hour, ('час', 'часа', 'часов'))
    clock += ' ' + quantity(moment.minute, ('минута', 'минуты', 'минут'), 'female') if moment.minute else ' ровно'
    return ORDINAL_DAYS[moment.day] + ' ' + MONTHS[moment.month] + ', ' + clock + ' по московскому времени'


def quest_progress(run):
    import json
    rule = json.loads(run['rule'])
    progress, target = run['progress'], rule['target']
    if not progress:
        return 'Первые строки этого пути ещё ждут добрых дел.'
    if progress >= target:
        return 'Писарь собрал все добрые свидетельства этого пути.'
    if rule['kind'] in ('calm', 'visits', 'days'):
        return 'Позади ' + quantity(progress, ('мирный день', 'мирных дня', 'мирных дней')) + '. Сохраняй мир, пока песок в часах не иссякнет.'
    if progress * 2 >= target:
        return 'Больше половины пути уже вписано в летопись. Продолжай добрые дела — конец свитка близко.'
    return 'Первые добрые следы уже на полях летописи. Писарь ждёт продолжения твоего пути.'


def reward_words(value):
    amount = float(value)
    return number_words(int(amount)) + (' с половиной' if amount % 1 else '')
