"""Bounded, offline Tesseract fallback. It extracts text, never describes a scene."""
import base64
import csv
import io
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def _status(command, minute):
    if not command:
        return {'available': False, 'detail': 'Tesseract не установлен. Linux: tesseract-ocr и tesseract-ocr-rus; Windows: установите Tesseract и добавьте в PATH.'}
    try:
        result = subprocess.run([command, '--list-langs'], capture_output=True, timeout=3, check=False)
        languages = result.stdout.decode('utf-8', 'replace').splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return {'available': False, 'detail': 'Tesseract найден, но не отвечает.'}
    russian = 'rus' in languages
    return {'available': result.returncode == 0, 'russian': russian,
            'detail': 'Локальный Tesseract: только надписи, без понимания сцены. ' + ('Русский и английский доступны.' if russian else 'Русский язык не установлен: добавьте tesseract-ocr-rus.')}


def status():
    return _status(shutil.which('tesseract'), int(time.time()) // 60)


def read(images):
    command = shutil.which('tesseract')
    if not command:
        return ''
    images = images if isinstance(images, (list, tuple)) else [images]
    words = []
    with tempfile.TemporaryDirectory(prefix='assistant-ocr-') as directory:
        for index, image in enumerate(images[:3]):
            if not isinstance(image, str) or ';base64,' not in image:
                continue
            try:
                content = base64.b64decode(image.split(',', 1)[1], validate=True)
                if len(content) > 3_000_000:
                    continue
                path = Path(directory) / ('image-' + str(index))
                path.write_bytes(content)
                language = 'rus+eng' if status().get('russian') else 'eng'
                result = subprocess.run([command, str(path), 'stdout', '-l', language, '--psm', '11', 'tsv'],
                                        capture_output=True, timeout=15, check=False)
                if result.returncode:
                    result = subprocess.run([command, str(path), 'stdout', '-l', 'eng', '--psm', '11', 'tsv'],
                                            capture_output=True, timeout=10, check=False)
                if result.returncode == 0:
                    # Uncertain OCR must not become a confident moderation hit.
                    rows = csv.DictReader(io.StringIO(result.stdout.decode('utf-8', 'replace')), delimiter='\t')
                    for row in rows:
                        if float(row.get('conf') or '-1') >= 80 and row.get('text', '').strip():
                            words.append(row['text'].strip()[:200])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
    return ' '.join(' '.join(words).split())[:1500]
