"""Portable facts and triggers, without database IDs or personal data in code."""

import re

from .core import split_items


PACK_FORMAT = "repostbot-triggers"
PACK_VERSION = 1
PACK_MARKER = re.compile(r"\[\[PACK_FACT:([a-z0-9_]{1,64})\]\]")
DB_MARKER = re.compile(r"\[\[FACT_(\d+)\]\]")


def export_pack(store):
    facts = store.fixed_facts()
    keys = {str(fact["id"]): "fact_" + str(fact["id"]) for fact in facts}
    result = {"format": PACK_FORMAT, "version": PACK_VERSION, "facts": [
        {"key": keys[str(fact["id"])], "name": fact["name"], "value": fact["value"], "triggers": fact["triggers"]}
        for fact in facts], "triggers": []}
    for item in store.triggers():
        rule = {key: item[key] for key in ("name", "phrases", "match_type", "action", "chats", "require_address", "enabled", "priority")}
        if item["action"] == "fact":
            if item["payload"] not in keys:
                raise ValueError("Триггер «" + item["name"] + "» ссылается на удалённые постоянные данные")
            rule["fact_key"] = keys[item["payload"]]
        else:
            def portable(match):
                if match.group(1) not in keys:
                    raise ValueError("Триггер «" + item["name"] + "» ссылается на удалённые постоянные данные")
                return "[[PACK_FACT:" + keys[match.group(1)] + "]]"
            rule["payload"] = DB_MARKER.sub(portable, item["payload"])
        result["triggers"].append(rule)
    return result


def _checked_pack(pack):
    if not isinstance(pack, dict) or pack.get("format") != PACK_FORMAT or pack.get("version") != PACK_VERSION:
        raise ValueError("Выберите JSON-файл триггеров RepostBot версии 1")
    facts, rules = pack.get("facts"), pack.get("triggers")
    if not isinstance(facts, list) or not isinstance(rules, list) or len(facts) > 50 or len(rules) > 100:
        raise ValueError("Файл может содержать до 50 постоянных данных и 100 триггеров")
    fact_keys, fact_names, rule_names = set(), set(), set()
    for fact in facts:
        if not isinstance(fact, dict) or not all(isinstance(fact.get(k), str) for k in ("key", "name", "value", "triggers")):
            raise ValueError("Некорректная запись постоянных данных")
        key, name = fact["key"], fact["name"].strip()
        if not re.fullmatch(r"[a-z0-9_]{1,64}", key) or key in fact_keys or not name or len(name) > 100 or len(fact["value"].strip()) not in range(1, 2001) or len(fact["triggers"]) > 500 or name.casefold() in fact_names:
            raise ValueError("Проверьте ключи, названия и значения постоянных данных в файле")
        fact_keys.add(key)
        fact_names.add(name.casefold())
    for rule in rules:
        if not isinstance(rule, dict) or not all(isinstance(rule.get(k), str) for k in ("name", "phrases", "match_type", "action", "chats")):
            raise ValueError("Некорректная запись триггера")
        name = rule["name"].strip()
        if not name or len(name) > 100 or name.casefold() in rule_names or not split_items(rule["phrases"]) or len(rule["phrases"]) > 1000:
            raise ValueError("Проверьте названия и фразы триггеров в файле")
        rule_names.add(name.casefold())
        if rule["action"] not in ("answer", "fact", "ai", "image") or rule["match_type"] not in ("contains", "starts", "exact"):
            raise ValueError("Неизвестное действие или тип совпадения в файле")
        if type(rule.get("enabled")) is not bool or type(rule.get("require_address")) is not bool or type(rule.get("priority")) is not int or not 0 <= rule["priority"] <= 999:
            raise ValueError("Некорректные настройки триггера")
        if any(not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]{5,}", chat) for chat in split_items(rule["chats"])):
            raise ValueError("Некорректный список чатов триггера")
        if rule["action"] == "fact":
            if rule.get("fact_key") not in fact_keys:
                raise ValueError("Триггер «" + name + "» ссылается на отсутствующие постоянные данные")
        else:
            payload = rule.get("payload")
            if not isinstance(payload, str) or len(payload) > 3500 or rule["action"] == "answer" and not payload.strip():
                raise ValueError("Некорректный текст триггера «" + name + "»")
            if any(match.group(1) not in fact_keys for match in PACK_MARKER.finditer(payload)):
                raise ValueError("Триггер «" + name + "» ссылается на отсутствующие постоянные данные")
            if DB_MARKER.search(payload):
                raise ValueError("В файле нужны переносимые ссылки [[PACK_FACT:ключ]], а не ID базы")
            if "[[PACK_FACT:" in PACK_MARKER.sub("", payload):
                raise ValueError("Некорректная ссылка на постоянные данные в триггере «" + name + "»")
    return facts, rules


def import_pack(store, pack, *, overwrite=False, preview=False):
    facts, rules = _checked_pack(pack)
    with store.lock:
        store.db.execute("SAVEPOINT trigger_pack")
        try:
            existing_facts = {row["name"].casefold(): dict(row) for row in store.db.execute("SELECT * FROM fixed_facts")}
            existing_rules = {row["name"].casefold(): dict(row) for row in store.db.execute("SELECT * FROM triggers")}
            # Duplicate names in the database cannot be mapped unambiguously.
            if len(existing_facts) != store.db.execute("SELECT COUNT(*) FROM fixed_facts").fetchone()[0] or len(existing_rules) != store.db.execute("SELECT COUNT(*) FROM triggers").fetchone()[0]:
                raise ValueError("В базе есть одинаковые названия. Переименуйте их перед импортом")
            if len(existing_facts) + sum(f["name"].casefold() not in existing_facts for f in facts) > 50 or len(existing_rules) + sum(r["name"].casefold() not in existing_rules for r in rules) > 100:
                raise ValueError("Импорт превысит лимит постоянных данных или триггеров")
            status = {"facts_added": 0, "facts_updated": 0, "facts_unchanged": 0, "triggers_added": 0, "triggers_updated": 0, "triggers_unchanged": 0, "conflicts": []}
            key_to_id = {}
            for fact in facts:
                name, value, terms = fact["name"].strip(), fact["value"].strip(), fact["triggers"].strip()
                old = existing_facts.get(name.casefold())
                if old:
                    key_to_id[fact["key"]] = old["id"]
                    if (old["name"], old["value"], old["triggers"]) == (name, value, terms):
                        status["facts_unchanged"] += 1
                    elif not overwrite:
                        status["conflicts"].append("Постоянные данные: " + name)
                    else:
                        store.db.execute("UPDATE fixed_facts SET name=?,value=?,triggers=? WHERE id=?", (name, value, terms, old["id"]))
                        status["facts_updated"] += 1
                else:
                    key_to_id[fact["key"]] = store.db.execute("INSERT INTO fixed_facts(name,value,triggers) VALUES (?,?,?)", (name, value, terms)).lastrowid
                    status["facts_added"] += 1
            for rule in rules:
                action = rule["action"]
                payload = str(key_to_id[rule["fact_key"]]) if action == "fact" else PACK_MARKER.sub(lambda m: '[[FACT_' + str(key_to_id[m.group(1)]) + ']]', rule["payload"])
                values = (rule["name"].strip(), rule["phrases"].strip(), rule["match_type"], action, payload, rule["chats"].strip(), int(rule["require_address"]), int(rule["enabled"]), rule["priority"])
                old = existing_rules.get(values[0].casefold())
                if old:
                    if tuple(old[k] for k in ("name", "phrases", "match_type", "action", "payload", "chats", "require_address", "enabled", "priority")) == values:
                        status["triggers_unchanged"] += 1
                    elif not overwrite:
                        status["conflicts"].append("Триггер: " + values[0])
                    else:
                        store.db.execute("UPDATE triggers SET name=?,phrases=?,match_type=?,action=?,payload=?,chats=?,require_address=?,enabled=?,priority=? WHERE id=?", values + (old["id"],))
                        status["triggers_updated"] += 1
                else:
                    store.db.execute("INSERT INTO triggers(name,phrases,match_type,action,payload,chats,require_address,enabled,priority) VALUES (?,?,?,?,?,?,?,?,?)", values)
                    status["triggers_added"] += 1
            if preview or status["conflicts"]:
                store.db.execute("ROLLBACK TO SAVEPOINT trigger_pack")
            store.db.execute("RELEASE SAVEPOINT trigger_pack")
            if status["conflicts"] and not preview:
                raise ValueError("Есть совпадающие названия с другими значениями. Просмотрите конфликты и включите замену: " + "; ".join(status["conflicts"])[:500])
            return status
        except Exception:
            # RELEASE may have happened already when reporting a conflict.
            if store.db.in_transaction:
                try:
                    store.db.execute("ROLLBACK TO SAVEPOINT trigger_pack")
                    store.db.execute("RELEASE SAVEPOINT trigger_pack")
                except Exception:
                    pass
            raise
