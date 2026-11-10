"""Stateless OpenAI transport and the official local Sign in with ChatGPT flow."""

import base64
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

API = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class AIError(RuntimeError):
    def __init__(self, message, code='', status=None):
        super().__init__(message)
        self.code, self.status = code, status


class QuotaExceeded(AIError):
    pass


class IncompleteResponse(AIError):
    """A generation limit is not evidence that the provider is offline."""
    pass


QUOTA_CODES = {"insufficient_quota", "rate_limit_exceeded", "rate_limit_error", "usage_limit_exceeded", "subscription_sharing_usage_limit_exceeded", "billing_hard_limit_reached", "insufficient_balance", "insufficient_balance_error", "credits_exhausted"}


def _api_error(exc):
    """Report only a structured error code, never request bodies or tokens."""
    code = "request_failed"
    message = ""
    try:
        data = json.loads(exc.read(4096))
        error = data.get("error", {})
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or code)
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            code = error
    except (ValueError, OSError):
        pass
    messages = {
        "invalid_grant": "Авторизация истекла. Подключите аккаунт повторно.",
        "insufficient_quota": "Закончился баланс API или достигнут лимит расходов.",
        "subscription_sharing_usage_limit_exceeded": "Достигнут лимит ChatGPT. Проверьте использование в настройках ChatGPT.",
        "subscription_sharing_usage_unavailable": "Использование тарифа ChatGPT сейчас недоступно для этого аккаунта.",
        "subscription_sharing_user_not_eligible": "Этот аккаунт авторизован, но не допущен к использованию тарифа ChatGPT через внешнее приложение. Выберите другой аккаунт или переключитесь на OpenAI API/Ollama.",
        "model_not_found": "Модель недоступна. Обновите список и выберите доступную модель.",
        "billing_hard_limit_reached": "Достигнут жёсткий лимит расходов OpenAI API.",
    }
    error_class = QuotaExceeded if code in QUOTA_CODES or exc.code in (402, 429) else AIError
    detail = messages.get(code) or ("OpenAI: " + message[:220] if message else f"OpenAI: HTTP {exc.code}, код {code[:80]}")
    return error_class(detail, code=code, status=exc.code)


def openai_request(url, payload=None, token="", form=False, timeout=90):
    from .core import _https_context
    headers = {"User-Agent": "RepostBot", "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        body = urlencode(payload).encode() if form else json.dumps(payload, ensure_ascii=False).encode()
    try:
        request = Request(url, data=body, headers=headers)
        if token:
            # Compatible gateways must not forward credentials on redirects.
            request.add_unredirected_header('Authorization', 'Bearer ' + token)
        return urlopen(request, timeout=timeout, context=_https_context(True))
    except HTTPError as exc:
        raise _api_error(exc) from None
    except (URLError, TimeoutError, OSError):
        raise AIError("OpenAI: не удалось установить защищённое соединение", code='connection_failed') from None


def openai_json(url, payload=None, token="", form=False, timeout=90, allow_empty=False):
    with openai_request(url, payload, token, form, timeout) as response:
        raw = response.read(24 * 1024 * 1024 + 1)
    if not raw and allow_empty:
        return {}
    if len(raw) > 24 * 1024 * 1024:
        raise AIError("OpenAI: слишком большой ответ")
    return json.loads(raw)


def response_text(token, model, instructions, messages, account=False, max_tokens=300, timeout=90):
    payload = {"model": model, "instructions": instructions, "input": messages,
               "store": False, "stream": True}
    if not account:
        # Responses counts hidden reasoning in the same output allowance.
        # A 240-token JSON check can otherwise exhaust its budget before text.
        reasoning_model = bool(re.match(r'^gpt-[56](?:[.-]|$)', model))
        payload["max_output_tokens"] = max(int(max_tokens), 2048) if reasoning_model and not model.startswith('gpt-5.4') else int(max_tokens)
    if model.startswith("gpt-5.4"):
        payload["reasoning"] = {"effort": "none"}
    elif re.match(r'^gpt-[56](?:[.-]|$)', model):
        payload["reasoning"] = {"effort": "low"}
    # No server-side conversation, response ID, tools, search, or background job.
    with openai_request(API + "/responses", payload, token, timeout=timeout) as response:
        event_data = []
        chunks = []
        total = 0
        completed = None
        for raw_line in response:
            total += len(raw_line)
            if total > 8 * 1024 * 1024:
                raise AIError("OpenAI: поток ответа слишком большой")
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                event_data.append(line[5:].strip())
            elif not line and event_data:
                data = "\n".join(event_data)
                event_data = []
                if data == "[DONE]":
                    continue
                item = json.loads(data)
                kind = item.get("type")
                if kind == "response.output_text.delta":
                    chunks.append(item.get("delta", ""))
                elif kind == "response.completed":
                    completed = item.get("response", {})
                elif kind in ("response.failed", "response.incomplete", "error"):
                    response_data = item.get('response') or {}
                    error = response_data.get("error") or item.get("error") or {}
                    incomplete = response_data.get('incomplete_details') or {}
                    code = incomplete.get('reason') or (error.get("code", item.get("code", kind)) if isinstance(error, dict) else kind)
                    error_class = QuotaExceeded if code in QUOTA_CODES else IncompleteResponse if kind == 'response.incomplete' else AIError
                    raise error_class("OpenAI: ответ не завершён, код " + str(code)[:80], code=str(code))
        if completed is None:
            raise AIError("OpenAI: соединение прервалось до завершения ответа")
    text = "".join(chunks).strip()
    if not text:
        text = "\n".join(part.get("text", "") for out in completed.get("output", [])
                         if out.get("type") == "message" for part in out.get("content", [])
                         if part.get("type") == "output_text").strip()
    if not text:
        raise AIError("OpenAI вернул пустой ответ")
    return {"text": text[:3500], "usage": completed.get("usage", {})}


def _multipart_request(url, token, fields, files, timeout=180):
    """Send a bounded multipart request without exposing file contents in errors."""
    boundary = "repostbot-" + secrets.token_hex(16)
    chunks = []
    for name, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                       f'{value}\r\n').encode("utf-8"))
    for field, filename, mime, raw in files:
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                       f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8"))
        chunks.append(raw)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(chunks)
    headers = {"User-Agent": "RepostBot", "Accept": "application/json",
               "Authorization": "Bearer " + token,
               "Content-Type": "multipart/form-data; boundary=" + boundary}
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
            raw_response = response.read(24 * 1024 * 1024 + 1)
    except HTTPError as exc:
        raise _api_error(exc) from None
    except (URLError, TimeoutError, OSError):
        raise AIError("OpenAI: не удалось установить защищённое соединение") from None
    if len(raw_response) > 24 * 1024 * 1024:
        raise AIError("OpenAI: слишком большой ответ")
    try:
        return json.loads(raw_response)
    except (ValueError, TypeError):
        raise AIError("OpenAI: сервер вернул некорректный ответ") from None


def _image_result(result):
    items = result.get("data", [])
    if not items or not items[0].get("b64_json"):
        raise AIError("OpenAI не вернул изображение")
    raw = base64.b64decode(items[0]["b64_json"], validate=True)
    if len(raw) > 10 * 1024 * 1024 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AIError("OpenAI вернул неподдерживаемое изображение")
    return raw


def generate_image(token, prompt, model="gpt-image-1", quality="low", reference_images=None):
    if reference_images:
        fields = {
            "model": model, "prompt": prompt[:6000], "n": "1", "size": "1024x1024",
            "quality": quality, "output_format": "png",
        }
        files = [("image[]", filename, mime, raw) for filename, mime, raw in reference_images]
        result = _multipart_request(API + "/images/edits", token, fields, files, timeout=180)
    else:
        result = openai_json(API + "/images/generations", {
            "model": model, "prompt": prompt[:6000], "n": 1, "size": "1024x1024",
            "quality": quality, "output_format": "png",
        }, token, timeout=180)
    return _image_result(result)


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.replace(temporary, path)
    if os.name != "nt":
        path.chmod(0o600)


def _windows_private_bytes(raw, decrypt=False):
    """Protect OAuth credentials with the current Windows user's DPAPI key."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise AIError("Windows не смог защитить или прочитать подключение ChatGPT")
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(ctypes.cast(destination.data, ctypes.c_void_p))


def validate_identity(id_token, client_id, nonce, discovery):
    # A maintained JWT implementation handles all cryptography and JWK rotation.
    try:
        importlib.invalidate_caches()
        import jwt
    except ImportError:
        raise AIError("Для входа ChatGPT перезапустите start_windows.bat; Linux: pip install 'PyJWT[crypto]>=2.10.1,<3'") from None
    if discovery.get("issuer") != ISSUER or not str(discovery.get("jwks_uri", "")).startswith(ISSUER + "/"):
        raise AIError("OpenAI: неверная конфигурация авторизации")
    jwks = openai_json(discovery["jwks_uri"], timeout=15)
    try:
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") != "RS256":
            raise AIError("OpenAI: неподдерживаемая подпись ID token")
        key_data = next(key for key in jwks["keys"] if key.get("kid") == header.get("kid") and key.get("kty") == "RSA")
        key = jwt.PyJWK.from_dict(key_data, algorithm="RS256").key
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=client_id,
                            issuer=ISSUER, leeway=5, options={"require": ["sub", "exp", "iat", "nonce"]})
        if not isinstance(claims["sub"], str) or not claims["sub"] or not hmac.compare_digest(str(claims["nonce"]), nonce):
            raise AIError("OpenAI: неверный nonce или идентификатор аккаунта")
        return claims
    except AIError:
        raise
    except Exception:
        raise AIError("OpenAI: не удалось проверить подпись или данные аккаунта") from None


class ChatGPTAccount:
    """Local OAuth registrations, isolated from the panel's admin login."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "chatgpt-accounts.json"
        self.lock = threading.RLock()
        self.accounts = {"accounts": {}, "active": "", "host_id": "urn:uuid:" + str(uuid.uuid4())}
        self.last_error = ""
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("encryption") == "windows-dpapi":
                    if os.name != "nt":
                        raise AIError("Подключение ChatGPT защищено Windows. На новом компьютере войдите в аккаунт заново.")
                    saved = json.loads(_windows_private_bytes(base64.b64decode(saved["data"]), decrypt=True))
                if not isinstance(saved.get("accounts"), dict) or "active" not in saved or "host_id" not in saved:
                    raise AIError("Повреждено сохранённое подключение ChatGPT. Войдите повторно.")
                self.accounts = saved
            except Exception as exc:
                self.last_error = str(exc) if isinstance(exc, AIError) else "Не удалось прочитать подключение ChatGPT. Войдите повторно."
        self.pending = None
        self.listener = None
        self.catalog = []

    def save(self):
        value = self.accounts
        if os.name == "nt":
            raw = json.dumps(value, ensure_ascii=False).encode()
            value = {"encryption": "windows-dpapi", "data": base64.b64encode(_windows_private_bytes(raw)).decode()}
        _atomic_json(self.path, value)

    def status(self):
        with self.lock:
            records = [{"id": k, "label": v.get("email", "Аккаунт") + " · " + k[-6:],
                        "connected": bool(v.get("access_token"))} for k, v in self.accounts["accounts"].items()]
            active = self.accounts["accounts"].get(self.accounts["active"], {})
            return {"connected": bool(active.get("access_token")), "active": self.accounts["active"],
                    "accounts": records, "models": self.catalog, "error": self.last_error,
                    "pending": bool(self.pending and self.pending["expires_at"] > time.time())}

    def select(self, account_id):
        with self.lock:
            if account_id not in self.accounts["accounts"]:
                raise ValueError("Аккаунт не найден")
            self.accounts["active"] = account_id
            self.catalog = []
            self.save()

    def begin(self, account_id=""):
        # Fail before opening consent if verification dependencies are missing.
        try:
            importlib.invalidate_caches()
            import jwt  # noqa: F401
        except ImportError:
            raise AIError("Перезапустите start_windows.bat для установки подключения ChatGPT") from None
        with self.lock:
            if self.pending and self.pending["expires_at"] > time.time():
                raise AIError("Вход уже открыт. Завершите его или нажмите «Отменить вход».")
            old = self.accounts["accounts"].get(account_id, {})
            if account_id and not old:
                raise ValueError("Аккаунт не найден")
            discovery = openai_json(ISSUER + "/.well-known/openid-configuration", timeout=15)
            if discovery.get("issuer") != ISSUER:
                raise AIError("Неверный сервер авторизации OpenAI")
            manager = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Authorization code and state must never enter logs.

                def do_GET(self):
                    if urlsplit(self.path).path != "/auth/callback":
                        self.send_error(404)
                        return
                    try:
                        manager.finish(parse_qs(urlsplit(self.path).query))
                        message = "ChatGPT подключён. Вернитесь в панель RepostBot."
                        status = 200
                    except Exception as exc:
                        manager.last_error = str(exc)
                        message = "Подключение не выполнено. Посмотрите причину в панели RepostBot."
                        status = 400
                    raw = ("<!doctype html><meta charset=utf-8><title>RepostBot</title><p>" + message + "</p>").encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            listener = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
            verifier = secrets.token_urlsafe(48)
            state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            redirect = f"http://127.0.0.1:{listener.server_port}/auth/callback"
            self.pending = {"state": state, "nonce": nonce, "verifier": verifier, "redirect": redirect,
                            "client_id": old.get("client_id", "dynamic_agent_client"),
                            "subject": old.get("subject", ""), "expires_at": time.time() + 600,
                            "discovery": discovery}
            self.listener = listener
            self.last_error = ""
            try:
                self.save()  # Persist host id before first consent.
            except Exception:
                self.pending = None
                self.listener = None
                listener.server_close()
                raise
            params = {"client_id": self.pending["client_id"], "ext_agent_host_id": self.accounts["host_id"],
                      "response_type": "code", "redirect_uri": redirect, "scope": SCOPE, "resource": API,
                      "state": state, "nonce": nonce, "code_challenge_method": "S256",
                      "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
            if old.get("id_token"):
                params["id_token_hint"] = old["id_token"]
            if self.pending["client_id"] == "dynamic_agent_client":
                params["agent_name_hint"] = "RepostBot"
            threading.Thread(target=listener.serve_forever, daemon=True).start()
            timer = threading.Timer(600, lambda: self.cancel_if(listener))
            timer.daemon = True
            timer.start()
            return {"url": ISSUER + "/api/accounts/authorize?" + urlencode(params), "expires_in": 600}

    def cancel_if(self, listener):
        with self.lock:
            if self.listener is not listener:
                return
        self.cancel()

    def cancel(self):
        with self.lock:
            listener, self.listener = self.listener, None
            self.pending = None
        if listener:
            listener.shutdown()
            listener.server_close()

    def complete_from_url(self, callback_url):
        """Complete loopback consent from an authenticated remote panel."""
        value = str(callback_url or "").strip()
        if not value or len(value) > 8192 or any(ord(char) < 32 for char in value):
            raise ValueError("Вставьте полный адрес возврата из вкладки входа ChatGPT")
        with self.lock:
            pending = self.pending
            if not pending or pending["expires_at"] <= time.time():
                raise AIError("Нет активного входа. Нажмите Continue with ChatGPT заново.")
            try:
                parsed = urlsplit(value)
                expected = urlsplit(pending["redirect"])
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
            except ValueError:
                raise ValueError("Некорректный адрес возврата ChatGPT") from None
            if ((parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path)
                    or parsed.fragment or any(len(values) != 1 for values in query.values())):
                raise ValueError("Адрес возврата не совпадает с текущим входом ChatGPT")
            # finish checks single-use state, PKCE, signed identity and account binding.
            self.finish(query)
            self.last_error = ""
        return {"ok": True}

    def finish(self, query):
        with self.lock:
            pending = self.pending
            state = query.get("state", [""])[0]
            if not pending or pending["expires_at"] <= time.time() or not hmac.compare_digest(pending["state"], state):
                raise AIError("Неверный или истёкший запрос входа")
            self.pending = None  # Single-use, including declined authorization.
            listener = self.listener
            if listener:
                threading.Thread(target=lambda: self.cancel_if(listener), daemon=True).start()
            if query.get("error"):
                raise AIError("Вход отменён или доступ к тарифу ChatGPT не разрешён")
            client_id = query.get("client_id", [pending["client_id"]])[0]
            if client_id == "dynamic_agent_client" or not client_id:
                raise AIError("OpenAI не вернул client_id приложения")
            if pending["client_id"] != "dynamic_agent_client" and client_id != pending["client_id"]:
                raise AIError("Возвращён client_id другого аккаунта")
            code = query.get("code", [""])[0]
            if not code:
                raise AIError("OpenAI не вернул код входа")
            tokens = openai_json(ISSUER + "/api/accounts/oauth/token", {
                "grant_type": "authorization_code", "client_id": client_id, "code": code,
                "code_verifier": pending["verifier"], "redirect_uri": pending["redirect"], "resource": API,
            }, form=True, timeout=20)
            claims = validate_identity(tokens.get("id_token", ""), client_id, pending["nonce"], pending["discovery"])
            if pending["subject"] and pending["subject"] != claims["sub"]:
                raise AIError("Выбран другой аккаунт; добавьте его отдельным подключением")
            scopes = str(tokens.get("scope", "")).split()
            if "chatgpt.tokens.use.direct" not in scopes or not tokens.get("access_token"):
                raise AIError("Аккаунт не разрешил использование тарифа ChatGPT")
            record = dict(tokens, subject=claims["sub"], email=claims.get("email", "Аккаунт"),
                          client_id=client_id, scopes=scopes, expires_at=time.time() + int(tokens.get("expires_in", 3600)))
            self.accounts["accounts"][client_id] = record
            self.accounts["active"] = client_id
            self.catalog = []
            self.save()

    def access_token(self):
        with self.lock:
            record = self.accounts["accounts"].get(self.accounts["active"], {})
            if not record.get("access_token"):
                raise AIError("Сначала подключите аккаунт ChatGPT")
            if float(record.get("expires_at", 0)) <= time.time() + 60:
                if not record.get("refresh_token"):
                    raise AIError("Подключите аккаунт ChatGPT повторно")
                tokens = openai_json(ISSUER + "/api/accounts/oauth/token", {
                    "grant_type": "refresh_token", "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"], "resource": API,
                }, form=True, timeout=20)
                scopes = str(tokens.get("scope", " ".join(record["scopes"]))).split()
                if not tokens.get("access_token") or "chatgpt.tokens.use.direct" not in scopes:
                    raise AIError("Нет разрешения на использование тарифа ChatGPT")
                record.update(tokens, scopes=scopes, expires_at=time.time() + int(tokens.get("expires_in", 3600)))
                self.save()
            return record["access_token"]

    def models(self):
        with self.lock:
            data = openai_json(API + "/models", token=self.access_token(), timeout=15)
            self.catalog = [{"id": m["slug"], "name": m.get("display_name", m["slug"])}
                            for m in data.get("models", []) if m.get("visibility") == "list" and m.get("slug")]
            if not self.catalog:
                raise AIError("Аккаунт не вернул доступных моделей")
            return self.catalog

    def logout(self, account_id):
        with self.lock:
            record = self.accounts["accounts"].get(account_id)
            if not record:
                raise ValueError("Аккаунт не найден")
            warning = ""
            if record.get("refresh_token"):
                try:
                    discovery = openai_json(ISSUER + "/.well-known/openid-configuration", timeout=15)
                    endpoint = discovery["revocation_endpoint"]
                    if not endpoint.startswith(ISSUER + "/"):
                        raise AIError("Неверный адрес отзыва токена")
                    openai_json(endpoint, {"token": record["refresh_token"], "token_type_hint": "refresh_token",
                                         "client_id": record["client_id"]}, form=True, timeout=15, allow_empty=True)
                except Exception:
                    warning = "Локальный доступ удалён. Отзыв на сервере не подтверждён; отключите RepostBot в настройках ChatGPT."
            # Remove the local registration; otherwise every reauthorization
            # leaves a dead row in the selector forever.
            self.accounts["accounts"].pop(account_id, None)
            if self.accounts["active"] == account_id:
                connected = [key for key, value in self.accounts["accounts"].items() if value.get("access_token")]
                self.accounts["active"] = connected[-1] if connected else next(iter(self.accounts["accounts"]), "")
            self.catalog = []
            self.save()
            return {"ok": True, "warning": warning, "removed": account_id}
