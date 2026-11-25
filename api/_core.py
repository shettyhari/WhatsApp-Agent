"""Shared logic for the EBG WhatsApp Agent app.

Used by both:
  - server.py        (runs on a PC; data in data.json / sessions.json)
  - api/index.py     (Vercel serverless function; data in Upstash Redis)

The leading underscore stops Vercel from turning this file into its own endpoint.
"""
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

MIN_PASSWORD_LEN = 8
SESSION_TTL_SECONDS = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 5 * 60
MAX_BODY_BYTES = 5 * 1024 * 1024
ROLES = {"Admin", "Member"}
WEBHOOK_PATH_RE = re.compile(r"^/api/whatsapp/webhook/(agt_[a-z0-9]{6,32})$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def empty_data():
    return {"users": {}, "agents": [], "agentChats": {}}


def normalize_data(data):
    data = data if isinstance(data, dict) else {}
    for key, default in (("users", {}), ("agents", []), ("agentChats", {})):
        if not isinstance(data.get(key), type(default)):
            data[key] = default
    data["agents"] = [a for a in data["agents"] if isinstance(a, dict) and a.get("id")]
    return data


# ---------------------------------------------------------------------------
# Passwords and sessions
# ---------------------------------------------------------------------------
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def token_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------
class FileStorage:
    """data.json + sessions.json on disk (for running server.py on a PC)."""

    def __init__(self, directory):
        self.data_file = os.path.join(directory, "data.json")
        self.sessions_file = os.path.join(directory, "sessions.json")
        self.lock = threading.RLock()
        self.failures = {}
        try:
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            self.sessions = {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("expires", 0) > now}
        except (FileNotFoundError, ValueError):
            self.sessions = {}

    @staticmethod
    def _write(path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def read(self):
        with self.lock:
            if not os.path.exists(self.data_file):
                return empty_data()
            with open(self.data_file, "r", encoding="utf-8") as f:
                return normalize_data(json.load(f))

    def transact(self, fn):
        with self.lock:
            data = self.read()
            result = fn(data)
            if result.changed:
                self._write(self.data_file, data)
            return result

    def _save_sessions(self):
        self._write(self.sessions_file, self.sessions)

    def create_session(self, email):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token_key(token)] = {"email": email, "expires": time.time() + SESSION_TTL_SECONDS}
            self._save_sessions()
        return token

    def session_email(self, token):
        if not token:
            return None
        with self.lock:
            key = token_key(token)
            sess = self.sessions.get(key)
            if sess and sess["expires"] < time.time():
                del self.sessions[key]
                self._save_sessions()
                return None
            return sess["email"] if sess else None

    def delete_session(self, token):
        if not token:
            return
        with self.lock:
            if self.sessions.pop(token_key(token), None):
                self._save_sessions()

    def revoke_email(self, email, keep_token=None):
        keep = token_key(keep_token) if keep_token else None
        with self.lock:
            before = len(self.sessions)
            self.sessions = {k: v for k, v in self.sessions.items() if v["email"] != email or k == keep}
            if len(self.sessions) != before:
                self._save_sessions()

    def locked_seconds(self, key):
        rec = self.failures.get(key)
        return max(0, int(rec["until"] - time.time())) if rec and rec["until"] > time.time() else 0

    def record_failure(self, key):
        with self.lock:
            rec = self.failures.setdefault(key, {"count": 0, "until": 0})
            rec["count"] += 1
            if rec["count"] >= MAX_FAILED_LOGINS:
                rec.update(count=0, until=time.time() + LOCKOUT_SECONDS)

    def clear_failures(self, email, ip=None):
        with self.lock:
            for k in [k for k in self.failures if k[0] == email and (ip is None or k[1] == ip)]:
                self.failures.pop(k, None)


class RedisStorage:
    """Upstash Redis over its REST API (for Vercel). No extra Python packages needed."""

    CAS_SCRIPT = (
        "local cur = redis.call('GET', KEYS[2]) or '0' "
        "if cur == ARGV[1] then "
        "redis.call('SET', KEYS[1], ARGV[2]) redis.call('SET', KEYS[2], ARGV[3]) return 1 "
        "end return 0"
    )

    def __init__(self, url, token, prefix="ebgwa"):
        self.url = url.rstrip("/")
        self.token = token
        self.k_data = f"{prefix}:data"
        self.k_ver = f"{prefix}:ver"
        self.p = prefix

    def _request(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ApiError(503, f"Database error ({e.code}). Check the Upstash Redis connection in Vercel.")
        except urllib.error.URLError:
            raise ApiError(503, "Cannot reach the database. Check the Upstash Redis connection in Vercel.")

    def cmd(self, *args):
        res = self._request("", [str(a) for a in args])
        if "error" in res:
            raise ApiError(503, "Database error: " + str(res["error"]))
        return res.get("result")

    def pipeline(self, commands):
        res = self._request("/pipeline", [[str(a) for a in c] for c in commands])
        return [r.get("result") for r in res]

    def _read_with_version(self):
        raw, ver = self.cmd("MGET", self.k_data, self.k_ver)
        data = normalize_data(json.loads(raw)) if raw else empty_data()
        return data, ver or "0"

    def read(self):
        return self._read_with_version()[0]

    def transact(self, fn, attempts=25):
        for attempt in range(attempts):
            data, ver = self._read_with_version()
            result = fn(data)
            if not result.changed:
                return result
            new_ver = str(int(ver) + 1)
            ok = self.cmd("EVAL", self.CAS_SCRIPT, 2, self.k_data, self.k_ver, ver,
                          json.dumps(data, ensure_ascii=False), new_ver)
            if int(ok or 0) == 1:
                return result
            time.sleep(random.uniform(0.01, 0.05) * min(attempt + 1, 6))
        raise ApiError(409, "The app is busy. Please try again.")

    def _sk(self, key):
        return f"{self.p}:sess:{key}"

    def _ik(self, email):
        return f"{self.p}:sessidx:{email}"

    def create_session(self, email):
        token = secrets.token_urlsafe(32)
        key = token_key(token)
        self.pipeline([
            ["SET", self._sk(key), email, "EX", SESSION_TTL_SECONDS],
            ["SADD", self._ik(email), key],
            ["EXPIRE", self._ik(email), SESSION_TTL_SECONDS],
        ])
        return token

    def session_email(self, token):
        return self.cmd("GET", self._sk(token_key(token))) if token else None

    def delete_session(self, token):
        if not token:
            return
        key = token_key(token)
        email = self.cmd("GET", self._sk(key))
        cmds = [["DEL", self._sk(key)]]
        if email:
            cmds.append(["SREM", self._ik(email), key])
        self.pipeline(cmds)

    def revoke_email(self, email, keep_token=None):
        keep = token_key(keep_token) if keep_token else None
        keys = self.cmd("SMEMBERS", self._ik(email)) or []
        cmds = [["DEL", self._sk(k)] for k in keys if k != keep]
        cmds += [["SREM", self._ik(email), k] for k in keys if k != keep]
        if cmds:
            self.pipeline(cmds)

    def _fk(self, key):
        return f"{self.p}:fail:{key[0]}:{key[1]}"

    def _lk(self, key):
        return f"{self.p}:lock:{key[0]}:{key[1]}"

    def locked_seconds(self, key):
        ttl = self.cmd("TTL", self._lk(key))
        return int(ttl) if ttl and int(ttl) > 0 else 0

    def record_failure(self, key):
        count, _ = self.pipeline([["INCR", self._fk(key)], ["EXPIRE", self._fk(key), 15 * 60]])
        if int(count) >= MAX_FAILED_LOGINS:
            self.pipeline([["SET", self._lk(key), 1, "EX", LOCKOUT_SECONDS], ["DEL", self._fk(key)]])

    def clear_failures(self, email, ip=None):
        if ip is not None:
            self.pipeline([["DEL", self._fk((email, ip))], ["DEL", self._lk((email, ip))]])
        else:
            keys = (self.cmd("KEYS", f"{self.p}:fail:{email}:*") or []) + (self.cmd("KEYS", f"{self.p}:lock:{email}:*") or [])
            if keys:
                self.cmd("DEL", *keys)


# (URL setting, token setting) name endings, in order of preference. Vercel may add a custom
# prefix when a database is connected (e.g. STORAGE_KV_REST_API_URL), so match on the ending.
REDIS_SETTING_PAIRS = (("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"), ("KV_REST_API_URL", "KV_REST_API_TOKEN"))


def find_redis_settings(env=None):
    """Returns (url, token) from the environment, or (None, None). Unprefixed names win."""
    env = os.environ if env is None else env
    for url_end, token_end in REDIS_SETTING_PAIRS:
        candidates = sorted((k for k in env if k.endswith(url_end) and env[k]), key=lambda k: (len(k), k))
        for url_key in candidates:
            token_key_name = url_key[:-len(url_end)] + token_end
            if env.get(token_key_name):
                return env[url_key], env[token_key_name]
    return None, None


def database_setup_problem(env=None):
    """Explains why no database settings were found, naming (never showing) related settings."""
    env = os.environ if env is None else env
    related = sorted(k for k in env if any(w in k.upper() for w in ("KV_", "REDIS", "UPSTASH")))
    msg = "The database is not connected: this deployment has no Upstash Redis REST URL and token settings."
    if not related:
        return msg + " In Vercel, connect an Upstash for Redis database to this project (Storage tab), then redeploy (see README.md)."
    msg += f" Settings found: {', '.join(related[:12])}."
    if any(k.endswith(("REDIS_URL", "KV_URL")) for k in related) and not any("REST" in k for k in related):
        msg += " Only a redis:// connection string is set; this app needs the REST URL and token (…_KV_REST_API_URL / …_KV_REST_API_TOKEN)."
    else:
        msg += " Each URL setting needs a matching token setting with the same prefix (for example X_KV_REST_API_URL and X_KV_REST_API_TOKEN)."
    return msg + " Redeploy after changing settings."


def storage_from_env():
    url, token = find_redis_settings()
    if not url or not token:
        return None
    return RedisStorage(url, token, os.environ.get("REDIS_PREFIX", "ebgwa"))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def norm_email(email):
    return str(email or "").strip().lower()


def public_user(email, rec):
    return {"email": email, "name": rec.get("name", ""), "role": rec.get("role", "Member"),
            "createdAt": rec.get("createdAt", "")}


def check_new_account(data, email, password, name):
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError(400, "Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LEN:
        raise ApiError(400, f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if not name:
        raise ApiError(400, "Name is required.")
    if email in data["users"]:
        raise ApiError(400, "An account with this email already exists.")


def admin_count(data):
    return sum(1 for u in data["users"].values() if u.get("role") == "Admin")


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------
class Result:
    """Outcome of one transaction attempt. `after` runs only once the data is saved."""

    def __init__(self, status=200, payload=None, changed=False):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.changed = changed
        self.after = []


class Caller:
    def __init__(self, token, email, user):
        self.token = token
        self.email = email
        self.user = user
        self.name = user.get("name") or email
        self.is_admin = user.get("role") == "Admin"


class Api:
    """Transport-independent API. `handle()` returns (http_status, payload)."""

    def __init__(self, storage):
        self.storage = storage

    def agents(self):
        from _agents import AgentRoutes  # imported lazily (it imports from this module)
        return AgentRoutes(self)

    def handle(self, method, path, headers, body_bytes, client_ip, query=None):
        try:
            if self.storage is None:
                raise ApiError(503, database_setup_problem())
            token = ""
            auth = headers.get("Authorization") or headers.get("authorization") or ""
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            if len(body_bytes or b"") > MAX_BODY_BYTES:
                raise ApiError(413, "Request too large")
            # Meta's WhatsApp webhook: public, but every message is signed with the app secret
            match = WEBHOOK_PATH_RE.match(path)
            if match:
                if method == "GET":
                    return self.agents().verify_webhook(match.group(1), query or {})
                if method == "POST":
                    return self.agents().receive_webhook(match.group(1), headers, body_bytes)
            if method == "GET":
                return self.get(path, token)
            if method == "POST":
                try:
                    body = json.loads((body_bytes or b"{}").decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    raise ApiError(400, "Invalid JSON")
                if not isinstance(body, dict):
                    raise ApiError(400, "Request body must be a JSON object")
                return self.post(path, token, body, client_ip)
            raise ApiError(405, "Method not allowed")
        except ApiError as e:
            return e.status, {"success": False, "error": e.message}
        except Exception as e:
            print("API error:", repr(e))
            return 500, {"success": False, "error": "Server error"}

    def caller(self, data, token):
        email = self.storage.session_email(token)
        if not email:
            raise ApiError(401, "Please sign in again.")
        user = data["users"].get(email)
        if not user:
            self.storage.delete_session(token)
            raise ApiError(401, "This account no longer exists.")
        return Caller(token, email, user)

    def run(self, fn):
        result = self.storage.transact(fn)
        for effect in result.after:
            effect()
        return result.status, result.payload

    # ---- GET ---------------------------------------------------------------
    def get(self, path, token):
        if path == "/api/bootstrap":
            data = self.storage.read()
            return 200, {"server": "ebg-whatsapp-agent", "hasUsers": bool(data["users"])}
        if path == "/api/me":
            data = self.storage.read()
            ctx = self.caller(data, token)
            return 200, {"success": True, "user": public_user(ctx.email, ctx.user)}
        if path == "/api/users":
            data = self.storage.read()
            if not self.caller(data, token).is_admin:
                raise ApiError(403, "Only an Admin can manage the team.")
            return 200, {"success": True, "users": self.user_list(data)}
        if path == "/api/agents":
            return self.agents().list(token)
        raise ApiError(404, "Not found")

    # ---- POST --------------------------------------------------------------
    def post(self, path, token, body, client_ip):
        if path == "/api/login":
            return self.login(body, client_ip)
        if path == "/api/setup":
            return self.setup(body)
        if path == "/api/logout":
            self.storage.delete_session(token)
            return 200, {"success": True}
        if path == "/api/change-password":
            return self.change_password(body, token)
        if path == "/api/users":
            return self.manage_users(body, token)
        if path == "/api/agents":
            return self.agents().action(token, body)
        raise ApiError(404, "Not found")

    @staticmethod
    def user_list(data):
        return sorted((public_user(e, u) for e, u in data["users"].items()), key=lambda u: u["name"].lower())

    def login(self, body, client_ip):
        email = norm_email(body.get("email"))
        password = str(body.get("password") or "")
        key = (email, client_ip)
        wait = self.storage.locked_seconds(key)
        if wait:
            raise ApiError(429, f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).")
        data = self.storage.read()
        user = data["users"].get(email)
        if not user or not verify_password(password, user.get("passwordHash", "")):
            self.storage.record_failure(key)
            raise ApiError(401, "Incorrect email or password.")
        self.storage.clear_failures(email, client_ip)
        return 200, {"success": True, "token": self.storage.create_session(email), "user": public_user(email, user)}

    def setup(self, body):
        """Creates the first account (an Admin). After that, Admins add teammates."""
        email = norm_email(body.get("email"))
        password = str(body.get("password") or "")
        name = str(body.get("name") or "").strip()[:120]
        password_hash = hash_password(password)

        def fn(data):
            if data["users"]:
                raise ApiError(403, "This app is already set up. Ask an Admin to add you.")
            check_new_account(data, email, password, name)
            data["users"][email] = {"name": name, "role": "Admin", "passwordHash": password_hash, "createdAt": now_iso()}
            return Result(200, {"success": True}, changed=True)

        self.storage.transact(fn)
        user = self.storage.read()["users"][email]
        return 200, {"success": True, "token": self.storage.create_session(email), "user": public_user(email, user)}

    def change_password(self, body, token):
        new = str(body.get("newPassword") or "")
        if len(new) < MIN_PASSWORD_LEN:
            raise ApiError(400, f"New password must be at least {MIN_PASSWORD_LEN} characters.")
        new_hash = hash_password(new)

        def fn(data):
            ctx = self.caller(data, token)
            if not verify_password(str(body.get("currentPassword") or ""), ctx.user.get("passwordHash", "")):
                raise ApiError(400, "Your current password is incorrect.")
            ctx.user["passwordHash"] = new_hash
            res = Result(200, {"success": True}, changed=True)
            res.after.append(lambda: self.storage.revoke_email(ctx.email, keep_token=token))
            return res

        return self.run(fn)

    def manage_users(self, body, token):
        action = str(body.get("action") or "")
        email = norm_email(body.get("email"))
        password = str(body.get("password") or "")
        password_hash = hash_password(password) if password else ""

        def fn(data):
            ctx = self.caller(data, token)
            if not ctx.is_admin:
                raise ApiError(403, "Only an Admin can manage the team.")
            res = Result(200, changed=True)
            if action == "add":
                name = str(body.get("name") or "").strip()[:120]
                check_new_account(data, email, password, name)
                role = body.get("role") if body.get("role") in ROLES else "Member"
                data["users"][email] = {"name": name, "role": role, "passwordHash": password_hash,
                                        "createdAt": now_iso(), "addedBy": ctx.name}
            else:
                user = data["users"].get(email)
                if not user:
                    raise ApiError(404, "No teammate with that email.")
                if action == "remove":
                    if email == ctx.email:
                        raise ApiError(400, "You can't remove your own account.")
                    del data["users"][email]
                    res.after.append(lambda: self.storage.revoke_email(email))
                elif action == "setRole":
                    role = body.get("role")
                    if role not in ROLES:
                        raise ApiError(400, "Unknown role.")
                    if email == ctx.email and role != "Admin" and admin_count(data) == 1:
                        raise ApiError(400, "Keep at least one Admin.")
                    user["role"] = role
                elif action == "resetPassword":
                    if len(password) < MIN_PASSWORD_LEN:
                        raise ApiError(400, f"Password must be at least {MIN_PASSWORD_LEN} characters.")
                    user["passwordHash"] = password_hash
                    res.after.append(lambda: self.storage.revoke_email(email, keep_token=token if email == ctx.email else None))
                    res.after.append(lambda: self.storage.clear_failures(email))
                else:
                    raise ApiError(400, "Unknown action.")
            res.payload = {"success": True, "users": self.user_list(data)}
            return res

        return self.run(fn)
