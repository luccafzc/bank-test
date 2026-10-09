"""Finans: personal finance ledger with internal, simulated transfers."""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import Flask, Response, g, jsonify, request, send_from_directory
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime, ForeignKey,
                        Integer, String, UniqueConstraint, create_engine, delete,
                        func, or_, select)
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Sao_Paulo")
PASSWORDS = PasswordHasher()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
CATEGORIES = {"Salário", "Trabalho", "Moradia", "Alimentação", "Transporte", "Lazer", "Saúde", "Compras", "Outros"}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    email = Column(String(254), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    account_number = Column(String(12), unique=True, nullable=False)
    balance_cents = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=now)
    __table_args__ = (CheckConstraint("balance_cents >= 0", name="balance_nonnegative"),)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    csrf = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class Entry(Base):
    __tablename__ = "entries"
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount_cents = Column(BigInteger, nullable=False)
    description = Column(String(120), nullable=False)
    category = Column(String(30), nullable=False)
    kind = Column(String(16), nullable=False)
    transfer_id = Column(String(36), index=True)
    created_at = Column(DateTime, nullable=False, default=now, index=True)
    __table_args__ = (CheckConstraint("amount_cents <> 0", name="entry_nonzero"),)


class Operation(Base):
    __tablename__ = "operations"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    request_key = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    result_id = Column(String(36), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "request_key", name="operation_unique"),)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key = Column(String(64), primary_key=True)
    count = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,2})?", value):
        raise APIError("Informe um valor válido com até duas casas decimais.")
    cents = int(Decimal(value) * 100)
    if not 1 <= cents <= 100_000_000:
        raise APIError("O valor deve estar entre R$ 0,01 e R$ 1.000.000,00.")
    return cents


def email_address(value):
    if not isinstance(value, str):
        raise APIError("Informe um e-mail válido.")
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise APIError("Informe um e-mail válido.")
    return value


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Envie um objeto JSON válido.")
    return data


def text_field(value, label, maximum, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise APIError(f"{label}: use de {minimum} a {maximum} caracteres.")
    return value.strip()


def user_json(user):
    return {"id": user.id, "name": user.name, "email": user.email,
            "account_number": user.account_number, "balance_cents": user.balance_cents}


def entry_json(entry):
    return {"id": entry.id, "amount_cents": entry.amount_cents,
            "description": entry.description, "category": entry.category,
            "kind": entry.kind, "created_at": entry.created_at.isoformat() + "Z"}


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=16384,
                      COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
                      APP_ORIGIN=os.getenv("APP_ORIGIN", "http://localhost:8000"),
                      TESTING=False)
    if test_config:
        app.config.update(test_config)
    database = app.config.get("DATABASE_URL") or URL.create(
        "mysql+pymysql", username=os.getenv("MYSQL_USER", "aurora"),
        password=os.environ.get("MYSQL_PASSWORD", ""), host=os.getenv("MYSQL_HOST", "db"),
        port=int(os.getenv("MYSQL_PORT", "3306")), database=os.getenv("MYSQL_DATABASE", "aurora"),
        query={"charset": "utf8mb4"})
    engine = create_engine(database, pool_pre_ping=True, pool_recycle=1800)
    if engine.dialect.name != "mysql" and not app.config["TESTING"]:
        raise RuntimeError("Finans requer MySQL. SQLite é permitido apenas nos testes.")
    app.extensions["engine"] = engine

    @app.cli.command("init-db")
    def init_db():
        """Initialize a NEW database; schema changes need reviewed migrations."""
        Base.metadata.create_all(engine)
        print("Banco de dados inicializado.")

    @app.cli.command("cleanup-sessions")
    def cleanup_sessions():
        with Session(engine) as db, db.begin():
            db.execute(delete(AuthSession).where(AuthSession.expires_at < now()))
            db.execute(delete(RateBucket).where(RateBucket.expires_at < now()))
        print("Sessões e limites expirados removidos.")

    @app.before_request
    def before_request():
        if request.path.startswith("/api/"):
            g.db = Session(engine, expire_on_commit=False)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("Origin") not in {None, app.config["APP_ORIGIN"]}:
                    raise APIError("Origem não autorizada.", 403)
                if not request.is_json:
                    raise APIError("Use conteúdo JSON.", 415)

    @app.teardown_request
    def teardown(_error):
        if "db" in g:
            g.db.close()

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.path.startswith("/api/") or request.path == "/config.js":
            response.headers["Cache-Control"] = "no-store"
        if app.config["COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(413)
    def oversized(_error):
        return jsonify(error="Solicitação muito grande."), 413

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        if "db" in g:
            g.db.rollback()
        app.logger.error("Database operation failed: %s", type(error).__name__)
        return jsonify(error="Não foi possível concluir. Tente novamente em instantes."), 503

    def rate_limit(action, email):
        # Database-backed limits work across server threads and processes.
        for identity, maximum in ((request.remote_addr or "unknown", 60), (email, 10)):
            key = hashlib.sha256(f"{action}:{identity}".encode()).hexdigest()
            current = now()
            with Session(engine) as db:
                try:
                    db.add(RateBucket(key=key, count=0, expires_at=current + timedelta(minutes=15)))
                    db.commit()
                except IntegrityError:
                    db.rollback()
                row = db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
                if row.expires_at <= current:
                    row.count, row.expires_at = 0, current + timedelta(minutes=15)
                if row.count >= maximum:
                    raise APIError("Muitas tentativas. Aguarde 15 minutos.", 429)
                row.count += 1
                db.commit()

    def authenticated(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            raw = request.cookies.get("aurora_session", "")
            session = g.db.get(AuthSession, hashlib.sha256(raw.encode()).hexdigest()) if raw else None
            if not session or session.expires_at <= now():
                raise APIError("Entre na sua conta para continuar.", 401)
            g.user_id, g.auth = session.user_id, session
            if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), session.csrf):
                raise APIError("Sessão inválida. Atualize a página.", 403)
            return fn(*args, **kwargs)
        return wrapper

    def sign_in(user, remember=False):
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        duration = 30 * 86400 if remember else 12 * 3600
        previous = request.cookies.get("aurora_session")
        if previous:
            g.db.execute(delete(AuthSession).where(AuthSession.token_hash == hashlib.sha256(previous.encode()).hexdigest()))
        g.db.add(AuthSession(token_hash=hashlib.sha256(raw.encode()).hexdigest(), user_id=user.id,
                             csrf=csrf, expires_at=now() + timedelta(seconds=duration)))
        g.db.commit()
        response = jsonify(user=user_json(user), csrf=csrf)
        response.set_cookie("aurora_session", raw, max_age=duration if remember else None,
                            httponly=True, secure=app.config["COOKIE_SECURE"], samesite="Strict", path="/")
        return response

    @app.post("/api/auth/register")
    def register():
        data = body()
        email = email_address(data.get("email"))
        rate_limit("register", email)
        name = text_field(data.get("name"), "Nome", 80, 2)
        password = data.get("password")
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise APIError("A senha deve ter de 12 a 128 caracteres.")
        user = User(name=name, email=email, password_hash=PASSWORDS.hash(password),
                    account_number=str(secrets.randbelow(9_000_000_000) + 1_000_000_000), balance_cents=0)
        g.db.add(user)
        try:
            g.db.flush()
        except IntegrityError:
            g.db.rollback()
            raise APIError("Não foi possível cadastrar este e-mail. Tente entrar na conta.", 409)
        return sign_in(user)

    @app.post("/api/auth/login")
    def login():
        data = body()
        email = email_address(data.get("email"))
        rate_limit("login", email)
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 128:
            raise APIError("E-mail ou senha incorretos.", 401)
        user = g.db.scalar(select(User).where(User.email == email))
        try:
            PASSWORDS.verify(user.password_hash if user else DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            raise APIError("E-mail ou senha incorretos.", 401)
        if not user:
            raise APIError("E-mail ou senha incorretos.", 401)
        if PASSWORDS.check_needs_rehash(user.password_hash):
            user.password_hash = PASSWORDS.hash(password)
        return sign_in(user, data.get("remember") is True)

    @app.get("/api/auth/me")
    @authenticated
    def me():
        return jsonify(user=user_json(g.db.get(User, g.user_id)), csrf=g.auth.csrf)

    @app.post("/api/auth/logout")
    @authenticated
    def logout():
        g.db.delete(g.auth)
        g.db.commit()
        response = jsonify(ok=True)
        response.delete_cookie("aurora_session", path="/", secure=app.config["COOKIE_SECURE"], httponly=True, samesite="Strict")
        return response

    def period():
        value = request.args.get("month", datetime.now(TZ).strftime("%Y-%m"))
        if not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", value):
            raise APIError("Mês inválido.")
        year, month = map(int, value.split("-"))
        start = datetime(year, month, 1, tzinfo=TZ)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=TZ)
        return value, start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)

    def entries_query():
        _, start, end = period()
        return select(Entry).where(Entry.user_id == g.user_id, Entry.created_at >= start,
                                   Entry.created_at < end).order_by(Entry.created_at.desc(), Entry.id)

    @app.get("/api/dashboard")
    @authenticated
    def dashboard():
        entries = g.db.scalars(entries_query()).all()
        return jsonify(user=user_json(g.db.get(User, g.user_id)), entries=[entry_json(e) for e in entries], month=period()[0])

    def idempotency(data):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", key):
            raise APIError("Identificador da operação inválido.")
        digest = hashlib.sha256(json.dumps({"path": request.path, "data": data}, sort_keys=True).encode()).hexdigest()
        # A locking read observes the latest committed operation under MySQL's
        # REPEATABLE READ too, including a concurrent request that just finished.
        old = g.db.scalar(select(Operation).where(Operation.user_id == g.user_id, Operation.request_key == key).with_for_update())
        if old and old.payload_hash != digest:
            raise APIError("Identificador já usado em outra operação.", 409)
        return key, digest, old

    @app.post("/api/entries")
    @authenticated
    def add_entry():
        data = body()
        cents = money(data.get("amount"))
        kind = data.get("kind")
        category = data.get("category")
        description = text_field(data.get("description"), "Descrição", 120)
        if kind not in {"income", "expense"} or category not in CATEGORIES:
            raise APIError("Tipo ou categoria inválidos.")
        # Lock before idempotency read: concurrent duplicates serialize per user.
        user = g.db.scalar(select(User).where(User.id == g.user_id).with_for_update())
        key, digest, old = idempotency(data)
        if old:
            return jsonify(id=old.result_id, duplicate=True)
        signed = cents if kind == "income" else -cents
        if user.balance_cents + signed < 0:
            raise APIError("Saldo insuficiente para registrar esta saída.", 409)
        entry_id = str(uuid.uuid4())
        user.balance_cents += signed
        g.db.add(Entry(id=entry_id, user_id=user.id, amount_cents=signed, description=description, category=category, kind=kind))
        g.db.add(Operation(user_id=user.id, request_key=key, payload_hash=digest, result_id=entry_id))
        g.db.commit()
        return jsonify(id=entry_id), 201

    @app.post("/api/transfers")
    @authenticated
    def transfer():
        data = body()
        cents = money(data.get("amount"))
        email = email_address(data.get("email"))
        # Lock both accounts in deterministic order to prevent inverse-transfer deadlocks.
        users = g.db.scalars(select(User).where(or_(User.id == g.user_id, User.email == email))
                             .order_by(User.id).with_for_update()).all()
        sender = next(u for u in users if u.id == g.user_id)
        key, digest, old = idempotency(data)
        if old:
            return jsonify(id=old.result_id, duplicate=True)
        recipient = next((u for u in users if u.email == email), None)
        if recipient is None:
            raise APIError("Destinatário não encontrado. Use o e-mail de uma conta Finans.", 404)
        if recipient.id == sender.id:
            raise APIError("Escolha outra conta para transferir.")
        if sender.balance_cents < cents:
            raise APIError("Saldo insuficiente.", 409)
        transfer_id = str(uuid.uuid4())
        sender.balance_cents -= cents
        recipient.balance_cents += cents
        for user, signed, description in ((sender, -cents, f"Para {recipient.name}"), (recipient, cents, f"De {sender.name}")):
            g.db.add(Entry(id=str(uuid.uuid4()), user_id=user.id, amount_cents=signed, description=description,
                           category="Transferência", kind="transfer", transfer_id=transfer_id))
        g.db.add(Operation(user_id=sender.id, request_key=key, payload_hash=digest, result_id=transfer_id))
        g.db.commit()
        return jsonify(id=transfer_id), 201

    @app.get("/api/export")
    @authenticated
    def export():
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(["Data", "Descrição", "Categoria", "Valor (BRL)"])
        for entry in g.db.scalars(entries_query()):
            description = entry.description
            if description.lstrip().startswith(("=", "+", "-", "@")):
                description = "'" + description
            writer.writerow([entry.created_at.replace(tzinfo=timezone.utc).astimezone(TZ).isoformat(), description,
                             entry.category, f"{Decimal(entry.amount_cents) / 100:.2f}"])
        return Response("\ufeff" + buffer.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="finans-{period()[0]}.csv"'})

    @app.get("/api/health")
    def health():
        g.db.execute(select(1))
        return jsonify(status="ok", database=engine.dialect.name)

    @app.get("/config.js")
    def config():
        return Response("window.FINANS_CONFIG = {mode: 'api'};", mimetype="application/javascript")

    @app.get("/")
    def index():
        return send_from_directory(ROOT / "dist", "index.html")

    @app.get("/<path:filename>")
    def static_file(filename):
        return send_from_directory(ROOT / "dist", filename)

    return app
