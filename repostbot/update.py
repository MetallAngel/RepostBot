"""Validate and stage a code-only update for all supported launchers."""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
CURRENT_VERSION = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
MAX_UPLOAD = 20 * 1024 * 1024
MAX_UNPACKED = 40 * 1024 * 1024
REQUIRED = {"version.json", "repostbot/app.py", "repostbot/core.py", "repostbot/community.py", "repostbot/conversation.py", "repostbot/rich_reply.py", "repostbot/dice.py", "repostbot/dice_assets.py", "repostbot/quests.py", "repostbot/quest_text.py", "repostbot/auto_quests.py", "repostbot/memory_search.py", "repostbot/reply_context.py", "repostbot/reply_behavior.py", "repostbot/vk_identity.py", "repostbot/reply_metrics.py", "repostbot/speech.py", "repostbot/speech_engine.py", "repostbot/speech_voices.py", "repostbot/service_messages.py", "repostbot/twitch.py", "repostbot/message_template.py", "repostbot/trigger_pack.py", "repostbot/update.py", "start_windows.bat", "start_windows.ps1"}
REQUIRED |= {'repostbot/model_router.py', 'repostbot/local_ocr.py', 'repostbot/glitch_context.py', 'repostbot/social_features.py'}
REQUIRED |= {'repostbot/presence.py', 'repostbot/presence_actions.py', 'repostbot/idle_schedule.py'}
REQUIRED.add('repostbot/sticker_catalog.py')
REQUIRED.add('repostbot/sticker_policy.py')
REQUIRED.add('repostbot/role_scenes.py')
REQUIRED.add('repostbot/personal_roles.py')
REQUIRED.add('repostbot/stream_series.py')
REQUIRED.add('repostbot/sticker_context.py')
REQUIRED |= {'repostbot/scout.py', 'repostbot/scout_transport.py'}
REQUIRED.add('repostbot/scout_scenes.py')
REQUIRED.add('repostbot/scout_testing.py')
REQUIRED.add('repostbot/scout_dialogue.py')
REQUIRED |= {'repostbot/scout_style.py','repostbot/community_context.py'}
REQUIRED.add('repostbot/scout_priority.py')
REQUIRED |= {'repostbot/scout_disguise.py', 'repostbot/scout_avatar_assets.py'}
REQUIRED.add('repostbot/participant_roles.py')
REQUIRED |= {'repostbot/scout_joined.py','repostbot/scout_history.py'}
REQUIRED |= {'repostbot/scout_glitch.py','repostbot/scout_report_context.py'}
REQUIRED.add('repostbot/scout_roll.py')
REQUIRED.add('repostbot/scout_handoff.py')
REQUIRED.add('repostbot/idle_context.py')
REQUIRED.add('repostbot/reply_turn.py')
REQUIRED.add('repostbot/scout_cdp.py')
REQUIRED.add('repostbot/speaker_identity.py')
REQUIRED.add('repostbot/scout_twitch_irc.py')
REQUIRED |= {'repostbot/twitch_bots.py','repostbot/twitch_ledger.py','repostbot/account_links.py','repostbot/repost_stats.py'}
REQUIRED |= {'repostbot/scout_twitch.py','repostbot/scout_twitch_browser.py','repostbot/twitch_emotes.py'}
REQUIRED |= {'repostbot/temporary_context.py','repostbot/glitch_scope.py','repostbot/glitch_profile.py','repostbot/bot_profile_api.py','repostbot/glitch_avatar_assets.py'}
# build_release.py is a developer-only helper and must stay outside update
# archives so older launchers can accept newer releases.
ROOT_FILES = {"DEVELOPMENT.md", "requirements.txt", "requirements-dev.txt", "version.json", "README.md", "start_windows.bat", "start_windows.ps1", "start_linux.sh", "start_container.sh", "Dockerfile", "compose.yaml", "compose.casaos.yaml", ".env.example", ".dockerignore"}


def version_tuple(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Некорректный номер версии")
    return tuple(int(n) for n in value.split("."))


def allowed_file(name):
    if name in ROOT_FILES:
        return True
    parts = PurePosixPath(name).parts
    if len(parts) == 2 and parts[0] in ("repostbot", "tests") and parts[1].endswith(".py"):
        return True
    if len(parts) == 3 and parts[:2] == ("repostbot", "static") and parts[2] in ("index.html", "style.css", "app.js"):
        return True
    if len(parts) == 4 and parts[:3] == ("repostbot", "static", "dice") and parts[3].endswith(".gif"):
        return True
    return False


def inspect_package(raw):
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError("Архив пустой или больше 20 МБ")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError):
        raise ValueError("Файл не является ZIP-архивом") from None
    with archive:
        entries = {}
        total = 0
        for item in archive.infolist():
            if item.is_dir():
                continue
            parts = PurePosixPath(item.filename).parts
            if len(parts) < 2 or parts[0] != "RepostBot":
                raise ValueError("В архиве требуется папка RepostBot")
            name = "/".join(parts[1:])
            if not allowed_file(name) or ".." in parts or "\\" in item.filename or item.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Недопустимый файл в архиве: " + item.filename[:100])
            if name in entries:
                raise ValueError("Повтор файла в архиве: " + name)
            if item.file_size > MAX_UPLOAD:
                raise ValueError("Слишком большой файл в архиве")
            entries[name] = item
            total += item.file_size
            if total > MAX_UNPACKED:
                raise ValueError("Слишком большой распакованный архив")
        if not REQUIRED.issubset(entries):
            raise ValueError("В архиве отсутствуют файлы новой версии")
        if archive.testzip() is not None:
            raise ValueError("Архив повреждён")
        try:
            manifest = json.loads(archive.read(entries["version.json"]))
            version = manifest["version"]
            if version_tuple(version) <= version_tuple(CURRENT_VERSION):
                raise ValueError("Выберите версию новее " + CURRENT_VERSION)
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ValueError("В архиве нет корректного version.json") from None
    return version


def stage_package(raw, data_dir):
    version = inspect_package(raw)
    updates = Path(data_dir) / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    temporary = updates / "pending.tmp"
    pending = updates / "pending.zip"
    temporary.write_bytes(raw)
    temporary.replace(pending)
    return version


def pending_version(data_dir):
    pending = Path(data_dir) / "updates" / "pending.zip"
    if not pending.exists():
        return None
    try:
        return inspect_package(pending.read_bytes())
    except (ValueError, OSError):
        return None


def apply_staged(data_dir):
    """Run from the launcher after the bot exits; keep data and runtime intact."""
    pending = Path(data_dir) / "updates" / "pending.zip"
    raw = pending.read_bytes()
    version = inspect_package(raw)
    with tempfile.TemporaryDirectory(prefix="repostbot-update-") as staging:
        stage = Path(staging)
        changed = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [("/".join(PurePosixPath(i.filename).parts[1:]), i) for i in archive.infolist() if not i.is_dir()]
            # version.json is copied last, so a partial update never advertises success.
            entries.sort(key=lambda pair: pair[0] == "version.json")
            for name, item in entries:
                source = stage / "new" / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(archive.read(item))
        try:
            for name, _ in entries:
                dest = ROOT / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                backup = stage / "backup" / name
                existed = dest.exists()
                if existed:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, backup)
                temporary = dest.with_name(dest.name + ".update-tmp")
                shutil.copy2(stage / "new" / name, temporary)
                os.replace(temporary, dest)
                changed.append((dest, backup if existed else None))
        except Exception:
            for dest, backup in reversed(changed):
                if backup is None:
                    dest.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, dest)
            raise
    pending.unlink()
    (Path(data_dir) / "updates" / "apply-requested.txt").unlink(missing_ok=True)
    return version


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "apply":
        raise SystemExit("Usage: python -m repostbot.update apply DATA_DIR")
    print("Installed RepostBot " + apply_staged(sys.argv[2]))
