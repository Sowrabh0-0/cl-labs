from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pymysql
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Cookie, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


APP_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = APP_ROOT / "data"
DB_PATH = Path(os.getenv("APP_DB_PATH", str(DATA_DIR / "app.db")))
SEED_PATH = Path(os.getenv("APP_SEED_PATH", str(DATA_DIR / "seed.json")))
SESSION_COOKIE = "clahan_session"
SESSION_MAX_SECONDS = 4 * 60 * 60
IDLE_TIMEOUT_SECONDS = 15 * 60
PBKDF2_ITERATIONS = 260_000


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    isAdmin: bool = False
    vmId: str | None = None


class CreateVmRequest(BaseModel):
    id: str = Field(min_length=2, max_length=96)
    name: str = Field(min_length=2, max_length=128)
    host: str = Field(min_length=2, max_length=256)
    protocol: str = "rdp"
    status: str = "manual-ready"
    guacamoleConnectionId: str
    guacamoleLaunchUrl: str | None = None
    rdpUsername: str | None = Field(default=None, max_length=128)
    rdpPassword: str | None = Field(default=None, max_length=256)
    rdpDomain: str | None = Field(default=None, max_length=128)
    security: str = "any"
    ignoreCert: bool = True


class UpdateVmRequest(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    host: str = Field(min_length=2, max_length=256)
    protocol: str = "rdp"
    status: str = "manual-ready"
    guacamoleConnectionId: str
    guacamoleLaunchUrl: str | None = None
    rdpUsername: str | None = Field(default=None, max_length=128)
    rdpPassword: str | None = Field(default=None, max_length=256)
    rdpDomain: str | None = Field(default=None, max_length=128)
    security: str = "any"
    ignoreCert: bool = True


class AssignVmRequest(BaseModel):
    vmId: str | None = None


class ResetUserPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class VmSummary(BaseModel):
    id: str
    name: str
    host: str
    protocol: str
    status: str
    guacamoleConnectionId: str
    guacamoleLaunchUrl: str
    rdpUsername: str | None = None
    rdpDomain: str | None = None
    security: str = "any"
    ignoreCert: bool = True


class UserSummary(BaseModel):
    id: int
    username: str
    isAdmin: bool
    vmId: str | None
    vmName: str | None
    createdAt: int


class GuacamoleSyncSummary(BaseModel):
    enabled: bool
    status: str


class SessionSummary(BaseModel):
    username: str
    isAdmin: bool
    vm: VmSummary | None
    expiresAt: int
    idleExpiresAt: int


class GuacamoleLaunchResponse(BaseModel):
    launchUrl: str
    expiresAt: int


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def now() -> int:
    return int(time.time())


def get_secret() -> bytes:
    secret = os.getenv("APP_SESSION_SECRET", "dev-change-me")
    return secret.encode("utf-8")


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_payload(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_part = b64url_encode(payload_json)
    signature = hmac.new(get_secret(), payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{b64url_encode(signature)}"


def verify_token(token: str) -> dict[str, Any]:
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc

    expected = hmac.new(get_secret(), payload_part.encode("ascii"), hashlib.sha256).digest()
    supplied = b64url_decode(signature_part)
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    payload = json.loads(b64url_decode(payload_part))
    if int(payload.get("exp", 0)) < now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return payload


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${b64url_encode(salt)}${b64url_encode(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False

    salt = b64url_decode(salt_text)
    expected = b64url_decode(digest_text)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
    return hmac.compare_digest(expected, actual)


def guacamole_sync_enabled() -> bool:
    return os.getenv("GUACAMOLE_SYNC_ENABLED", "false").lower() == "true"


def get_guacamole_public_url() -> str:
    return os.getenv("GUACAMOLE_PUBLIC_URL", "/guacamole").rstrip("/") or "/guacamole"


def connect_guacamole_db() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("GUACAMOLE_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("GUACAMOLE_MYSQL_PORT", "3306")),
        user=os.getenv("GUACAMOLE_MYSQL_USER", "guacamole_user"),
        password=os.getenv("GUACAMOLE_MYSQL_PASSWORD", ""),
        database=os.getenv("GUACAMOLE_MYSQL_DATABASE", "guacamole_db"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def hash_guacamole_password(password: str) -> tuple[bytes, bytes]:
    salt = secrets.token_bytes(32)
    # Guacamole JDBC auth stores SHA256(password + HEX(salt)) as binary.
    digest = hashlib.sha256(f"{password}{salt.hex().upper()}".encode("utf-8")).digest()
    return digest, salt


def ensure_guacamole_user(cursor: pymysql.cursors.Cursor, username: str, password: str) -> int:
    cursor.execute(
        "SELECT entity_id FROM guacamole_entity WHERE name = %s AND type = 'USER'",
        (username,),
    )
    entity = cursor.fetchone()
    if entity:
        entity_id = int(entity["entity_id"])
    else:
        cursor.execute(
            "INSERT INTO guacamole_entity (name, type) VALUES (%s, 'USER')",
            (username,),
        )
        entity_id = int(cursor.lastrowid)

    password_hash, password_salt = hash_guacamole_password(password)
    cursor.execute(
        """
        INSERT INTO guacamole_user (entity_id, password_hash, password_salt, password_date, disabled, expired)
        VALUES (%s, %s, %s, UTC_TIMESTAMP(), 0, 0)
        ON DUPLICATE KEY UPDATE
            password_hash = VALUES(password_hash),
            password_salt = VALUES(password_salt),
            password_date = UTC_TIMESTAMP(),
            disabled = 0,
            expired = 0
        """,
        (entity_id, password_hash, password_salt),
    )
    return entity_id


def ensure_guacamole_connection(cursor: pymysql.cursors.Cursor, vm: VmSummary) -> int:
    cursor.execute(
        "SELECT connection_id FROM guacamole_connection WHERE connection_name = %s",
        (vm.guacamoleConnectionId,),
    )
    connection = cursor.fetchone()
    if connection:
        connection_id = int(connection["connection_id"])
        cursor.execute(
            "UPDATE guacamole_connection SET protocol = %s WHERE connection_id = %s",
            (vm.protocol.lower(), connection_id),
        )
    else:
        cursor.execute(
            "INSERT INTO guacamole_connection (connection_name, protocol) VALUES (%s, %s)",
            (vm.guacamoleConnectionId, vm.protocol.lower()),
        )
        connection_id = int(cursor.lastrowid)

    params = {
        "hostname": vm.host,
        "port": "3389" if vm.protocol.lower() == "rdp" else "",
        "security": vm.security,
        "ignore-cert": "true" if vm.ignoreCert else "false",
        "username": vm.rdpUsername or "",
        "domain": vm.rdpDomain or "",
    }
    for name, value in params.items():
        if not value:
            continue
        cursor.execute(
            """
            INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE parameter_value = VALUES(parameter_value)
            """,
            (connection_id, name, value),
        )

    return connection_id


def sync_guacamole_connection_password(connection_name: str, rdp_password: str | None) -> None:
    if not guacamole_sync_enabled() or not rdp_password:
        return

    with connect_guacamole_db() as guac_db:
        with guac_db.cursor() as cursor:
            cursor.execute(
                "SELECT connection_id FROM guacamole_connection WHERE connection_name = %s",
                (connection_name,),
            )
            connection = cursor.fetchone()
            if not connection:
                return
            cursor.execute(
                """
                INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
                VALUES (%s, 'password', %s)
                ON DUPLICATE KEY UPDATE parameter_value = VALUES(parameter_value)
                """,
                (int(connection["connection_id"]), rdp_password),
            )
        guac_db.commit()


def get_guacamole_connection_password(connection_name: str) -> str | None:
    if not guacamole_sync_enabled():
        return None

    with connect_guacamole_db() as guac_db:
        with guac_db.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.parameter_value
                FROM guacamole_connection c
                JOIN guacamole_connection_parameter p ON p.connection_id = c.connection_id
                WHERE c.connection_name = %s
                  AND p.parameter_name = 'password'
                LIMIT 1
                """,
                (connection_name,),
            )
            row = cursor.fetchone()
            return str(row["parameter_value"]) if row else None


def get_json_secret_key() -> bytes:
    secret = os.getenv("GUACAMOLE_JSON_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Guacamole JSON auth secret is not configured",
        )
    try:
        key = bytes.fromhex(secret)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Guacamole JSON auth secret must be a 32-character hex value",
        ) from exc
    if len(key) != 16:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Guacamole JSON auth secret must be 128-bit / 16 bytes",
        )
    return key


def encrypt_guacamole_json(payload: dict[str, Any]) -> str:
    key = get_json_secret_key()
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key, plaintext, hashlib.sha256).digest()
    signed_payload = signature + plaintext

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded_payload = padder.update(signed_payload) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(bytes(16)))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded_payload) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def build_guacamole_launch_url(username: str, vm: VmSummary, app_expires_at: int) -> GuacamoleLaunchResponse:
    rdp_password = get_guacamole_connection_password(vm.guacamoleConnectionId)
    if not vm.rdpUsername or not rdp_password:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="VM RDP credentials are missing. Re-register the VM with RDP username and password.",
        )

    expires_at = min(app_expires_at, now() + 5 * 60)
    params = {
        "hostname": vm.host,
        "port": "3389" if vm.protocol.lower() == "rdp" else "",
        "security": vm.security,
        "ignore-cert": "true" if vm.ignoreCert else "false",
        "username": vm.rdpUsername,
        "password": rdp_password,
    }
    if vm.rdpDomain:
        params["domain"] = vm.rdpDomain

    payload = {
        "username": username,
        "expires": expires_at * 1000,
        "connections": {
            vm.guacamoleConnectionId: {
                "protocol": vm.protocol.lower(),
                "parameters": {key: value for key, value in params.items() if value},
            }
        },
    }
    token = encrypt_guacamole_json(payload)
    return GuacamoleLaunchResponse(
        launchUrl=f"{get_guacamole_public_url()}/?data={quote(token, safe='')}",
        expiresAt=expires_at,
    )


def grant_guacamole_connection(cursor: pymysql.cursors.Cursor, entity_id: int, connection_id: int) -> None:
    cursor.execute(
        """
        INSERT IGNORE INTO guacamole_connection_permission (entity_id, connection_id, permission)
        VALUES (%s, %s, 'READ')
        """,
        (entity_id, connection_id),
    )


def revoke_guacamole_connection(cursor: pymysql.cursors.Cursor, entity_id: int, connection_id: int) -> None:
    cursor.execute(
        """
        DELETE FROM guacamole_connection_permission
        WHERE entity_id = %s AND connection_id = %s AND permission = 'READ'
        """,
        (entity_id, connection_id),
    )


def sync_guacamole_user_mapping(username: str, password: str, vm: VmSummary | None, is_admin: bool) -> None:
    if not guacamole_sync_enabled():
        return

    with connect_guacamole_db() as guac_db:
        with guac_db.cursor() as cursor:
            entity_id = ensure_guacamole_user(cursor, username, password)
            if vm:
                connection_id = ensure_guacamole_connection(cursor, vm)
                grant_guacamole_connection(cursor, entity_id, connection_id)

            if is_admin:
                cursor.execute("SELECT connection_id FROM guacamole_connection")
                for row in cursor.fetchall():
                    grant_guacamole_connection(cursor, entity_id, int(row["connection_id"]))

        guac_db.commit()


def sync_guacamole_assignment(username: str, vm: VmSummary | None, is_admin: bool) -> None:
    if not guacamole_sync_enabled() or not vm:
        return

    with connect_guacamole_db() as guac_db:
        with guac_db.cursor() as cursor:
            cursor.execute(
                "SELECT entity_id FROM guacamole_entity WHERE name = %s AND type = 'USER'",
                (username,),
            )
            entity = cursor.fetchone()
            if not entity:
                return
            entity_id = int(entity["entity_id"])
            connection_id = ensure_guacamole_connection(cursor, vm)
            grant_guacamole_connection(cursor, entity_id, connection_id)
            if not is_admin:
                cursor.execute(
                    """
                    DELETE cp FROM guacamole_connection_permission cp
                    JOIN guacamole_connection c ON c.connection_id = cp.connection_id
                    WHERE cp.entity_id = %s
                      AND cp.permission = 'READ'
                      AND c.connection_name <> %s
                    """,
                    (entity_id, vm.guacamoleConnectionId),
                )
        guac_db.commit()


def sync_guacamole_connection_admin_permissions(admin_usernames: list[str], vm: VmSummary) -> None:
    if not guacamole_sync_enabled():
        return

    with connect_guacamole_db() as guac_db:
        with guac_db.cursor() as cursor:
            connection_id = ensure_guacamole_connection(cursor, vm)
            for username in admin_usernames:
                cursor.execute(
                    "SELECT entity_id FROM guacamole_entity WHERE name = %s AND type = 'USER'",
                    (username,),
                )
                entity = cursor.fetchone()
                if entity:
                    grant_guacamole_connection(cursor, int(entity["entity_id"]), connection_id)
        guac_db.commit()


def vm_assigned_to_other_non_admin(db: sqlite3.Connection, vm_id: str, username: str | None = None) -> sqlite3.Row | None:
    if username:
        return db.execute(
            """
            SELECT username FROM users
            WHERE vm_id = ? AND is_admin = 0 AND username <> ?
            LIMIT 1
            """,
            (vm_id, username),
        ).fetchone()
    return db.execute(
        "SELECT username FROM users WHERE vm_id = ? AND is_admin = 0 LIMIT 1",
        (vm_id,),
    ).fetchone()


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS vms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                protocol TEXT NOT NULL,
                status TEXT NOT NULL,
                guacamole_connection_id TEXT NOT NULL,
                guacamole_launch_url TEXT,
                rdp_username TEXT,
                rdp_domain TEXT,
                security TEXT NOT NULL DEFAULT 'any',
                ignore_cert INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                vm_id TEXT REFERENCES vms(id),
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                idle_expires_at INTEGER NOT NULL,
                revoked_at INTEGER
            );
            """
        )

        existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(vms)").fetchall()}
        migrations = {
            "rdp_username": "ALTER TABLE vms ADD COLUMN rdp_username TEXT",
            "rdp_domain": "ALTER TABLE vms ADD COLUMN rdp_domain TEXT",
            "security": "ALTER TABLE vms ADD COLUMN security TEXT NOT NULL DEFAULT 'any'",
            "ignore_cert": "ALTER TABLE vms ADD COLUMN ignore_cert INTEGER NOT NULL DEFAULT 1",
        }
        for column_name, statement in migrations.items():
            if column_name not in existing_columns:
                db.execute(statement)

        count = db.execute("SELECT COUNT(*) AS total FROM vms").fetchone()["total"]
        if count == 0 and SEED_PATH.exists():
            seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            for vm_id, vm in seed.get("vms", {}).items():
                db.execute(
                    """
                    INSERT INTO vms (
                        id, name, host, protocol, status, guacamole_connection_id,
                        guacamole_launch_url, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        vm_id,
                        vm["name"],
                        vm["host"],
                        vm.get("protocol", "rdp"),
                        vm.get("status", "manual-ready"),
                        vm["guacamoleConnectionId"],
                        vm.get("guacamoleLaunchUrl"),
                        now(),
                    ),
                )


def row_to_vm(row: sqlite3.Row) -> VmSummary:
    guacamole_base_url = os.getenv("GUACAMOLE_PUBLIC_URL", "").rstrip("/")
    fallback_url = f"{guacamole_base_url}/" if guacamole_base_url else ""
    return VmSummary(
        id=row["id"],
        name=row["name"],
        host=row["host"],
        protocol=row["protocol"],
        status=row["status"],
        guacamoleConnectionId=row["guacamole_connection_id"],
        guacamoleLaunchUrl=row["guacamole_launch_url"] or fallback_url,
        rdpUsername=row["rdp_username"],
        rdpDomain=row["rdp_domain"],
        security=row["security"],
        ignoreCert=bool(row["ignore_cert"]),
    )


def get_vm(db: sqlite3.Connection, vm_id: str | None) -> VmSummary | None:
    if not vm_id:
        return None
    row = db.execute("SELECT * FROM vms WHERE id = ?", (vm_id,)).fetchone()
    return row_to_vm(row) if row else None


def issue_session(db: sqlite3.Connection, user: sqlite3.Row, response: Response) -> SessionSummary:
    current_time = now()
    expires_at = current_time + SESSION_MAX_SECONDS
    idle_expires_at = current_time + IDLE_TIMEOUT_SECONDS
    session_id = secrets.token_urlsafe(32)
    db.execute(
        """
        INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at, idle_expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, user["id"], current_time, expires_at, current_time, idle_expires_at),
    )

    token = sign_payload({"sid": session_id, "sub": user["username"], "exp": expires_at})
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        max_age=SESSION_MAX_SECONDS,
    )
    return build_session_summary(db, user, expires_at, idle_expires_at)


def get_current_user(session_token: str | None, *, touch: bool = True) -> tuple[sqlite3.Connection, sqlite3.Row, sqlite3.Row]:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")

    payload = verify_token(session_token)
    session_id = payload.get("sid")
    current_time = now()
    db = connect_db()
    session = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session or session["revoked_at"]:
        db.close()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer active")
    if session["expires_at"] < current_time:
        db.close()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    if session["idle_expires_at"] < current_time:
        db.execute("UPDATE sessions SET revoked_at = ? WHERE id = ?", (current_time, session_id))
        db.commit()
        db.close()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Idle timeout reached")

    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user:
        db.close()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")

    if touch:
        new_idle_expires_at = min(session["expires_at"], current_time + IDLE_TIMEOUT_SECONDS)
        db.execute(
            "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? WHERE id = ?",
            (current_time, new_idle_expires_at, session_id),
        )
        db.commit()
        session = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    return db, user, session


def build_session_summary(
    db: sqlite3.Connection, user: sqlite3.Row, expires_at: int, idle_expires_at: int
) -> SessionSummary:
    return SessionSummary(
        username=user["username"],
        isAdmin=bool(user["is_admin"]),
        vm=get_vm(db, user["vm_id"]),
        expiresAt=expires_at,
        idleExpiresAt=idle_expires_at,
    )


def require_admin(session_token: str | None) -> tuple[sqlite3.Connection, sqlite3.Row, sqlite3.Row]:
    db, user, session = get_current_user(session_token)
    if not user["is_admin"]:
        db.close()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return db, user, session


app = FastAPI(title="Clahan Labs VM Gateway", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/setup/status")
def setup_status() -> dict[str, bool]:
    with connect_db() as db:
        total = db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
    return {"needsSetup": total == 0}


@app.post("/api/setup/admin", response_model=SessionSummary)
def setup_admin(request: SetupAdminRequest, response: Response) -> SessionSummary:
    with connect_db() as db:
        total = db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
        if total != 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Setup has already been completed")

        db.execute(
            "INSERT INTO users (username, password_hash, is_admin, vm_id, created_at) VALUES (?, ?, 1, NULL, ?)",
            (request.username, hash_password(request.password), now()),
        )
        user = db.execute("SELECT * FROM users WHERE username = ?", (request.username,)).fetchone()
        sync_guacamole_user_mapping(request.username, request.password, None, True)
        return issue_session(db, user, response)


@app.post("/api/auth/login", response_model=SessionSummary)
def login(request: LoginRequest, response: Response) -> SessionSummary:
    with connect_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (request.username,)).fetchone()
        if not user or not verify_password(request.password, user["password_hash"]):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        return issue_session(db, user, response)


@app.get("/api/session", response_model=SessionSummary)
def session(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> SessionSummary:
    db, user, session_row = get_current_user(session_token)
    try:
        return build_session_summary(db, user, session_row["expires_at"], session_row["idle_expires_at"])
    finally:
        db.close()


@app.post("/api/session/heartbeat", response_model=SessionSummary)
def heartbeat(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> SessionSummary:
    db, user, session_row = get_current_user(session_token)
    try:
        return build_session_summary(db, user, session_row["expires_at"], session_row["idle_expires_at"])
    finally:
        db.close()


@app.post("/api/session/guacamole-launch", response_model=GuacamoleLaunchResponse)
def guacamole_launch(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> GuacamoleLaunchResponse:
    db, user, session_row = get_current_user(session_token)
    try:
        vm = get_vm(db, user["vm_id"])
        if not vm:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No VM assigned to this user")
        return build_guacamole_launch_url(user["username"], vm, int(session_row["expires_at"]))
    finally:
        db.close()


@app.post("/api/auth/logout")
def logout(response: Response, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> dict[str, str]:
    if session_token:
        try:
            payload = verify_token(session_token)
            with connect_db() as db:
                db.execute("UPDATE sessions SET revoked_at = ? WHERE id = ?", (now(), payload.get("sid")))
        except HTTPException:
            pass
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged_out"}


@app.get("/api/admin/users", response_model=list[UserSummary])
def list_users(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> list[UserSummary]:
    db, _, _ = require_admin(session_token)
    try:
        rows = db.execute(
            """
            SELECT users.id, users.username, users.is_admin, users.vm_id, users.created_at, vms.name AS vm_name
            FROM users
            LEFT JOIN vms ON users.vm_id = vms.id
            ORDER BY users.created_at DESC
            """
        ).fetchall()
        return [
            UserSummary(
                id=row["id"],
                username=row["username"],
                isAdmin=bool(row["is_admin"]),
                vmId=row["vm_id"],
                vmName=row["vm_name"],
                createdAt=row["created_at"],
            )
            for row in rows
        ]
    finally:
        db.close()


@app.post("/api/admin/users", response_model=UserSummary)
def create_user(
    request: CreateUserRequest, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)
) -> UserSummary:
    db, _, _ = require_admin(session_token)
    try:
        if request.vmId and not get_vm(db, request.vmId):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="VM does not exist")
        if request.vmId and not request.isAdmin:
            assigned = vm_assigned_to_other_non_admin(db, request.vmId)
            if assigned:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"VM is already assigned to user {assigned['username']}",
                )
        try:
            db.execute(
                """
                INSERT INTO users (username, password_hash, is_admin, vm_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request.username, hash_password(request.password), int(request.isAdmin), request.vmId, now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists") from exc
        db.commit()
        vm = get_vm(db, request.vmId)
        sync_guacamole_user_mapping(request.username, request.password, vm, request.isAdmin)
        row = db.execute(
            """
            SELECT users.id, users.username, users.is_admin, users.vm_id, users.created_at, vms.name AS vm_name
            FROM users
            LEFT JOIN vms ON users.vm_id = vms.id
            WHERE users.username = ?
            """,
            (request.username,),
        ).fetchone()
        return UserSummary(
            id=row["id"],
            username=row["username"],
            isAdmin=bool(row["is_admin"]),
            vmId=row["vm_id"],
            vmName=row["vm_name"],
            createdAt=row["created_at"],
        )
    finally:
        db.close()


@app.put("/api/admin/users/{username}/assignment", response_model=UserSummary)
def assign_user_vm(
    username: str, request: AssignVmRequest, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)
) -> UserSummary:
    db, _, _ = require_admin(session_token)
    try:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if request.vmId and not get_vm(db, request.vmId):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="VM does not exist")
        if request.vmId and not user["is_admin"]:
            assigned = vm_assigned_to_other_non_admin(db, request.vmId, username)
            if assigned:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"VM is already assigned to user {assigned['username']}",
                )
        cursor = db.execute("UPDATE users SET vm_id = ? WHERE username = ?", (request.vmId, username))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        db.commit()
        vm = get_vm(db, request.vmId)
        sync_guacamole_assignment(username, vm, bool(user["is_admin"]))
        row = db.execute(
            """
            SELECT users.id, users.username, users.is_admin, users.vm_id, users.created_at, vms.name AS vm_name
            FROM users
            LEFT JOIN vms ON users.vm_id = vms.id
            WHERE users.username = ?
            """,
            (username,),
        ).fetchone()
        return UserSummary(
            id=row["id"],
            username=row["username"],
            isAdmin=bool(row["is_admin"]),
            vmId=row["vm_id"],
            vmName=row["vm_name"],
            createdAt=row["created_at"],
        )
    finally:
        db.close()


@app.put("/api/admin/users/{username}/password", response_model=UserSummary)
def reset_user_password(
    username: str, request: ResetUserPasswordRequest, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)
) -> UserSummary:
    db, _, _ = require_admin(session_token)
    try:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        db.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(request.password), username),
        )
        db.commit()
        vm = get_vm(db, user["vm_id"])
        sync_guacamole_user_mapping(username, request.password, vm, bool(user["is_admin"]))
        row = db.execute(
            """
            SELECT users.id, users.username, users.is_admin, users.vm_id, users.created_at, vms.name AS vm_name
            FROM users
            LEFT JOIN vms ON users.vm_id = vms.id
            WHERE users.username = ?
            """,
            (username,),
        ).fetchone()
        return UserSummary(
            id=row["id"],
            username=row["username"],
            isAdmin=bool(row["is_admin"]),
            vmId=row["vm_id"],
            vmName=row["vm_name"],
            createdAt=row["created_at"],
        )
    finally:
        db.close()


@app.get("/api/admin/vms", response_model=list[VmSummary])
def list_vms(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> list[VmSummary]:
    db, _, _ = require_admin(session_token)
    try:
        rows = db.execute("SELECT * FROM vms ORDER BY name").fetchall()
        return [row_to_vm(row) for row in rows]
    finally:
        db.close()


@app.post("/api/admin/vms", response_model=VmSummary)
def create_vm(
    request: CreateVmRequest, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)
) -> VmSummary:
    db, _, _ = require_admin(session_token)
    try:
        try:
            db.execute(
                """
                INSERT INTO vms (
                    id, name, host, protocol, status, guacamole_connection_id,
                    guacamole_launch_url, rdp_username, rdp_domain, security,
                    ignore_cert, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.id,
                    request.name,
                    request.host,
                    request.protocol,
                    request.status,
                    request.guacamoleConnectionId,
                    request.guacamoleLaunchUrl,
                    request.rdpUsername,
                    request.rdpDomain,
                    request.security,
                    int(request.ignoreCert),
                    now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="VM already exists") from exc
        db.commit()
        row = db.execute("SELECT * FROM vms WHERE id = ?", (request.id,)).fetchone()
        vm = row_to_vm(row)
        admin_rows = db.execute("SELECT username FROM users WHERE is_admin = 1").fetchall()
        sync_guacamole_connection_admin_permissions([admin["username"] for admin in admin_rows], vm)
        sync_guacamole_connection_password(vm.guacamoleConnectionId, request.rdpPassword)
        return vm
    finally:
        db.close()


@app.put("/api/admin/vms/{vm_id}", response_model=VmSummary)
def update_vm(
    vm_id: str, request: UpdateVmRequest, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)
) -> VmSummary:
    db, _, _ = require_admin(session_token)
    try:
        existing = db.execute("SELECT * FROM vms WHERE id = ?", (vm_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM not found")

        db.execute(
            """
            UPDATE vms
            SET name = ?,
                host = ?,
                protocol = ?,
                status = ?,
                guacamole_connection_id = ?,
                guacamole_launch_url = ?,
                rdp_username = ?,
                rdp_domain = ?,
                security = ?,
                ignore_cert = ?
            WHERE id = ?
            """,
            (
                request.name,
                request.host,
                request.protocol,
                request.status,
                request.guacamoleConnectionId,
                request.guacamoleLaunchUrl,
                request.rdpUsername,
                request.rdpDomain,
                request.security,
                int(request.ignoreCert),
                vm_id,
            ),
        )
        db.commit()
        row = db.execute("SELECT * FROM vms WHERE id = ?", (vm_id,)).fetchone()
        vm = row_to_vm(row)
        admin_rows = db.execute("SELECT username FROM users WHERE is_admin = 1").fetchall()
        sync_guacamole_connection_admin_permissions([admin["username"] for admin in admin_rows], vm)
        sync_guacamole_connection_password(vm.guacamoleConnectionId, request.rdpPassword)
        return vm
    finally:
        db.close()
