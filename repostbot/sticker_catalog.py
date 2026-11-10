"""One local identity and transactional catalog writer for every sticker source."""

import base64
import binascii
import hashlib
import json
import re
import struct
import time
from functools import lru_cache, wraps


MAPS = (
    "reply_sticker_semantics", "reply_sticker_formats", "reply_sticker_thumbs",
    "reply_sticker_packs", "reply_sticker_learning_pending", "reply_sticker_quarantine_sources",
    "reply_sticker_unique_ids", "reply_sticker_file_aliases", "reply_sticker_key_aliases",
    "reply_sticker_origins", "reply_sticker_bot_reviews",
    "reply_sticker_ban_evidence",
    "reply_sticker_curated_contexts",
)
CATALOG_SETTINGS = set(MAPS) | {
    "reply_stickers", "reply_sticker_catalog_backup", "reply_sticker_disabled", "reply_sticker_default_key",
}
KEY = re.compile(r"[a-zа-яё0-9_-]{1,24}")
FILE = re.compile(r"[A-Za-z0-9_-]{10,256}")


@lru_cache(maxsize=8192)
def document_identity(file_id):
    """Recover a sticker document ID from known Telegram file_id versions.

    file_id includes a renewable file_reference. Old catalogs did not always
    retain file_unique_id; the underlying document ID still proves identity.
    Decode only the known sticker layout; unknown/malformed formats stay opaque.
    """
    if not FILE.fullmatch(str(file_id)):
        return ''
    try:
        encoded = base64.b64decode(file_id + '=' * (-len(file_id) % 4), altchars=b'-_', validate=True)
        data, offset = bytearray(), 0
        while offset < len(encoded):
            value = encoded[offset]
            offset += 1
            if value == 0:
                if offset == len(encoded) or not encoded[offset]:
                    return ''
                data.extend(b'\0' * encoded[offset])
                offset += 1
            else:
                data.append(value)
            if len(data) > 1024:
                return ''
        if len(data) < 25 or data[-1] not in (2, 4):
            return ''
        payload_end = len(data) - (2 if data[-1] == 4 else 1)
        media_type, dc = struct.unpack_from('<ii', data)
        reference_flag = 1 << 25
        if media_type & ~reference_flag != 8 or not 0 < dc < 10000:
            return ''
        offset = 8
        if media_type & reference_flag:
            length = data[offset]
            header = 1
            if length == 254:
                length = int.from_bytes(data[offset + 1:offset + 4], 'little')
                header = 4
            elif length == 255:
                return ''
            offset += (header + length + 3) // 4 * 4
        if offset + 16 != payload_end:
            return ''
        document = struct.unpack_from('<q', data, offset)[0]
        return 'document:' + str(document) if document else ''
    except (ValueError, TypeError, binascii.Error, struct.error, IndexError):
        return ''


def document_keys(files):
    result = {}
    for file_id, key in files.items():
        identity = document_identity(file_id)
        if identity:
            result.setdefault(identity, key)
    return result


def catalog_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.store.lock:
            return method(self, *args, **kwargs)
    return locked


def mapping(value):
    try:
        value = json.loads(value or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def joined(*values):
    parts = []
    for value in values:
        for part in str(value or "").split("\n"):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return "\n".join(parts)


def free_key(preferred, file_id, taken):
    base = re.sub(r"[^a-zа-яё0-9_-]+", "_", str(preferred).casefold()).strip("_")[:15] or "sticker"
    candidate = str(preferred)
    count = 0
    while not KEY.fullmatch(candidate) or candidate in taken:
        candidate = base + "_" + hashlib.sha1((file_id + (str(count) if count else "")).encode()).hexdigest()[:8]
        count += 1
    return candidate


class StickerCatalog:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS sticker_catalog_repairs (id INTEGER PRIMARY KEY, at INTEGER NOT NULL, snapshot TEXT NOT NULL)")
            store.db.execute("CREATE INDEX IF NOT EXISTS sticker_safety_events_key ON sticker_safety_events(sticker_key)")

    def snapshot(self):
        return {key: self.store.setting(key) for key in CATALOG_SETTINGS}

    def normalize(self, incoming, recover=False):
        """Called inside Store.set_settings's transaction; never call APIs."""
        before = self.snapshot()
        data = {**before, **incoming}
        source = str(data.get("reply_stickers") or "")
        if recover and not source:
            source = str(data.get("reply_sticker_catalog_backup") or "")
        maps = {key: mapping(data.get(key)) for key in MAPS}
        try:
            disabled = set(json.loads(data.get("reply_sticker_disabled") or "[]"))
        except (TypeError, ValueError):
            disabled = set()
        rows, files, uniques, taken, aliases, learning_keys = {}, {}, {}, set(), {}, {}
        documents = {}
        previous_documents = document_keys(maps['reply_sticker_file_aliases'])
        structural = False
        merged = {key: {} for key in MAPS}
        blocked = set()
        for raw in source.splitlines():
            if not raw.strip():
                continue
            fields = [part.strip() for part in raw.split("|", 2)]
            if len(fields) < 2 or not FILE.fullmatch(fields[1]):
                # Keep invalid legacy data in the repair snapshot, not the live catalog.
                structural = True
                continue
            old_key, file_id = fields[:2]
            description = fields[2] if len(fields) > 2 else ""
            document = document_identity(file_id)
            previous_key = maps["reply_sticker_file_aliases"].get(file_id) or previous_documents.get(document, '')
            unique = str(maps["reply_sticker_unique_ids"].get(old_key) or maps["reply_sticker_unique_ids"].get(previous_key) or "")
            legacy_learning = bool(re.fullmatch(r"обучение_[a-f0-9]{10}", old_key))
            if old_key in rows and not legacy_learning and file_id not in files:
                unique = ""  # A reused human-readable name is not proof of identity.
            canonical = (files.get(file_id) or (uniques.get(unique) if unique else None)
                         or documents.get(document) or (learning_keys.get(old_key) if legacy_learning else None))
            if canonical is None:
                canonical = free_key(old_key, file_id, taken)
                taken.add(canonical)
                rows[canonical] = [file_id, description[:100]]
                structural |= canonical != old_key
            else:
                structural = True
                # Keep the canonical name and analysis, but use the later
                # file reference from an appended/reimported duplicate.
                rows[canonical][0] = file_id
                if not rows[canonical][1]:
                    rows[canonical][1] = description[:100]
            files[file_id] = canonical
            if document:
                documents[document] = canonical
            if legacy_learning:
                learning_keys[old_key] = canonical
            if unique:
                uniques[unique] = canonical
                merged["reply_sticker_unique_ids"][canonical] = unique
            aliases.setdefault(old_key, canonical)
            if previous_key:
                aliases.setdefault(previous_key, canonical)
            if old_key in disabled or previous_key in disabled:
                blocked.add(canonical)
            for metadata_key in dict.fromkeys((old_key, previous_key)):
                if not metadata_key:
                    continue
                for setting in (*MAPS[:6], 'reply_sticker_curated_contexts'):
                    value = maps[setting].get(metadata_key)
                    if value is None:
                        continue
                    if setting in ("reply_sticker_semantics", "reply_sticker_learning_pending", 'reply_sticker_curated_contexts'):
                        merged[setting][canonical] = value if canonical not in merged[setting] or merged[setting][canonical] == value else joined(merged[setting][canonical], value)
                    elif setting == "reply_sticker_packs":
                        previous = merged[setting].get(canonical, "")
                        priority = lambda pack: 3 if pack == "Банворды" else 0 if pack in ("", "Обучение", "Без набора") else 2
                        if not previous or priority(value) > priority(previous):
                            merged[setting][canonical] = value
                    else:
                        merged[setting].setdefault(canonical, value)
                origin = maps["reply_sticker_origins"].get(metadata_key)
                if origin:
                    old_origin = merged["reply_sticker_origins"].get(canonical)
                    if not old_origin or origin in ("manual", "imported"):
                        merged["reply_sticker_origins"][canonical] = origin
                review = maps["reply_sticker_bot_reviews"].get(metadata_key)
                if isinstance(review, dict):
                    prior = merged["reply_sticker_bot_reviews"].get(canonical, {})
                    allowed = lambda value: value.get("confidence") == "high" and value.get("kind") in ("safe", "playful") and not value.get("manual_blocked")
                    if not prior or not allowed(review):
                        merged["reply_sticker_bot_reviews"][canonical] = review
                evidence = maps["reply_sticker_ban_evidence"].get(metadata_key)
                if isinstance(evidence, dict) and evidence:
                    merged["reply_sticker_ban_evidence"].setdefault(canonical, evidence)
            # Descriptions of folded rows must not disappear with their aliases.
            if description and (description != rows[canonical][1] or canonical != old_key):
                merged["reply_sticker_semantics"][canonical] = joined(merged["reply_sticker_semantics"].get(canonical), description)
        for file_id, key in maps["reply_sticker_file_aliases"].items():
            canonical = aliases.get(key, key)
            if canonical in rows:
                files.setdefault(file_id, canonical)
        merged["reply_sticker_file_aliases"] = dict(sorted(files.items()))
        for old_key, key in maps["reply_sticker_key_aliases"].items():
            canonical = aliases.get(key, key)
            if canonical in rows and old_key not in rows:
                aliases.setdefault(old_key, canonical)
        merged["reply_sticker_key_aliases"] = {key: value for key, value in aliases.items() if key != value and key not in rows}
        from .sticker_context import PRESETS, PACK_OVERRIDES
        for original, (_, note) in PRESETS.items():
            key = merged['reply_sticker_key_aliases'].get(original, original)
            if key in rows:
                merged['reply_sticker_curated_contexts'].setdefault(key, note)
                if original in PACK_OVERRIDES and merged['reply_sticker_packs'].get(key) in (None,'','Обучение','Без набора'):
                    merged['reply_sticker_packs'][key] = PACK_OVERRIDES[original]
        for key in rows:
            source_pack = merged["reply_sticker_quarantine_sources"].get(key) if merged["reply_sticker_packs"].get(key) == "Банворды" else merged["reply_sticker_packs"].get(key)
            inferred = "learned" if source_pack == "Обучение" or (not source_pack or source_pack == "Без набора") and key.startswith("обучение_") else "manual"
            merged["reply_sticker_origins"].setdefault(key, inferred)
        # These meanings were explicitly supplied by the owner, rather
        # than inferred by learning. Preserve all existing off/ban decisions.
        for original in PRESETS:
            key = merged['reply_sticker_key_aliases'].get(original, original)
            review = merged['reply_sticker_bot_reviews'].get(key, {})
            if key in rows and merged['reply_sticker_packs'].get(key) != 'Банворды' and not review.get('manual_blocked'):
                merged['reply_sticker_origins'][key] = 'manual'
                merged['reply_sticker_learning_pending'].pop(key, None)
        for key, pack in merged["reply_sticker_packs"].items():
            if pack == "Банворды":
                blocked.add(key)
                merged["reply_sticker_learning_pending"].pop(key, None)
        catalog = "\n".join("|".join((key, file_id, description)).rstrip("|") for key, (file_id, description) in rows.items())
        output = {"reply_stickers": catalog, "reply_sticker_catalog_backup": catalog,
                  "reply_sticker_disabled": json.dumps(sorted(blocked), ensure_ascii=False)}
        output.update({key: json.dumps(value, ensure_ascii=False) for key, value in merged.items()})
        default = str(data.get("reply_sticker_default_key") or "")
        output["reply_sticker_default_key"] = aliases.get(default, default)
        if structural:
            snapshot = json.dumps(data, ensure_ascii=False)
            previous = self.store.db.execute("SELECT snapshot FROM sticker_catalog_repairs ORDER BY id DESC LIMIT 1").fetchone()
            if not previous or previous[0] != snapshot:
                self.store.db.execute("INSERT INTO sticker_catalog_repairs(at,snapshot) VALUES (?,?)", (int(time.time()), snapshot))
        # Fold counters into the surviving key as part of the same transaction.
        previous_aliases = mapping(before.get("reply_sticker_key_aliases"))
        for old_key, key in merged["reply_sticker_key_aliases"].items():
            if previous_aliases.get(old_key) != key:
                self.store.db.execute("UPDATE sticker_safety_events SET sticker_key=? WHERE sticker_key=?", (key, old_key))
        return {**incoming, **{key: value for key, value in output.items() if key in incoming or value != before.get(key)}}

    def repair(self):
        with self.store.lock, self.store.db:
            values = self.normalize({}, recover=True)
            self.store.db.executemany("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())

    def resolve_key(self, key):
        key = str(key or "").strip().casefold()
        return mapping(self.store.setting("reply_sticker_key_aliases")).get(key, key)

    def find(self, file_id, unique_id=""):
        with self.store.lock:
            files = mapping(self.store.setting("reply_sticker_file_aliases"))
            if str(file_id) in files:
                return files[str(file_id)]
            if unique_id:
                unique = mapping(self.store.setting("reply_sticker_unique_ids"))
                known = next((key for key, value in unique.items() if value == str(unique_id)), '')
                if known:
                    return known
            identity = document_identity(file_id)
            return document_keys(files).get(identity, '') if identity else ''

    def upsert_many(self, stickers, pack="", preferred_keys=None, origin=""):
        """Merge API identities and metadata without holding the lock over network I/O."""
        with self.store.lock:
            data = self.snapshot()
            maps = {key: mapping(data.get(key)) for key in MAPS}
            rows = {}
            for line in data["reply_stickers"].splitlines():
                parts = line.split("|", 2)
                if len(parts) >= 2:
                    rows[parts[0]] = parts[1:]
            files = maps["reply_sticker_file_aliases"]
            files.update({value[0]: key for key, value in rows.items()})
            by_document = document_keys(files)
            unique = maps["reply_sticker_unique_ids"]
            by_unique = {value: key for key, value in unique.items() if key in rows}
            keys, added = [], 0
            for index, sticker in enumerate(stickers):
                file_id = str(sticker.get("file_id") or "")
                if not FILE.fullmatch(file_id):
                    continue
                uid = str(sticker.get("file_unique_id") or "")
                document = document_identity(file_id)
                suggested = preferred_keys[index] if preferred_keys else "обучение_" + hashlib.sha1((uid or file_id).encode()).hexdigest()[:10]
                key = files.get(file_id) or by_unique.get(uid) or by_document.get(document)
                # Legacy learning rows used this stable key before saving UID metadata.
                if not key and uid and suggested in rows and (not unique.get(suggested) or unique.get(suggested) == uid):
                    key = suggested
                if key not in rows:
                    key = free_key(suggested, file_id, set(rows))
                    rows[key] = [file_id, str(sticker.get("description") or "")[:100]]
                    added += 1
                else:
                    # file_id can change while Telegram's file_unique_id stays stable.
                    files[rows[key][0]] = key
                    rows[key][0] = file_id
                    description = str(sticker.get("description") or "")
                    if description:
                        if not (len(rows[key]) > 1 and rows[key][1]):
                            rows[key] = [file_id, description[:100]]
                        else:
                            maps["reply_sticker_semantics"][key] = joined(maps["reply_sticker_semantics"].get(key), description)
                files[file_id] = key
                if document:
                    by_document[document] = key
                current_origin = maps["reply_sticker_origins"].get(key)
                if origin in ("manual", "imported") or origin == "learned" and current_origin not in ("manual", "imported"):
                    maps["reply_sticker_origins"][key] = origin
                if uid:
                    unique[key] = uid
                    by_unique[uid] = key
                if "is_video" in sticker or "is_animated" in sticker:
                    maps["reply_sticker_formats"][key] = "video" if sticker.get("is_video") else "animated" if sticker.get("is_animated") else "static"
                thumb = sticker.get("thumbnail") or sticker.get("thumb") or {}
                if thumb.get("file_id"):
                    maps["reply_sticker_thumbs"][key] = thumb["file_id"]
                current_pack = maps["reply_sticker_packs"].get(key, "")
                if pack and current_pack != "Банворды" and (pack != "Обучение" or not current_pack):
                    maps["reply_sticker_packs"][key] = pack
                keys.append(key)
            values = {"reply_stickers": "\n".join("|".join([key, *value]) for key, value in rows.items())}
            values.update({key: json.dumps(value, ensure_ascii=False) for key, value in maps.items()})
            self.store.set_settings(values)
            aliases = mapping(self.store.setting('reply_sticker_key_aliases'))
            return [aliases.get(key, key) for key in keys], added
