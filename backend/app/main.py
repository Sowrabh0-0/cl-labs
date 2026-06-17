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


class AssignVmRequest(BaseModel):
    vmId: str | None = None


class VmSummary(BaseModel):
    id: str
    name: str
    host: str
    protocol: str
    status: str
    guacamoleConnectionId: str
    guacamoleLaunchUrl: str


class UserSummary(BaseModel):
    id: int
    username: str
    isAdmin: bool
    vmId: str | None
    vmName: str | None
    createdAt: int


class SessionSummary(BaseModel):
    username: str
    isAdmin: bool
    vm: VmSummary | None
    expiresAt: int
    idleExpiresAt: int


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
        if request.vmId and not get_vm(db, request.vmId):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="VM does not exist")
        cursor = db.execute("UPDATE users SET vm_id = ? WHERE username = ?", (request.vmId, username))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        db.commit()
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
                    guacamole_launch_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.id,
                    request.name,
                    request.host,
                    request.protocol,
                    request.status,
                    request.guacamoleConnectionId,
                    request.guacamoleLaunchUrl,
                    now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="VM already exists") from exc
        db.commit()
        row = db.execute("SELECT * FROM vms WHERE id = ?", (request.id,)).fetchone()
        return row_to_vm(row)
    finally:
        db.close()
