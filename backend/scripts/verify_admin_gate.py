# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0
"""Hermetic verification for the admin role gate + registration controls.

No PostgreSQL, no running server: builds a tiny FastAPI app with only
``admin.router``, ``auth.router`` and ``memory.admin_router``, overrides
``get_db`` with an in-memory fake session and ``get_current_user`` with a
switchable user, then drives it through ``TestClient``.

Run from ``backend/``::

    python -m scripts.verify_admin_gate

Exit code 0 = every check passed; 1 = at least one failed.
"""

import operator
import os
import sys
import tempfile
import traceback
import uuid
from datetime import datetime

# ── Hermetic config: point Config at a throwaway TOML before importing app ──
_tmpdir = tempfile.mkdtemp(prefix="wt-verify-")
_cfg_path = os.path.join(_tmpdir, "config.toml")
with open(_cfg_path, "w", encoding="utf-8") as f:
    f.write(
        '[server]\nhost = "127.0.0.1"\nport = 8158\n\n'
        '[security]\njwt_secret_key = "verify-admin-gate-secret"\n\n'
        '[database]\nhost = "127.0.0.1"\nport = 5432\nusername = "x"\n'
        'password = "x"\nname = "x"\n'
    )
os.environ["WEAVE_CONFIG_PATH"] = _cfg_path
os.environ.pop("CONFIG_MODEL_PATH", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.sql import Select  # noqa: E402

from app.api import admin, auth, memory  # noqa: E402
from app.core import deps  # noqa: E402
from app.core.config import get_config  # noqa: E402
from app.db.database import User, get_db  # noqa: E402
from app.schemas import chat as chat_schemas  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((True, name, ""))
        print(f"PASS  {name}")
    except Exception as e:  # noqa: BLE001 — report every failure, keep going
        msg = f"{type(e).__name__}: {e}"
        RESULTS.append((False, name, msg))
        print(f"FAIL  {name}\n      {msg}")
        if os.environ.get("VERBOSE"):
            traceback.print_exc()


# ── In-memory fake AsyncSession (only what these handlers use) ──
class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Interprets ``select(User).where(User.<col> ==/!= value, ...)``;
    raw ``text()`` statements (delete_user's taint cleanup) are no-ops."""

    def __init__(self):
        self.users = {}

    async def execute(self, stmt, params=None):
        if not isinstance(stmt, Select):
            return FakeResult([])
        rows = list(self.users.values())
        for crit in stmt._where_criteria:
            col, val, op = crit.left.key, crit.right.value, crit.operator
            if op is operator.eq:
                rows = [u for u in rows if getattr(u, col) == val]
            elif op is operator.ne:
                rows = [u for u in rows if getattr(u, col) != val]
            else:
                raise NotImplementedError(f"fake session: operator {op}")
        rows.sort(key=lambda u: u.created_at, reverse=True)
        return FakeResult(rows)

    def add(self, obj):
        obj.id = obj.id or str(uuid.uuid4())
        obj.created_at = obj.created_at or datetime.now()
        obj.role = obj.role or "user"
        obj.is_active = True if obj.is_active is None else obj.is_active
        self.users[obj.id] = obj

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def delete(self, obj):
        self.users.pop(obj.id, None)


def make_user(db, username, role):
    u = User(username=username, password_hash="orig-hash", role=role)
    u.id = str(uuid.uuid4())
    u.created_at = datetime.now()
    u.is_active = True
    db.users[u.id] = u
    return u


# ── App under test ──
db = FakeSession()
CURRENT = {"user": None}


async def fake_get_db():
    yield db


async def fake_current_user():
    return CURRENT["user"]


async def _noop(*a, **k):
    return None


# register() side effects that need real tables / filesystem
auth.ensure_user_agent_state = _noop
auth.ensure_user_workspace = _noop

app = FastAPI()
app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(memory.admin_router)
app.dependency_overrides[get_db] = fake_get_db
app.dependency_overrides[deps.get_current_user] = fake_current_user
client = TestClient(app)

admin_user = make_user(db, "root_admin", "admin")
normal_user = make_user(db, "alice", "user")
victim = make_user(db, "victim", "user")
roleless = make_user(db, "legacy", None)


def dep_calls(dependant):
    out = set()
    for d in dependant.dependencies:
        out.add(d.call)
        out |= dep_calls(d)
    return out


# ── 1. require_admin lives in deps and is shared ──
REQ = getattr(deps, "require_admin", None)


def t_deps_has_require_admin():
    assert REQ is not None, "app.core.deps.require_admin missing"


def t_memory_uses_same():
    assert getattr(memory, "require_admin", None) is REQ, \
        "memory.require_admin is not deps.require_admin"
    routes = [r for r in memory.admin_router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 3, f"expected 3 memory admin routes, got {len(routes)}"
    for r in routes:
        assert REQ in dep_calls(r.dependant), f"{r.path} lacks require_admin"


def t_admin_routes_gated():
    routes = [r for r in admin.router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 5, f"expected 5 admin routes, got {len(routes)}"
    for r in routes:
        assert REQ is not None and REQ in dep_calls(r.dependant), \
            f"{sorted(r.methods)} {r.path} lacks require_admin"


check("deps.require_admin exists", t_deps_has_require_admin)
check("memory admin endpoints use the same deps.require_admin", t_memory_uses_same)
check("all 5 /api/admin/users routes depend on require_admin", t_admin_routes_gated)


# ── 2. Non-admin (role='user' and role=NULL) → 403 on every admin op ──
def non_admin_case(who):
    def t():
        CURRENT["user"] = who
        before_ids = set(db.users)
        calls = [
            ("GET", "/api/admin/users", {}),
            ("POST", "/api/admin/users", {"username": "intruder", "password": "pw123456"}),
            ("GET", f"/api/admin/users/{victim.id}", {}),
            ("PUT", f"/api/admin/users/{victim.id}", {"password": "hijacked1"}),
            ("DELETE", f"/api/admin/users/{victim.id}", {}),
        ]
        for method, url, params in calls:
            r = client.request(method, url, params=params)
            assert r.status_code == 403, f"{method} {url} → {r.status_code} {r.text}"
            assert r.json()["detail"] == "Admin role required", r.text
        assert victim.password_hash == "orig-hash", "victim password changed!"
        assert set(db.users) == before_ids, "user set changed!"
    return t


check("non-admin (role='user') gets 403 on list/create/get/update/delete; no side effects",
      non_admin_case(normal_user))
check("role=NULL user gets 403 on all 5 (fail-closed)", non_admin_case(roleless))


def t_memory_admin_403():
    CURRENT["user"] = normal_user
    for method, url in (("GET", "/api/admin/memory/migration/status"),
                        ("POST", "/api/admin/memory/migration/run"),
                        ("POST", "/api/admin/memory/migration/rollback")):
        r = client.request(method, url)
        assert r.status_code == 403, f"{method} {url} → {r.status_code} {r.text}"


check("non-admin still gets 403 on memory admin endpoints", t_memory_admin_403)


# ── 3. Admin passes the gate and the handlers run ──
def t_admin_passes():
    CURRENT["user"] = admin_user
    r = client.get("/api/admin/users")
    assert r.status_code == 200, r.text
    assert {"root_admin", "alice", "victim"} <= {u["username"] for u in r.json()}

    r = client.post("/api/admin/users", params={"username": "newbie", "password": "pw123456"})
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert new_id in db.users

    r = client.get(f"/api/admin/users/{victim.id}")
    assert r.status_code == 200 and r.json()["username"] == "victim", r.text

    r = client.put(f"/api/admin/users/{victim.id}", params={"password": "reset-by-admin"})
    assert r.status_code == 200, r.text
    assert victim.password_hash != "orig-hash", "admin password reset did not apply"

    r = client.delete(f"/api/admin/users/{new_id}")
    assert r.status_code == 200, r.text
    assert new_id not in db.users

    r = client.delete(f"/api/admin/users/{admin_user.id}")
    assert r.status_code == 400, f"self-delete guard: {r.status_code} {r.text}"


check("admin gets past the gate: list/create/get/update(password reset)/delete all succeed",
      t_admin_passes)


# ── 4. Registration controls ──
cfg = get_config()
SEC = cfg._config.setdefault("security", {})


def set_sec(**kw):
    for k in ("registration_open", "registration_invite_code"):
        SEC.pop(k, None)
    SEC.update(kw)


def register(username, **extra):
    CURRENT["user"] = None
    body = {"username": username, "password": "pw123456", **extra}
    return client.post("/api/auth/register", json=body)


def t_defaults():
    set_sec()
    assert getattr(cfg, "registration_open") is True
    assert getattr(cfg, "registration_invite_code") == ""
    r = register("default_user")
    assert r.status_code == 201, f"{r.status_code} {r.text}"


def t_closed():
    set_sec(registration_open=False)
    n = len(db.users)
    r = register("blocked_user")
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    assert r.json()["detail"] == "注册已关闭，请联系管理员", r.text
    r = register("blocked_user2", invite_code="anything")
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    assert len(db.users) == n


def t_invite():
    set_sec(registration_invite_code="s3cret-邀请")
    n = len(db.users)
    r = register("no_code")
    assert r.status_code == 403 and r.json()["detail"] == "邀请码无效", f"{r.status_code} {r.text}"
    r = register("empty_code", invite_code="")
    assert r.status_code == 403 and r.json()["detail"] == "邀请码无效", f"{r.status_code} {r.text}"
    r = register("wrong_code", invite_code="s3cret")
    assert r.status_code == 403 and r.json()["detail"] == "邀请码无效", f"{r.status_code} {r.text}"
    assert len(db.users) == n
    r = register("right_code", invite_code="s3cret-邀请")
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    assert any(u.username == "right_code" for u in db.users.values())


def t_login_schema_untouched():
    assert set(chat_schemas.LoginRequest.model_fields) == {"username", "password"}
    rr = getattr(chat_schemas, "RegisterRequest", None)
    assert rr is not None and issubclass(rr, chat_schemas.LoginRequest)
    assert "invite_code" in rr.model_fields and not rr.model_fields["invite_code"].is_required()


check("defaults: registration_open=True, invite_code='' → register works without a code",
      t_defaults)
check("registration_open=false → 403 注册已关闭，请联系管理员 (no user created)", t_closed)
check("invite code set → missing/empty/wrong 403 邀请码无效; correct → 201", t_invite)
check("LoginRequest unchanged; RegisterRequest(LoginRequest) adds optional invite_code",
      t_login_schema_untouched)
set_sec()

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
sys.exit(1 if failed else 0)
