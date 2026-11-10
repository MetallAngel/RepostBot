"""Small optional live forecast: no language-model calls or shared location history."""
import json
import re
import time
from urllib.parse import urlencode


WEATHER = re.compile(r"погод|прогноз|температур|дожд|снег|осадк|ветер|жарк|холодн", re.I)
CITY = re.compile(r"[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z -]{2,45}")
CITY_TRAILING = re.compile(r"\b(?:на\s+)?(?:сегодня|завтра|послезавтра|сейчас|вечером|утром|ночью|дн[её]м|пожалуйста|какая|какой|сколько|будет|там)\b", re.I)


def named_city(value):
    match = re.search(r"\b(?:живу\s+в\s+(?:городе\s+)?|нахожусь\s+в\s+(?:городе\s+)?|из\s+(?:города\s+)?|в\s+городе\s+|городе?\s+)([А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z -]{2,40})", value, re.I)
    if not match and WEATHER.search(value):
        match = re.search(r"\b(?:в|для)\s+([А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z -]{2,40})", value, re.I)
    if not match:
        return ""
    candidate = re.split(r"[,.;?!]|\s+[—–-]\s+", match[1], 1)[0].strip()
    candidate = CITY_TRAILING.split(candidate, 1)[0].strip()
    return candidate if CITY.fullmatch(candidate) and len(candidate.split()) <= 4 else ""


def bare_city(value):
    """Only a short place name can be a follow-up; a normal sentence is not a city."""
    candidate = value.strip(" ?!.,")
    return candidate if re.fullmatch(r"[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z-]{2,44}", candidate) and candidate.casefold() not in ("помощник", "помощник") else ""


def city_queries(city):
    """Try a small set of Russian nominative forms if geocoding declines a locative."""
    yield city
    if city.casefold().endswith("е") and len(city) > 4:
        yield city[:-1] + "а"  # Москве → Москва; Кинешме → Кинешма
        yield city[:-1]        # Петербурге → Петербург
    elif city.casefold().endswith("и") and len(city) > 4:
        yield city[:-1] + "ь"  # Казани → Казань


def condition(code):
    if code == 0:
        return "ясно"
    if code in (1, 2, 3):
        return "облачно"
    if code in (45, 48):
        return "туман"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "дождь"
    if code in (71, 73, 75, 77, 85, 86):
        return "снег"
    if code in (95, 96, 99):
        return "гроза"
    return "погода переменчива"


def answer(store, request_json, chat, person, question, diagnostic=False):
    if store.setting("weather_enabled", "0") != "1" or not person:
        return None
    asked = bool(WEATHER.search(question))
    if not asked:
        # Follow up only when the bot has just asked this person for a city.
        prompt_key = "weather_city_prompt:" + str(chat) + ":" + str(person)
        if int(store.state(prompt_key, "0")) < int(time.time()) - 600:
            return None
        explicit = named_city(question) or bare_city(question)
        if not explicit:
            return None
    else:
        prompt_key = "weather_city_prompt:" + str(chat) + ":" + str(person)
        explicit = named_city(question)
    if not explicit and asked:
        with store.lock:
            rows = store.db.execute(
                "SELECT text FROM chat_memory WHERE chat=? AND person=? AND at>=? AND text<>? ORDER BY at DESC,message_id DESC LIMIT 30",
                (str(chat), str(person), int(time.time()) - 90 * 86400, question)).fetchall()
        for row in rows:
            candidate = named_city(row[0])
            if candidate and not WEATHER.search(candidate) and "стикер" not in candidate.casefold():
                explicit = candidate
                break
    city = explicit or store.state("weather_city:" + str(person), "")
    if not city:
        store.set_state(prompt_key, int(time.time()))
        return "Назови город, путник, и я узнаю для тебя погоду."
    if not CITY.fullmatch(city):
        store.set_state(prompt_key, int(time.time()))
        return "Назови город словами, чтобы я нашла его на карте."
    store.set_state(prompt_key, 0)
    cache_key = "weather_cache:" + city.casefold()
    if not diagnostic:
        try:
            cached = json.loads(store.state(cache_key, "{}"))
            if int(time.time()) - int(cached["at"]) < 600:
                return cached["answer"]
        except (ValueError, KeyError, TypeError):
            pass
    api_key = store.setting("weather_api_key", "")
    params = {"name": city, "count": 1, "language": "ru", "format": "json"}
    if api_key:
        params["apikey"] = api_key
    geo_host = "customer-geocoding-api.open-meteo.com" if api_key else "geocoding-api.open-meteo.com"
    try:
        place = None
        for variant in city_queries(city):
            params["name"] = variant
            results = request_json("https://" + geo_host + "/v1/search?" + urlencode(params), timeout=10).get("results") or []
            if results:
                place = results[0]
                break
        if place is None:
            raise IndexError("город не найден")
        params = {"latitude": float(place["latitude"]), "longitude": float(place["longitude"]),
                  "current": "temperature_2m,weather_code,wind_speed_10m",
                  "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                  "timezone": "auto", "forecast_days": 2}
        if api_key:
            params["apikey"] = api_key
        host = "customer-api.open-meteo.com" if api_key else "api.open-meteo.com"
        forecast = request_json("https://" + host + "/v1/forecast?" + urlencode(params), timeout=12)
        current, daily = forecast["current"], forecast["daily"]
        label = str(place.get("name") or city)[:60]
        tomorrow = (f" Завтра: {daily['temperature_2m_min'][1]:g}…{daily['temperature_2m_max'][1]:g} °C, "
                    f"вероятность осадков до {daily['precipitation_probability_max'][1]:g}%.") if len(daily.get("time", [])) > 1 else ""
        result = (f"Город {label}: сейчас {current['temperature_2m']:g} °C, {condition(int(current.get('weather_code', -1)))}; "
                  f"ветер {current['wind_speed_10m']:g} км/ч." + tomorrow + " Данные: Open-Meteo.")
        store.set_state("weather_city:" + str(person), label)
        store.set_state(cache_key, json.dumps({"at": int(time.time()), "answer": result}, ensure_ascii=False))
        return result
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        store.event("info", "Погода: город или ответ сервиса не распознан: " + str(exc)[:100])
        store.set_state(prompt_key, int(time.time()))
        return "Не смогла найти такой город или разобрать прогноз. Уточни название города." + ((" Причина: " + str(exc)[:120]) if diagnostic else "")
    except Exception as exc:
        reason = str(exc).replace(api_key, "[ключ]")[:180] if api_key else str(exc)[:180]
        store.event("error", "Погода: сервис недоступен: " + reason[:120])
        return "Погодный свиток пока закрыт пеленой. Попробуй спросить чуть позже." + ((" Причина: " + reason) if diagnostic else "")
