# ETF Portfolio Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A LAN-only dashboard in the homelab cluster that shows portfolio value over time, a per-ETF overview, and a per-ETF transaction table with FIFO profit/loss, fed from the Scalable Capital MCP.

**Architecture:** A small FastAPI app (SQLite on a retained Longhorn PVC) renders three server-side pages with Chart.js. A separate fetcher script on raspi2 (systemd timer, 08:00 and 23:30 Berlin) calls the read-only Scalable MCP tools and pushes trades, daily closes and quotes to the app's bearer-protected ingest endpoint. Cost basis, realized P/L and XIRR are computed in the app, never in the fetcher.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, SQLite (`sqlite3`), Chart.js 4 (vendored), pytest, httpx, `mcp` Python SDK, Docker, GitHub Actions (ghcr), Flux/Kustomize/SOPS, Traefik, Prometheus operator CRDs, Ansible + systemd.

**Spec:** GitHub issue umueller74/k8s-homelab#197 and `~/Workspace/second-brain/topics/kubernetes-homelab/2026-10-11-etf-dashboard-design.md`.

**Design note (simplification of the agreed design):** the "daily snapshot" table is the `closes` table. The fetcher pushes `year_to_date` closes on every run and the app upserts them, so history is backfilled on the first run and extended daily; stored rows survive the New Year reset of `year_to_date`.

## Global Constraints

- EUR only; no FX. Money and quantities are `Decimal` (stored as TEXT in SQLite), never `float`, except for chart and XIRR output.
- Python 3.13. Image must be `linux/amd64`.
- Only `kind == "security"` rows with `status == "SETTLED"` and `isCancellation == false` are trades. Cash rows and `CANCELLED` rows are ignored.
- Trade price = `marketValuation / quantity` from `get_transaction_details` (never the order's `limitPrice`, and never `|amount| / quantity`: the list's `amount` is the broker total *after* fees). Fee = `abs(abs(totalAmount) - marketValuation)`. Verified on real data 2026-10-11: cost = quantity * price + fee equals the broker's `totalAmount` (for example BUY total -42.99 = 42.00 + 0.99).
- FIFO cost basis; a buy lot's cost includes its fee; a sale's proceeds are net of its fee.
- Ranges: `1w`, `1m`, `3m`, `6m`, `1y`, `all` on the chart; "1D" is only a last-session change figure.
- XIRR is shown only when the oldest cash flow is at least 90 days old.
- No secrets in Git. App repo is public and holds code only. All Kubernetes secrets are SOPS (`*.sops.yaml`).
- The fetcher only calls read tools: `list_portfolio_transactions`, `get_transaction_details`, `get_security_chart`, `get_security_quote`.
- Token and state files on raspi2 are mode 0600 and outside Git and Ansible vault.
- Hostname `portfolio.homelab.cs-ol.de`; Traefik basicAuth on every path except `/api`; `/api` uses its own bearer token.
- Resources: requests 100m CPU / 128Mi, limits 500m / 256Mi. Backup: NAS only, no MEGA.
- Never commit to `main` in `k8s-homelab` or `homelab`: branch `197-etf-dashboard`, commit messages start with `#197 `. Flux deploys from `main` only after PR merge.
- Pages are English; dates are `YYYY-MM-DD` in Europe/Berlin; numbers use two decimals for money and six for quantities.

## Review Focus

- A sale larger than the held quantity (bad or partial data): the page shows a visible data-error banner, never a 500.
- Fractional savings-plan quantities (for example 0.296736 at 84.25 = 25.0000): no float drift, totals match the broker to the cent.
- An ETF with no quote or no closes yet (bought today, fetcher has not caught up): shows "n/a" and falls back to cost, never a crash or a zero value.
- Empty database before the first ingest: all three pages render "No data yet" with HTTP 200.
- A fully sold-out ETF (quantity 0): hidden from the overview and from the portfolio value, still listed on its transactions page with realized P/L.
- A very young position: XIRR annualization would show absurd percentages, so it is hidden under 90 days.
- A trade `kind` the app has not seen before (not `SINGLE` or `SAVINGS_PLAN`): stored as-is, ingest does not fail.

---

## File Structure

Repo `umueller74/portfolio-dashboard` (local path `~/Workspace/portfolio-dashboard`):

| File | Responsibility |
|---|---|
| `pyproject.toml` | package metadata, dependencies, pytest config |
| `portfolio/db.py` | SQLite connection and schema |
| `portfolio/fifo.py` | `Trade`, `Lot`, `Sale`, `replay()` (FIFO) |
| `portfolio/xirr.py` | `xirr()` |
| `portfolio/ingest.py` | pydantic payload models, `POST /api/ingest`, bearer auth, `GET /api/backup` |
| `portfolio/services.py` | holdings, value series, range slicing, last-session change |
| `portfolio/web.py` | page routes, Jinja filters, `/healthz`, `/metrics` |
| `portfolio/app.py` | `create_app()` wiring |
| `portfolio/templates/*.html`, `portfolio/static/` | pages and vendored Chart.js |
| `fetcher/normalize.py` | pure parsers for MCP payloads |
| `fetcher/auth_tokenfile.py` | access token from Hermes' token files with refresh under lock |
| `fetcher/run.py` | MCP session, collection, push to ingest |
| `Dockerfile`, `.github/workflows/image.yml` | image build and publish |
| `deploy/` | systemd unit and timer templates copied by Ansible |
| `tests/` | pytest suites |

Repo `k8s-homelab`: `apps/base/portfolio/*`, `apps/production/portfolio/*`, `clusters/production/portfolio.yaml`.
Repo `homelab` (Ansible): a new role `portfolio_fetcher`.

---

### Task 0: Raspi2 spike: can the fetcher import Hermes' OAuth module?

Decides which implementation of `get_access_token()` Task 9 ships. Do this first; it needs shell access to raspi2 and takes about 15 minutes.

**Files:** none (findings go into the issue as a comment).

**Produces:** a decision, "import" (option b) or "tokenfile" (option a, fallback).

- [ ] **Step 1: Find Hermes' Python and the OAuth API**

Run on raspi2 as user `hermes`:
```bash
ls -d /home/hermes/.hermes/hermes-agent/venv /home/hermes/.hermes/hermes-agent/.venv 2>&1
cat /home/hermes/.hermes/hermes-agent/.hermes/bin/hermes | head -20
grep -n "^def \|^async def \|^class \|flock\|refresh.lock" /home/hermes/.hermes/hermes-agent/tools/mcp_oauth.py | head -60
```
Expected: a venv path (or a launcher naming the Python) and the function names of the refresh logic, including how the lock is taken (`fcntl.flock` or another mechanism).

- [ ] **Step 2: Try an import with Hermes' own Python**

```bash
cd /home/hermes/.hermes/hermes-agent
<venv>/bin/python -c "import tools.mcp_oauth as m; print([n for n in dir(m) if not n.startswith('_')])"
```
Expected: the module imports and lists public names. Look for a function that returns a valid, refreshed access token for server name `scalable`.

- [ ] **Step 3: Decide and record**

If a public function returns a refreshed token without side effects beyond the token file, choose **import**: Task 9 adds `fetcher/auth_hermes.py` that calls it with Hermes' interpreter on `PYTHONPATH`. Otherwise choose **tokenfile**: Task 9 ships `auth_tokenfile.py` as written. Also note whether Hermes uses `fcntl.flock` on `scalable.json.refresh.lock`; the tokenfile code assumes it does.

Record the decision:
```bash
gh issue comment 197 --repo umueller74/k8s-homelab --body "Spike result: <import|tokenfile>. Hermes lock mechanism: <flock|other>."
```

**Result (executed 2026-10-11): "import" works.** Facts found on raspi2:
- Hermes runs on a bundled interpreter, `/home/hermes/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3` (version string changes on Hermes updates). Its dependencies (including `mcp` 2.0.0) live in a venv that `import hermes_bootstrap` adds to `sys.path`. The launcher is `hermes --print-runtime-command`.
- `tools.mcp_oauth.build_oauth_auth(server_name, server_url, oauth_config) -> OAuthClientProvider` is an `httpx2.Auth` that refreshes tokens itself under a non-blocking `flock` fence on `scalable.json.refresh.lock`.
- `mcp` 2.0 API: `create_mcp_http_client(headers=None, timeout=None, auth=None)` returns an `httpx2.AsyncClient`; `streamable_http_client(url, http_client=client)` yields streams (`streams[0]`, `streams[1]`); `CallToolResult` uses `is_error` (snake_case), not `isError`.
- Proven with a throwaway script: `build_oauth_auth("scalable", URL, {"redirect_port": 8765})` plus `ClientSession.call_tool("ping", {})` returned `pong` as user `hermes`, with no re-login.
- The interpreter's venv has `httpx2`, not `httpx`; the fetcher therefore uses only the standard library plus `mcp` for its own HTTP.

---

### Task 1: Repo scaffold and database schema

**Files:**
- Create: `~/Workspace/portfolio-dashboard/pyproject.toml`, `portfolio/__init__.py`, `portfolio/db.py`, `tests/test_db.py`, `.gitignore`

**Produces:** `portfolio.db.connect(path) -> sqlite3.Connection` (schema applied, `row_factory=sqlite3.Row`).

- [ ] **Step 1: Create the repo**

```bash
mkdir -p ~/Workspace/portfolio-dashboard && cd ~/Workspace/portfolio-dashboard
git init -b main
python3.13 -m venv .venv && . .venv/bin/activate
printf '.venv/\n__pycache__/\n*.db\n.pytest_cache/\n' > .gitignore
```

- [ ] **Step 2: `pyproject.toml`**

```toml
[project]
name = "portfolio-dashboard"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["fastapi>=0.115", "uvicorn>=0.30", "jinja2>=3.1", "python-multipart>=0.0.9"]

[project.optional-dependencies]
fetcher = ["httpx>=0.27", "mcp>=1.2"]
dev = ["pytest>=8", "httpx>=0.27", "pytest-asyncio>=0.24"]

[tool.setuptools]
packages = ["portfolio", "fetcher"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```
Run: `pip install -e '.[dev,fetcher]'`

- [ ] **Step 3: Write the failing test** (`tests/test_db.py`)

```python
from portfolio.db import connect


def test_schema_has_expected_tables(tmp_path):
    conn = connect(tmp_path / "p.db")
    names = {r["name"] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"trades", "closes", "quotes", "meta"} <= names


def test_side_is_constrained(tmp_path):
    conn = connect(tmp_path / "p.db")
    import sqlite3, pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into trades(id,isin,name,side,kind,executed_at,quantity,price,amount,fee)"
            " values('x','I','n','HOLD','SINGLE','2026-01-01T00:00:00+00:00','1','1','1','0')"
        )
```

- [ ] **Step 4: Run, expect failure**

Run: `pytest tests/test_db.py -v` — Expected: FAIL (`ModuleNotFoundError: portfolio.db`).

- [ ] **Step 5: Implement `portfolio/db.py`** (and an empty `portfolio/__init__.py`)

```python
import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists trades(
  id text primary key,
  isin text not null,
  name text not null,
  side text not null check (side in ('BUY','SELL')),
  kind text not null,
  executed_at text not null,
  quantity text not null,
  price text not null,
  amount text not null,
  fee text not null default '0'
);
create index if not exists trades_isin on trades(isin);
create table if not exists closes(
  isin text not null, day text not null, price text not null,
  primary key (isin, day)
);
create table if not exists quotes(
  isin text primary key, price text not null, quoted_at text not null, outdated integer not null
);
create table if not exists meta(key text primary key, value text not null);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript(SCHEMA)
    return conn
```

- [ ] **Step 6: Run, expect pass**

Run: `pytest tests/test_db.py -v` — Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Scaffold project and SQLite schema"
```

---

### Task 2: FIFO engine

**Files:**
- Create: `portfolio/fifo.py`, `tests/test_fifo.py`

**Produces:**
- `Trade(id, isin, name, side, kind, executed_at: datetime, quantity: Decimal, price: Decimal, fee: Decimal = 0)`
- `Lot(trade, remaining)` with `.unit_cost`, `.cost`
- `Sale(trade, proceeds, cost_basis)` with `.realized`
- `replay(trades) -> (list[Lot], list[Sale])`, raises `OversellError`

- [ ] **Step 1: Write the failing tests** (`tests/test_fifo.py`)

```python
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from portfolio.fifo import OversellError, Trade, replay


def t(id, side, qty, price, day, fee="0"):
    return Trade(id, "IE1", "ETF", side, "SINGLE",
                 datetime(2026, 1, day, tzinfo=timezone.utc), D(qty), D(price), D(fee))


def test_sale_consumes_oldest_lot_first():
    lots, sales = replay([t("a", "BUY", "10", "100", 1), t("b", "BUY", "10", "110", 2),
                          t("c", "SELL", "15", "120", 3)])
    assert [l.remaining for l in lots] == [D(0), D(5)]
    assert sales[0].cost_basis == D("1550")          # 10*100 + 5*110
    assert sales[0].realized == D("250")              # 15*120 - 1550


def test_fee_raises_cost_and_lowers_proceeds():
    lots, sales = replay([t("a", "BUY", "10", "100", 1, fee="1"), t("b", "SELL", "10", "110", 2, fee="1")])
    assert lots[0].unit_cost == D("100.1")
    assert sales[0].realized == D("1099") - D("1001")


def test_fractional_quantities_stay_exact():
    lots, _ = replay([t("a", "BUY", "0.296736", "84.25", 1)])
    assert lots[0].cost.quantize(D("0.0001")) == D("25.0000")


def test_oversell_raises():
    with pytest.raises(OversellError):
        replay([t("a", "BUY", "1", "100", 1), t("b", "SELL", "2", "100", 2)])


def test_order_is_by_time_not_input_order():
    lots, sales = replay([t("b", "SELL", "5", "120", 3), t("a", "BUY", "5", "100", 1)])
    assert sales[0].realized == D("100")
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/test_fifo.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `portfolio/fifo.py`**

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class OversellError(ValueError):
    pass


@dataclass(frozen=True)
class Trade:
    id: str
    isin: str
    name: str
    side: str
    kind: str
    executed_at: datetime
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal(0)


@dataclass
class Lot:
    trade: Trade
    remaining: Decimal

    @property
    def unit_cost(self) -> Decimal:
        t = self.trade
        return (t.quantity * t.price + t.fee) / t.quantity

    @property
    def cost(self) -> Decimal:
        return self.remaining * self.unit_cost


@dataclass(frozen=True)
class Sale:
    trade: Trade
    proceeds: Decimal
    cost_basis: Decimal

    @property
    def realized(self) -> Decimal:
        return self.proceeds - self.cost_basis


def replay(trades: list[Trade]) -> tuple[list[Lot], list[Sale]]:
    lots: list[Lot] = []
    sales: list[Sale] = []
    for t in sorted(trades, key=lambda x: (x.executed_at, x.id)):
        if t.side == "BUY":
            lots.append(Lot(t, t.quantity))
            continue
        need, basis = t.quantity, Decimal(0)
        for lot in lots:
            if need == 0:
                break
            take = min(lot.remaining, need)
            basis += take * lot.unit_cost
            lot.remaining -= take
            need -= take
        if need > 0:
            raise OversellError(f"{t.isin}: sale {t.id} exceeds holdings by {need}")
        sales.append(Sale(t, t.quantity * t.price - t.fee, basis))
    return lots, sales
```

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/test_fifo.py -v` — Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add FIFO engine with fees and oversell detection"
```

---

### Task 3: XIRR

**Files:**
- Create: `portfolio/xirr.py`, `tests/test_xirr.py`

**Produces:** `xirr(flows: list[tuple[date, float]]) -> float | None` (annualized, 0.10 = 10 %).

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date

import pytest

from portfolio.xirr import xirr


def test_simple_one_year_ten_percent():
    assert xirr([(date(2026, 1, 1), -1000), (date(2027, 1, 1), 1100)]) == pytest.approx(0.10, abs=1e-6)


def test_multiple_contributions():
    flows = [(date(2026, 1, 1), -100), (date(2026, 7, 1), -100), (date(2027, 1, 1), 230)]
    r = xirr(flows)
    assert 0.1 < r < 0.2


def test_no_sign_change_returns_none():
    assert xirr([(date(2026, 1, 1), -100), (date(2026, 6, 1), -50)]) is None


def test_single_flow_returns_none():
    assert xirr([(date(2026, 1, 1), -100)]) is None
```

- [ ] **Step 2: Run, expect failure** — `pytest tests/test_xirr.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `portfolio/xirr.py`**

```python
from datetime import date


def xirr(flows: list[tuple[date, float]]) -> float | None:
    flows = sorted(flows)
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = flows[0][0]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** ((d - t0).days / 365) for d, a in flows)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2
```

- [ ] **Step 4: Run, expect pass** — 4 passed.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add XIRR"`

---

### Task 4: Ingest API

**Files:**
- Create: `portfolio/ingest.py`, `portfolio/app.py`, `tests/conftest.py`, `tests/test_ingest.py`

**Consumes:** `portfolio.db.connect`.

**Produces:**
- `create_app(db_path, ingest_token) -> FastAPI` with `app.state.conn` and `app.state.token`.
- `POST /api/ingest` (bearer) body `{"fetched_at": iso, "trades":[{id,isin,name,side,kind,executed_at,quantity,price,amount,fee?}], "closes":[{isin,day,price}], "quotes":[{isin,price,quoted_at,outdated}]}`; idempotent upsert; sets `meta.last_ingest` to `fetched_at`.
- `GET /api/backup` (bearer) returns a consistent copy of the SQLite file.

- [ ] **Step 1: Write the failing tests** (`tests/conftest.py`, `tests/test_ingest.py`)

```python
# tests/conftest.py
import pytest
from fastapi.testclient import TestClient

from portfolio.app import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "p.db", TOKEN))


def payload(**over):
    body = {
        "fetched_at": "2026-10-11T21:30:00+00:00",
        "trades": [
            {"id": "t1", "isin": "IE0000000001", "name": "Test World ETF (Acc)", "side": "BUY",
             "kind": "SINGLE", "executed_at": "2026-09-14T09:00:48.260Z",
             "quantity": "5", "price": "700.00", "amount": "-2100"},
        ],
        "closes": [{"isin": "IE0000000001", "day": "2026-10-09", "price": "710.50"}],
        "quotes": [{"isin": "IE0000000001", "price": "710.50",
                    "quoted_at": "2026-10-09T20:59:56.615Z", "outdated": True}],
    }
    body.update(over)
    return body
```

```python
# tests/test_ingest.py
from tests.conftest import AUTH, payload


def test_rejects_missing_or_wrong_token(client):
    assert client.post("/api/ingest", json=payload()).status_code == 401
    assert client.post("/api/ingest", json=payload(), headers={"Authorization": "Bearer nope"}).status_code == 401


def test_ingest_is_idempotent(client):
    for _ in range(2):
        assert client.post("/api/ingest", json=payload(), headers=AUTH).status_code == 200
    conn = client.app.state.conn
    assert conn.execute("select count(*) from trades").fetchone()[0] == 1
    assert conn.execute("select count(*) from closes").fetchone()[0] == 1
    assert conn.execute("select value from meta where key='last_ingest'").fetchone()[0] == "2026-10-11T21:30:00+00:00"


def test_unknown_trade_kind_is_stored(client):
    body = payload()
    body["trades"][0]["kind"] = "SOMETHING_NEW"
    assert client.post("/api/ingest", json=body, headers=AUTH).status_code == 200


def test_fee_updates_on_second_ingest(client):
    client.post("/api/ingest", json=payload(), headers=AUTH)
    body = payload()
    body["trades"][0]["fee"] = "0.99"
    client.post("/api/ingest", json=body, headers=AUTH)
    row = client.app.state.conn.execute("select fee from trades").fetchone()
    assert row["fee"] == "0.99"


def test_backup_returns_sqlite_file(client):
    client.post("/api/ingest", json=payload(), headers=AUTH)
    r = client.get("/api/backup", headers=AUTH)
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")
    assert client.get("/api/backup").status_code == 401
```

- [ ] **Step 2: Run, expect failure** — `pytest tests/test_ingest.py -v` → FAIL (`portfolio.app` missing).

- [ ] **Step 3: Implement `portfolio/ingest.py`**

```python
import hmac
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

router = APIRouter(prefix="/api")


class TradeIn(BaseModel):
    id: str
    isin: str
    name: str
    side: str
    kind: str
    executed_at: datetime
    quantity: Decimal
    price: Decimal
    amount: Decimal
    fee: Decimal = Decimal(0)


class CloseIn(BaseModel):
    isin: str
    day: date
    price: Decimal


class QuoteIn(BaseModel):
    isin: str
    price: Decimal
    quoted_at: datetime
    outdated: bool


class IngestIn(BaseModel):
    fetched_at: datetime
    trades: list[TradeIn] = []
    closes: list[CloseIn] = []
    quotes: list[QuoteIn] = []


def require_token(request: Request, authorization: str = Header(default="")):
    expected = f"Bearer {request.app.state.token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/ingest", dependencies=[Depends(require_token)])
async def ingest(body: IngestIn, request: Request):
    conn = request.app.state.conn
    with conn:
        for t in body.trades:
            conn.execute(
                "insert into trades(id,isin,name,side,kind,executed_at,quantity,price,amount,fee)"
                " values(?,?,?,?,?,?,?,?,?,?)"
                " on conflict(id) do update set isin=excluded.isin, name=excluded.name, side=excluded.side,"
                " kind=excluded.kind, executed_at=excluded.executed_at, quantity=excluded.quantity,"
                " price=excluded.price, amount=excluded.amount, fee=excluded.fee",
                (t.id, t.isin, t.name, t.side, t.kind, t.executed_at.isoformat(),
                 str(t.quantity), str(t.price), str(t.amount), str(t.fee)),
            )
        for c in body.closes:
            conn.execute(
                "insert into closes(isin,day,price) values(?,?,?)"
                " on conflict(isin,day) do update set price=excluded.price",
                (c.isin, c.day.isoformat(), str(c.price)),
            )
        for q in body.quotes:
            conn.execute(
                "insert into quotes(isin,price,quoted_at,outdated) values(?,?,?,?)"
                " on conflict(isin) do update set price=excluded.price, quoted_at=excluded.quoted_at,"
                " outdated=excluded.outdated",
                (q.isin, str(q.price), q.quoted_at.isoformat(), int(q.outdated)),
            )
        conn.execute(
            "insert into meta(key,value) values('last_ingest',?)"
            " on conflict(key) do update set value=excluded.value",
            (body.fetched_at.isoformat(),),
        )
    return {"trades": len(body.trades), "closes": len(body.closes), "quotes": len(body.quotes)}


@router.get("/backup", dependencies=[Depends(require_token)])
async def backup(request: Request):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # VACUUM INTO needs a non-existing target
    request.app.state.conn.execute("vacuum into ?", (path,))
    return FileResponse(path, media_type="application/octet-stream", filename="portfolio.db",
                        background=BackgroundTask(os.unlink, path))
```

- [ ] **Step 4: Implement `portfolio/app.py`**

```python
from pathlib import Path

from fastapi import FastAPI

from portfolio import ingest
from portfolio.db import connect


def create_app(db_path: str | Path, ingest_token: str) -> FastAPI:
    app = FastAPI(title="portfolio-dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.conn = connect(db_path)
    app.state.token = ingest_token
    app.include_router(ingest.router)
    return app
```

- [ ] **Step 5: Run, expect pass** — `pytest tests/test_ingest.py -v` → 5 passed.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "Add ingest and backup API"`

---

### Task 5: Portfolio services (holdings, value series, ranges, last session)

**Files:**
- Create: `portfolio/services.py`, `tests/test_services.py`

**Consumes:** `connect`, `fifo.Trade/replay/OversellError`, `xirr`.

**Produces:**
- `load_trades(conn) -> list[Trade]`
- `Holding(isin, name, quantity, cost, price, value, pnl_abs, pnl_pct, share, avg_cost, xirr, price_missing)` dataclass; all money fields `Decimal | None` where unknown
- `overview(conn, today: date) -> Overview(holdings: list[Holding], total_value, total_cost, pnl_abs, pnl_pct, xirr, error: str | None)`
- `value_series(conn) -> list[SeriesPoint(day: date, value: float, invested: float)]`
- `slice_range(series, range_key: str, today: date) -> list[SeriesPoint]`
- `last_session_change(conn) -> tuple[Decimal, Decimal] | None` (absolute, percent)
- `isin_detail(conn, isin) -> IsinDetail(name, rows: list[TxRow], realized_total: Decimal, error)` where `TxRow(trade, price_paid_sum, current_value, pnl_pct, remaining, realized)`

- [ ] **Step 1: Write the failing tests** (`tests/test_services.py`)

```python
from datetime import date

import pytest
from decimal import Decimal as D

from portfolio import services
from tests.conftest import AUTH, payload

ISIN = "IE0000000001"


def seed(client, trades, closes=(), quotes=()):
    r = client.post("/api/ingest", headers=AUTH, json=payload(trades=trades, closes=list(closes), quotes=list(quotes)))
    assert r.status_code == 200


def trade(id, side, qty, price, ts, fee="0", isin=ISIN):
    sign = "-" if side == "BUY" else ""
    return {"id": id, "isin": isin, "name": "ETF " + isin, "side": side, "kind": "SINGLE", "executed_at": ts,
            "quantity": qty, "price": price, "amount": f"{sign}{D(qty) * D(price)}", "fee": fee}


def test_overview_values_holding_with_quote(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    o = services.overview(client.app.state.conn, date(2026, 10, 11))
    h = o.holdings[0]
    assert (h.quantity, h.cost, h.value, h.pnl_abs) == (D(5), D(500), D(550), D(50))
    assert h.pnl_pct == D(10) and o.total_value == D(550)
    assert h.xirr is not None


def test_overview_without_quote_falls_back_to_last_close_then_cost(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-09", "price": "120"}])
    h = services.overview(client.app.state.conn, date(2026, 10, 11)).holdings[0]
    assert h.value == D(600)
    seed(client, [trade("b", "BUY", "1", "100", "2026-10-10T10:00:00Z", isin="IE2")])
    h2 = [x for x in services.overview(client.app.state.conn, date(2026, 10, 11)).holdings if x.isin == "IE2"][0]
    assert h2.price_missing and h2.value == D(100)


def test_sold_out_isin_hidden_but_has_detail(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "5", "120", "2026-02-05T10:00:00Z")])
    conn = client.app.state.conn
    assert services.overview(conn, date(2026, 10, 11)).holdings == []
    d = services.isin_detail(conn, ISIN)
    assert d.realized_total == D(100) and len(d.rows) == 2


def test_oversell_sets_error_instead_of_raising(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "100", "2026-01-06T10:00:00Z")])
    assert services.overview(client.app.state.conn, date(2026, 10, 11)).error
    assert services.isin_detail(client.app.state.conn, ISIN).error


def test_empty_database(client):
    conn = client.app.state.conn
    o = services.overview(conn, date(2026, 10, 11))
    assert o.holdings == [] and o.total_value == D(0) and o.xirr is None
    assert services.value_series(conn) == [] and services.last_session_change(conn) is None


def test_value_series_uses_forward_filled_closes(client):
    seed(client, [trade("a", "BUY", "2", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-01-05", "price": "100"},
                 {"isin": ISIN, "day": "2026-01-06", "price": "110"}])
    s = services.value_series(client.app.state.conn)
    assert [(p.day, p.value, p.invested) for p in s] == [
        (date(2026, 1, 5), 200.0, 200.0), (date(2026, 1, 6), 220.0, 200.0)]


def test_slice_range():
    from portfolio.services import SeriesPoint
    s = [SeriesPoint(date(2026, 1, 1) + __import__("datetime").timedelta(days=i), 1.0, 1.0) for i in range(300)]
    today = s[-1].day
    assert len(services.slice_range(s, "1w", today)) == 8
    assert len(services.slice_range(s, "all", today)) == 300


def test_last_session_change(client):
    seed(client, [trade("a", "BUY", "2", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "100"},
                 {"isin": ISIN, "day": "2026-10-09", "price": "110"}])
    abs_, pct = services.last_session_change(client.app.state.conn)
    assert abs_ == D(20) and pct == D(10)


def test_xirr_hidden_for_young_position(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-10-01T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "101", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    assert services.overview(client.app.state.conn, date(2026, 10, 11)).holdings[0].xirr is None
```

- [ ] **Step 2: Run, expect failure** — `pytest tests/test_services.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `portfolio/services.py`**

```python
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio.fifo import Lot, OversellError, Sale, Trade, replay
from portfolio.xirr import xirr

BERLIN = ZoneInfo("Europe/Berlin")
RANGE_DAYS = {"1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365}
MIN_XIRR_AGE_DAYS = 90


def _day(dt: datetime) -> date:
    return dt.astimezone(BERLIN).date()


def load_trades(conn) -> list[Trade]:
    return [
        Trade(r["id"], r["isin"], r["name"], r["side"], r["kind"], datetime.fromisoformat(r["executed_at"]),
              Decimal(r["quantity"]), Decimal(r["price"]), Decimal(r["fee"]))
        for r in conn.execute("select * from trades order by executed_at, id")
    ]


def _closes(conn) -> dict[str, list[tuple[date, Decimal]]]:
    out: dict[str, list[tuple[date, Decimal]]] = {}
    for r in conn.execute("select isin, day, price from closes order by isin, day"):
        out.setdefault(r["isin"], []).append((date.fromisoformat(r["day"]), Decimal(r["price"])))
    return out


def _price_on(rows: list[tuple[date, Decimal]] | None, day: date) -> Decimal | None:
    if not rows:
        return None
    i = bisect_right([d for d, _ in rows], day)
    return rows[i - 1][1] if i else None


@dataclass
class Holding:
    isin: str
    name: str
    quantity: Decimal
    cost: Decimal
    price: Decimal | None
    value: Decimal
    pnl_abs: Decimal
    pnl_pct: Decimal | None
    share: Decimal
    avg_cost: Decimal
    xirr: float | None
    price_missing: bool


@dataclass
class Overview:
    holdings: list[Holding] = field(default_factory=list)
    total_value: Decimal = Decimal(0)
    total_cost: Decimal = Decimal(0)
    pnl_abs: Decimal = Decimal(0)
    pnl_pct: Decimal | None = None
    xirr: float | None = None
    error: str | None = None


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return part / whole * 100 if whole else None


def _xirr(flows: list[tuple[date, float]], today: date) -> float | None:
    if not flows or (today - min(d for d, _ in flows)).days < MIN_XIRR_AGE_DAYS:
        return None
    return xirr(flows)


def overview(conn, today: date) -> Overview:
    trades = load_trades(conn)
    if not trades:
        return Overview()
    try:
        lots, sales = replay(trades)
    except OversellError as e:
        return Overview(error=str(e))
    quotes = {r["isin"]: Decimal(r["price"]) for r in conn.execute("select isin, price from quotes")}
    closes = _closes(conn)
    names = {t.isin: t.name for t in trades}
    by_isin: dict[str, list[Lot]] = {}
    for lot in lots:
        if lot.remaining > 0:
            by_isin.setdefault(lot.trade.isin, []).append(lot)
    holdings: list[Holding] = []
    for isin, ls in by_isin.items():
        qty = sum((l.remaining for l in ls), Decimal(0))
        cost = sum((l.cost for l in ls), Decimal(0))
        price = quotes.get(isin) or (closes[isin][-1][1] if closes.get(isin) else None)
        value = qty * price if price is not None else cost
        flows = _flows([t for t in trades if t.isin == isin], value, today)
        holdings.append(Holding(isin, names[isin], qty, cost, price, value, value - cost, _pct(value - cost, cost),
                                Decimal(0), cost / qty, _xirr(flows, today), price is None))
    total_value = sum((h.value for h in holdings), Decimal(0))
    total_cost = sum((h.cost for h in holdings), Decimal(0))
    for h in holdings:
        h.share = _pct(h.value, total_value) or Decimal(0)
    holdings.sort(key=lambda h: h.value, reverse=True)
    return Overview(holdings, total_value, total_cost, total_value - total_cost, _pct(total_value - total_cost, total_cost),
                    _xirr(_flows(trades, total_value, today), today))


def _flows(trades: list[Trade], terminal_value: Decimal, today: date) -> list[tuple[date, float]]:
    flows = []
    for t in trades:
        gross = t.quantity * t.price
        flows.append((_day(t.executed_at), float(-(gross + t.fee) if t.side == "BUY" else gross - t.fee)))
    flows.append((today, float(terminal_value)))
    return flows


@dataclass
class SeriesPoint:
    day: date
    value: float
    invested: float


def value_series(conn) -> list[SeriesPoint]:
    trades = load_trades(conn)
    if not trades:
        return []
    closes = _closes(conn)
    first = _day(trades[0].executed_at)
    days = sorted({d for rows in closes.values() for d, _ in rows if d >= first})
    series: list[SeriesPoint] = []
    for day in days:
        upto = [t for t in trades if _day(t.executed_at) <= day]
        try:
            lots, _ = replay(upto)
        except OversellError:
            return []
        value = invested = Decimal(0)
        for lot in lots:
            if lot.remaining <= 0:
                continue
            price = _price_on(closes.get(lot.trade.isin), day)
            invested += lot.cost
            value += lot.remaining * price if price is not None else lot.cost
        series.append(SeriesPoint(day, float(value), float(invested)))
    return series


def slice_range(series: list[SeriesPoint], key: str, today: date) -> list[SeriesPoint]:
    if key not in RANGE_DAYS:
        return series
    start = today - timedelta(days=RANGE_DAYS[key])
    return [p for p in series if p.day >= start]


def last_session_change(conn) -> tuple[Decimal, Decimal] | None:
    trades = load_trades(conn)
    if not trades:
        return None
    try:
        lots, _ = replay(trades)
    except OversellError:
        return None
    closes = _closes(conn)
    qty: dict[str, Decimal] = {}
    for lot in lots:
        qty[lot.trade.isin] = qty.get(lot.trade.isin, Decimal(0)) + lot.remaining
    change = prev_value = Decimal(0)
    for isin, q in qty.items():
        rows = closes.get(isin, [])
        if q > 0 and len(rows) >= 2:
            change += q * (rows[-1][1] - rows[-2][1])
            prev_value += q * rows[-2][1]
    return (change, change / prev_value * 100) if prev_value else None


@dataclass
class TxRow:
    trade: Trade
    paid: Decimal            # cost for a buy, proceeds for a sell
    current_value: Decimal | None
    pnl_pct: Decimal | None
    remaining: Decimal | None
    realized: Decimal | None


@dataclass
class IsinDetail:
    name: str = ""
    rows: list[TxRow] = field(default_factory=list)
    realized_total: Decimal = Decimal(0)
    error: str | None = None


def isin_detail(conn, isin: str) -> IsinDetail:
    trades = [t for t in load_trades(conn) if t.isin == isin]
    if not trades:
        return IsinDetail()
    detail = IsinDetail(name=trades[0].name)
    try:
        lots, sales = replay(trades)
    except OversellError as e:
        detail.error = str(e)
        return detail
    row = conn.execute("select price from quotes where isin=?", (isin,)).fetchone()
    closes = _closes(conn).get(isin)
    price = Decimal(row["price"]) if row else (closes[-1][1] if closes else None)
    lot_by_id = {l.trade.id: l for l in lots}
    sale_by_id = {s.trade.id: s for s in sales}
    for t in trades:
        if t.side == "BUY":
            lot = lot_by_id[t.id]
            paid = t.quantity * t.price + t.fee
            value = lot.remaining * price if price is not None else None
            pct = _pct(value - lot.cost, lot.cost) if value is not None and lot.remaining > 0 else None
            detail.rows.append(TxRow(t, paid, value, pct, lot.remaining, None))
        else:
            s = sale_by_id[t.id]
            detail.rows.append(TxRow(t, s.proceeds, None, _pct(s.realized, s.cost_basis), None, s.realized))
            detail.realized_total += s.realized
    return detail
```

- [ ] **Step 4: Run, expect pass** — `pytest tests/test_services.py -v` → 9 passed. If `test_overview_values_holding_with_quote` fails only on `h.xirr is not None`, note that the quote is dated 2026-10-09 and the buy 2026-01-05 (>90 days), so XIRR must be non-None; fix the code, not the test.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add holdings, value series and per-ETF detail services"`

---

### Task 6: Pages

**Files:**
- Create: `portfolio/web.py`, `portfolio/templates/{base,portfolio,etf_list,etf_detail}.html`, `portfolio/static/chart.umd.min.js`, `tests/test_web.py`
- Modify: `portfolio/app.py` (include router, mount static)

**Consumes:** everything in `services`.

**Produces:** `GET /` (portfolio, `?range=` one of `1w,1m,3m,6m,1y,all`, default `1y`), `GET /etf`, `GET /etf/{isin}`, `GET /healthz`; Jinja filters `eur`, `pct`, `qty`.

- [ ] **Step 1: Vendor Chart.js**

```bash
mkdir -p portfolio/static portfolio/templates
curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js -o portfolio/static/chart.umd.min.js
test "$(wc -c < portfolio/static/chart.umd.min.js)" -gt 100000 && echo ok
```

- [ ] **Step 2: Write the failing tests** (`tests/test_web.py`)

```python
from tests.conftest import AUTH, payload
from tests.test_services import ISIN, seed, trade


def test_pages_render_when_empty(client):
    for url in ("/", "/etf", f"/etf/{ISIN}", "/healthz"):
        assert client.get(url).status_code == 200
    assert "No data yet" in client.get("/").text


def test_portfolio_page_shows_totals_and_ranges(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z")],
         closes=[{"isin": ISIN, "day": "2026-10-08", "price": "100"}, {"isin": ISIN, "day": "2026-10-09", "price": "110"}],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    html = client.get("/?range=3m").text
    assert "550.00" in html and "+10.00" in html
    for key in ("1w", "1m", "3m", "6m", "1y", "all"):
        assert f"range={key}" in html


def test_etf_list_and_detail(client):
    seed(client, [trade("a", "BUY", "5", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "120", "2026-02-05T10:00:00Z")],
         quotes=[{"isin": ISIN, "price": "110", "quoted_at": "2026-10-09T20:59:00Z", "outdated": False}])
    assert ISIN in client.get("/etf").text
    html = client.get(f"/etf/{ISIN}").text
    assert "2026-01-05" in html and "BUY" in html and "SELL" in html and "+40.00" in html


def test_oversell_shows_banner_not_500(client):
    seed(client, [trade("a", "BUY", "1", "100", "2026-01-05T10:00:00Z"),
                  trade("b", "SELL", "2", "100", "2026-01-06T10:00:00Z")])
    for url in ("/", "/etf", f"/etf/{ISIN}"):
        r = client.get(url)
        assert r.status_code == 200 and "Data error" in r.text


def test_stale_banner_when_last_ingest_old(client):
    client.post("/api/ingest", headers=AUTH, json=payload(fetched_at="2020-01-01T00:00:00+00:00"))
    assert 'class="stale"' in client.get("/").text
```

- [ ] **Step 3: Run, expect failure** — `pytest tests/test_web.py -v` → FAIL (no routes).

- [ ] **Step 4: Implement `portfolio/web.py`**

```python
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from portfolio import services

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STALE_AFTER_HOURS = 36
RANGES = ["1w", "1m", "3m", "6m", "1y", "all"]


def _eur(v) -> str:
    return "n/a" if v is None else f"{Decimal(v):,.2f}"


def _signed(v) -> str:
    return "n/a" if v is None else f"{Decimal(v):+,.2f}"


def _qty(v) -> str:
    return "n/a" if v is None else f"{Decimal(v).normalize():f}"


templates.env.filters.update(eur=_eur, signed=_signed, qty=_qty)


def _last_ingest(conn) -> datetime | None:
    row = conn.execute("select value from meta where key='last_ingest'").fetchone()
    return datetime.fromisoformat(row["value"]) if row else None


def _ctx(request: Request, **extra) -> dict:
    last = _last_ingest(request.app.state.conn)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600 if last else None
    return {"request": request, "last_ingest": last, "stale": last is None or age_h > STALE_AFTER_HOURS, **extra}


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/")
async def portfolio_page(request: Request, range: str = "1y"):
    conn = request.app.state.conn
    today = date.today()
    ov = services.overview(conn, today)
    series = services.slice_range(services.value_series(conn), range, today)
    return templates.TemplateResponse(request, "portfolio.html", _ctx(
        request, ov=ov, range=range, ranges=RANGES, change=services.last_session_change(conn),
        labels=[p.day.isoformat() for p in series], values=[p.value for p in series],
        invested=[p.invested for p in series]))


@router.get("/etf")
async def etf_list(request: Request):
    ov = services.overview(request.app.state.conn, date.today())
    return templates.TemplateResponse(request, "etf_list.html", _ctx(request, ov=ov))


@router.get("/etf/{isin}")
async def etf_detail(request: Request, isin: str):
    detail = services.isin_detail(request.app.state.conn, isin)
    return templates.TemplateResponse(request, "etf_detail.html", _ctx(request, isin=isin, d=detail))
```
Edit `portfolio/app.py` to add:
```python
from fastapi.staticfiles import StaticFiles
from portfolio import web
...
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(web.router)
```

- [ ] **Step 5: Templates**

`portfolio/templates/base.html`:
```html
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1c1f24}
 nav{background:#1c1f24;padding:.7rem 1.2rem}nav a{color:#fff;margin-right:1.2rem;text-decoration:none}
 main{max-width:1100px;margin:1.2rem auto;padding:0 1rem}
 table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:.45rem .6rem;border-bottom:1px solid #e3e6ea;text-align:right}
 th:first-child,td:first-child{text-align:left}.pos{color:#0a7d33}.neg{color:#b3261e}
 .stale{background:#fde7e7;color:#8a1c14;padding:.5rem 1rem;margin-bottom:1rem}.err{background:#fff3cd;padding:.5rem 1rem;margin-bottom:1rem}
 .cards{display:flex;gap:1rem;margin-bottom:1rem;flex-wrap:wrap}.card{background:#fff;padding:.8rem 1.2rem;flex:1;min-width:160px}
 .card b{display:block;font-size:1.4rem}.muted{color:#6b7280}.ranges a{margin-right:.6rem}
</style></head><body>
<nav><a href="/">Portfolio</a><a href="/etf">ETFs</a></nav>
<main>
{% if stale %}<div class="stale">{% if last_ingest %}Last updated {{ last_ingest.strftime('%Y-%m-%d %H:%M') }} UTC, data may be stale.{% else %}No data yet. The fetcher has not delivered anything.{% endif %}</div>
{% else %}<p class="muted">Last updated {{ last_ingest.strftime('%Y-%m-%d %H:%M') }} UTC</p>{% endif %}
{% block body %}{% endblock %}
</main></body></html>
```

`portfolio/templates/portfolio.html`:
```html
{% extends "base.html" %}{% block body %}
{% if ov.error %}<div class="err">Data error: {{ ov.error }}</div>{% endif %}
{% if not ov.holdings and not ov.error %}<p>No data yet.</p>{% else %}
<div class="cards">
 <div class="card">Value<b>{{ ov.total_value|eur }} €</b></div>
 <div class="card">Invested<b>{{ ov.total_cost|eur }} €</b></div>
 <div class="card">Profit / loss<b class="{{ 'pos' if ov.pnl_abs >= 0 else 'neg' }}">{{ ov.pnl_abs|signed }} € ({{ ov.pnl_pct|signed }} %)</b></div>
 <div class="card">XIRR<b>{% if ov.xirr is not none %}{{ '%+.2f'|format(ov.xirr*100) }} %{% else %}n/a{% endif %}</b></div>
 <div class="card">Last session<b>{% if change %}{{ change[0]|signed }} € ({{ change[1]|signed }} %){% else %}n/a{% endif %}</b></div>
</div>
<p class="ranges">{% for r in ranges %}<a href="/?range={{ r }}">{{ '<b>' if r == range }}{{ r }}{{ '</b>' if r == range }}</a>{% endfor %}</p>
<canvas id="chart" height="110"></canvas>
<script src="/static/chart.umd.min.js"></script>
<script>
new Chart(document.getElementById('chart'), {type:'line',
 data:{labels:{{ labels|tojson }},datasets:[
  {label:'Value (EUR)',data:{{ values|tojson }},borderColor:'#2563eb',pointRadius:0,tension:.1},
  {label:'Invested (EUR)',data:{{ invested|tojson }},borderColor:'#9ca3af',pointRadius:0,borderDash:[5,4]}]},
 options:{interaction:{mode:'index',intersect:false}}});
</script>
{% endif %}{% endblock %}
```
(The `<b>` markers in the range links are autoescaped by Jinja, so use a CSS class instead: replace the loop body with `<a href="/?range={{ r }}"{% if r == range %} style="font-weight:700"{% endif %}>{{ r }}</a>`. Use this version.)

`portfolio/templates/etf_list.html`:
```html
{% extends "base.html" %}{% block body %}
{% if ov.error %}<div class="err">Data error: {{ ov.error }}</div>{% endif %}
{% if not ov.holdings and not ov.error %}<p>No data yet.</p>{% else %}
<table><tr><th>ETF</th><th>Quantity</th><th>Avg cost</th><th>Price</th><th>Value</th><th>Share</th><th>P/L</th><th>P/L %</th><th>XIRR</th></tr>
{% for h in ov.holdings %}
<tr><td><a href="/etf/{{ h.isin }}">{{ h.name }}</a><br><span class="muted">{{ h.isin }}</span></td>
<td>{{ h.quantity|qty }}</td><td>{{ h.avg_cost|eur }}</td><td>{{ h.price|eur }}</td><td>{{ h.value|eur }}</td>
<td>{{ h.share|eur }} %</td>
<td class="{{ 'pos' if h.pnl_abs >= 0 else 'neg' }}">{{ h.pnl_abs|signed }}</td>
<td class="{{ 'pos' if h.pnl_abs >= 0 else 'neg' }}">{{ h.pnl_pct|signed }} %</td>
<td>{% if h.xirr is not none %}{{ '%+.2f'|format(h.xirr*100) }} %{% else %}n/a{% endif %}</td></tr>
{% endfor %}</table>{% endif %}{% endblock %}
```

`portfolio/templates/etf_detail.html`:
```html
{% extends "base.html" %}{% block body %}
{% if d.error %}<div class="err">Data error: {{ d.error }}</div>{% endif %}
{% if not d.rows and not d.error %}<p>No data yet.</p>{% else %}
<h2>{{ d.name }} <span class="muted">{{ isin }}</span></h2>
<p>Realized P/L: <b>{{ d.realized_total|signed }} €</b></p>
<table><tr><th>Date</th><th>Type</th><th>Quantity</th><th>Price</th><th>Sum</th><th>Fee</th><th>Current value</th><th>Held</th><th>P/L %</th><th>Realized</th></tr>
{% for r in d.rows %}
<tr><td>{{ r.trade.executed_at.astimezone().strftime('%Y-%m-%d') }}</td>
<td>{{ r.trade.side }}{% if r.trade.kind == 'SAVINGS_PLAN' %} <span class="muted">(plan)</span>{% endif %}</td>
<td>{{ r.trade.quantity|qty }}</td><td>{{ r.trade.price|eur }}</td><td>{{ r.paid|eur }}</td><td>{{ r.trade.fee|eur }}</td>
<td>{{ r.current_value|eur if r.current_value is not none else '' }}</td>
<td>{{ r.remaining|qty if r.remaining is not none else '' }}</td>
<td class="{{ 'pos' if r.pnl_pct is not none and r.pnl_pct >= 0 else 'neg' }}">{{ r.pnl_pct|signed if r.pnl_pct is not none else '' }}{{ ' %' if r.pnl_pct is not none }}</td>
<td>{{ r.realized|signed if r.realized is not none else '' }}</td></tr>
{% endfor %}</table>{% endif %}{% endblock %}
```
Note: the date column must be in Europe/Berlin, not the server's zone. Replace `r.trade.executed_at.astimezone()` by a filter: add to `web.py` `templates.env.filters["berlin_date"] = lambda dt: dt.astimezone(services.BERLIN).strftime("%Y-%m-%d")` and use `{{ r.trade.executed_at|berlin_date }}`.

- [ ] **Step 6: Run, expect pass** — `pytest tests/test_web.py -v` → 5 passed. Then run the full suite: `pytest -v`.

- [ ] **Step 7: Commit** — `git add -A && git commit -m "Add portfolio, ETF list and ETF detail pages"`

---

### Task 7: Metrics endpoint and entrypoint

**Files:**
- Modify: `portfolio/web.py` (add `/metrics`)
- Create: `portfolio/main.py`, `tests/test_metrics.py`

**Produces:** `GET /metrics` with `portfolio_last_ingest_timestamp_seconds` (0 if never) and `portfolio_trades_total`; ASGI entry `portfolio.main:app` reading `DB_PATH` and `INGEST_TOKEN` from the environment.

- [ ] **Step 1: Write the failing test**

```python
from tests.conftest import AUTH, payload


def test_metrics_empty_then_after_ingest(client):
    text = client.get("/metrics").text
    assert "portfolio_last_ingest_timestamp_seconds 0" in text
    client.post("/api/ingest", headers=AUTH, json=payload(fetched_at="2026-10-11T21:30:00+00:00"))
    text = client.get("/metrics").text
    assert "portfolio_last_ingest_timestamp_seconds 1791754200" in text
    assert "portfolio_trades_total 1" in text
```
(1791754200 is 2026-10-11T21:30:00Z; verify with `python -c "from datetime import datetime,timezone; print(int(datetime(2026,10,11,21,30,tzinfo=timezone.utc).timestamp()))"` and correct the literal in the test if it differs.)

- [ ] **Step 2: Run, expect failure** — 404 on `/metrics`.

- [ ] **Step 3: Implement** — append to `portfolio/web.py`:

```python
from fastapi.responses import PlainTextResponse


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    conn = request.app.state.conn
    last = _last_ingest(conn)
    n = conn.execute("select count(*) from trades").fetchone()[0]
    return (
        "# TYPE portfolio_last_ingest_timestamp_seconds gauge\n"
        f"portfolio_last_ingest_timestamp_seconds {int(last.timestamp()) if last else 0}\n"
        "# TYPE portfolio_trades_total gauge\n"
        f"portfolio_trades_total {n}\n"
    )
```
`portfolio/main.py`:
```python
import os

from portfolio.app import create_app

app = create_app(os.environ.get("DB_PATH", "/data/portfolio.db"), os.environ["INGEST_TOKEN"])
```

- [ ] **Step 4: Run, expect pass** — `pytest -v` → all pass.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add metrics endpoint and ASGI entrypoint"`

---

### Task 8: Fetcher parsers (pure functions)

**Files:**
- Create: `fetcher/__init__.py`, `fetcher/normalize.py`, `tests/test_normalize.py`

**Produces:**
- `unwrap(payload) -> dict`
- `parse_transactions(payload) -> tuple[list[dict], str | None]` (trade dicts as in the ingest body without `fee`; second value is `page.nextCursor`)
- `fee_from_details(payload) -> Decimal`
- `parse_chart(payload) -> list[dict]` (`{"day": "YYYY-MM-DD", "price": str}`, one per Berlin day, last point wins)
- `parse_quote(payload) -> dict` (`{"isin","price","quoted_at","outdated"}`)

The fixtures below are trimmed from real MCP outputs captured on 2026-10-11.

- [ ] **Step 1: Write the failing tests** (`tests/test_normalize.py`)

```python
import json
from decimal import Decimal as D

from fetcher import normalize as n

LIST = {"portfolioId": "p", "page": {"nextCursor": None, "totalCount": 4, "returnedCount": 4}, "transactions": [
    {"id": "i1", "kind": "security", "status": "SETTLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-09-14T09:00:48.260Z", "description": "Test World ETF (Acc)",
     "security": {"isin": "IE0000000001", "transactionType": "SINGLE", "quantity": "5", "amount": "-2100",
                  "side": "BUY", "limitPrice": "735", "stopPrice": None}, "cash": None},
    {"id": "i2", "kind": "security", "status": "SETTLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-09-10T12:13:15.145Z", "description": "Test Emerging ETF (Acc)",
     "security": {"isin": "IE0000000002", "transactionType": "SAVINGS_PLAN", "quantity": "0.296736",
                  "amount": "-25.0000", "side": "BUY", "limitPrice": None, "stopPrice": None}, "cash": None},
    {"id": "i3", "kind": "security", "status": "CANCELLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-10-05T07:24:03.452Z", "description": "Test Ex-US ETF (Acc)",
     "security": {"isin": "IE0000000003", "transactionType": "SINGLE", "quantity": "486", "amount": "0",
                  "side": "BUY", "limitPrice": "6.15", "stopPrice": None}, "cash": None},
    {"id": "c1", "kind": "cash", "status": "SETTLED", "isCancellation": False, "currency": "EUR",
     "lastEventAt": "2026-10-08T00:00:00.000Z", "description": "Sparpläne", "security": None,
     "cash": {"relatedIsin": None, "transactionType": "DEPOSIT", "amount": "250"}},
]}


def test_parse_transactions_keeps_only_settled_security_trades():
    trades, cursor = n.parse_transactions(LIST)
    assert [t["id"] for t in trades] == ["i1", "i2"] and cursor is None
    assert trades[0]["price"] == "700.000000"          # abs(amount)/quantity, not limitPrice 735
    assert trades[1]["kind"] == "SAVINGS_PLAN" and trades[1]["price"] == "84.249973"


def test_unwrap_handles_hermes_result_wrapper():
    wrapped = {"result": json.dumps(LIST)}
    assert n.unwrap(wrapped) == LIST and n.unwrap(json.dumps(LIST)) == LIST


def details(total, market):
    return {"transaction": {"securityTrade": {"totalAmount": total,
            "tradeTransactionAmounts": {"marketValuation": market}}}}


def test_fee_from_details():
    assert n.fee_from_details(details("-2100", "2100")) == D(0)
    assert n.fee_from_details(details("-2100.99", "2100")) == D("0.99")
    assert n.fee_from_details(details("-25.0000", None)) == D(0)
    assert n.fee_from_details({"transaction": {"securityTrade": {"totalAmount": "0", "tradeTransactionAmounts": None}}}) == D(0)


def test_parse_chart_one_point_per_berlin_day_last_wins():
    payload = {"points": [
        {"midPrice": 100.0, "timestampUtc": "2026-10-08T07:29:59.000Z"},
        {"midPrice": 101.5, "timestampUtc": "2026-10-08T20:59:57.000Z"},
        {"midPrice": 102.0, "timestampUtc": "2026-10-09T20:59:56.000Z"}]}
    assert n.parse_chart(payload) == [{"day": "2026-10-08", "price": "101.5"}, {"day": "2026-10-09", "price": "102.0"}]


def test_parse_chart_finds_points_under_any_key():
    nested = {"security": {"chart": {"series": [{"midPrice": 1, "timestampUtc": "2026-01-02T21:59:30.000Z"}]}}}
    assert n.parse_chart(nested) == [{"day": "2026-01-02", "price": "1"}]


def test_parse_quote():
    payload = {"security": {"isin": "IE0000000001", "quote": {"midPrice": 710.50, "bidPrice": 709.90,
               "timestampUtc": "2026-10-09T20:59:56.615Z", "isOutdated": True}}}
    assert n.parse_quote(payload) == {"isin": "IE0000000001", "price": "710.50",
                                      "quoted_at": "2026-10-09T20:59:56.615Z", "outdated": True}
```

- [ ] **Step 2: Run, expect failure** — `pytest tests/test_normalize.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `fetcher/normalize.py`** (empty `fetcher/__init__.py`)

```python
import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
SIX_PLACES = Decimal("0.000001")


def unwrap(payload):
    """Hermes prints {"result": "<json>"}; the MCP SDK yields the inner JSON text."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict) and set(payload) == {"result"} and isinstance(payload["result"], str):
        return json.loads(payload["result"])
    return payload


def parse_transactions(payload) -> tuple[list[dict], str | None]:
    data = unwrap(payload)
    trades = []
    for row in data["transactions"]:
        sec = row.get("security")
        if row.get("kind") != "security" or sec is None:
            continue
        if row.get("status") != "SETTLED" or row.get("isCancellation"):
            continue
        qty, amount = Decimal(sec["quantity"]), Decimal(sec["amount"])
        if qty == 0:
            continue
        trades.append({
            "id": row["id"], "isin": sec["isin"], "name": row["description"], "side": sec["side"],
            "kind": sec["transactionType"], "executed_at": row["lastEventAt"],
            "quantity": str(qty), "amount": str(amount),
            "price": str((abs(amount) / qty).quantize(SIX_PLACES, ROUND_HALF_UP)),
        })
    return trades, data["page"]["nextCursor"]


def fee_from_details(payload) -> Decimal:
    trade = unwrap(payload)["transaction"]["securityTrade"]
    total = trade.get("totalAmount")
    market = (trade.get("tradeTransactionAmounts") or {}).get("marketValuation")
    if total is None or market is None:
        return Decimal(0)
    return abs(abs(Decimal(total)) - Decimal(market))


def _find_points(obj):
    if isinstance(obj, list):
        if obj and all(isinstance(p, dict) and "midPrice" in p and "timestampUtc" in p for p in obj):
            return obj
        for item in obj:
            found = _find_points(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for value in obj.values():
            found = _find_points(value)
            if found:
                return found
    return []


def parse_chart(payload) -> list[dict]:
    by_day: dict[str, str] = {}
    points = sorted(_find_points(unwrap(payload)), key=lambda p: p["timestampUtc"])
    for p in points:
        ts = datetime.fromisoformat(p["timestampUtc"].replace("Z", "+00:00"))
        by_day[ts.astimezone(BERLIN).date().isoformat()] = str(p["midPrice"])
    return [{"day": d, "price": price} for d, price in sorted(by_day.items())]


def parse_quote(payload) -> dict:
    sec = unwrap(payload)["security"]
    q = sec["quote"]
    return {"isin": sec["isin"], "price": str(q["midPrice"]), "quoted_at": q["timestampUtc"],
            "outdated": bool(q["isOutdated"])}
```

- [ ] **Step 4: Run, expect pass** — 6 passed.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add MCP payload parsers for the fetcher"`

---

### Task 9: Fetcher access token

**Files:**
- Create: `fetcher/auth_hermes.py` (primary, Steps 5-6), `fetcher/auth_tokenfile.py` and `tests/test_auth_tokenfile.py` (fallback, Steps 1-4), `fetcher/entry.py` (Step 7)

**Consumes:** Task 0 result ("import" works).

**Produces:**
- `auth_hermes.build_auth(url: str) -> httpx2.Auth`, used by `run.py` (Task 10). Primary.
- `auth_tokenfile.get_access_token(token_dir, name="scalable", now=time.time, client=None) -> str`, the fallback if Hermes' internals change; it is built and tested now so the fallback exists, but `run.py` does not use it.
- `fetcher/entry.py`: the systemd entry point that runs `fetcher.run.main()` inside Hermes' interpreter.

Steps 1-4 (tokenfile fallback) may be done after Steps 5-7 if time is short; nothing else depends on them.

- [ ] **Step 1: Write the failing tests**

```python
import json
import os
import stat

import httpx

from fetcher.auth_tokenfile import get_access_token


def make_dir(tmp_path, expires_at):
    (tmp_path / "scalable.json").write_text(json.dumps({
        "access_token": "old", "token_type": "Bearer", "expires_in": 3600, "scope": "x",
        "refresh_token": "r1", "expires_at": expires_at, "hermes_issuer": "i"}))
    (tmp_path / "scalable.meta.json").write_text(json.dumps({"token_endpoint": "https://auth.example/token"}))
    (tmp_path / "scalable.client.json").write_text(json.dumps({"client_id": "cid"}))
    return tmp_path


def test_valid_token_makes_no_http_call(tmp_path):
    d = make_dir(tmp_path, expires_at=10_000)
    def boom(request): raise AssertionError("no http expected")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert get_access_token(d, now=lambda: 1000, client=client) == "old"


def test_expired_token_is_refreshed_and_persisted(tmp_path):
    d = make_dir(tmp_path, expires_at=1000)
    seen = {}
    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600, "refresh_token": "r2"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_access_token(d, now=lambda: 2000, client=client) == "new"
    assert "grant_type=refresh_token" in seen["body"] and "refresh_token=r1" in seen["body"] and "client_id=cid" in seen["body"]
    saved = json.loads((d / "scalable.json").read_text())
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r2"
    assert saved["expires_at"] == 2000 + 3600 and saved["hermes_issuer"] == "i"   # foreign fields survive
    assert stat.S_IMODE(os.stat(d / "scalable.json").st_mode) == 0o600


def test_refresh_failure_raises(tmp_path):
    import pytest
    d = make_dir(tmp_path, expires_at=1000)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(httpx.HTTPStatusError):
        get_access_token(d, now=lambda: 2000, client=client)
```

- [ ] **Step 2: Run, expect failure** — module missing.

- [ ] **Step 3: Implement `fetcher/auth_tokenfile.py`**

```python
import fcntl
import json
import os
import time
from pathlib import Path

import httpx

SKEW_SECONDS = 120


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def get_access_token(token_dir, name: str = "scalable", now=time.time, client=None) -> str:
    d = Path(token_dir)
    token_path = d / f"{name}.json"
    tok = _read(token_path)
    if tok["expires_at"] - SKEW_SECONDS > now():
        return tok["access_token"]
    meta, registered = _read(d / f"{name}.meta.json"), _read(d / f"{name}.client.json")
    with open(d / f"{name}.json.refresh.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tok = _read(token_path)  # another process (Hermes) may have refreshed while we waited
        if tok["expires_at"] - SKEW_SECONDS > now():
            return tok["access_token"]
        post = (client or httpx).post
        resp = post(meta["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": registered["client_id"]}, timeout=30)
        resp.raise_for_status()
        new = resp.json()
        tok.update(access_token=new["access_token"], expires_in=new["expires_in"],
                   expires_at=now() + new["expires_in"])
        if "refresh_token" in new:
            tok["refresh_token"] = new["refresh_token"]
        tmp = d / f"{name}.json.tmp"
        tmp.write_text(json.dumps(tok))
        os.chmod(tmp, 0o600)
        os.replace(tmp, token_path)
        return tok["access_token"]
```

- [ ] **Step 4: Run, expect pass** — 3 passed.

- [ ] **Step 5: Add the primary implementation `fetcher/auth_hermes.py`** (Task 0 chose "import")

Unlike the tokenfile code, this returns an `httpx2.Auth` object, not a token string: the SDK refreshes the token itself, under Hermes' own lock. It only works inside Hermes' interpreter (Task 13 runs the fetcher that way); there is no unit test for it, the proof is Step 6.

```python
import sys

HERMES_DIR = "/home/hermes/.hermes/hermes-agent"
SERVER = "scalable"
REDIRECT_PORT = 8765  # same as mcp_servers.scalable.oauth.redirect_port in Hermes' config


def build_auth(url: str):
    """OAuth provider backed by Hermes' token store. Raises if no cached tokens exist."""
    if HERMES_DIR not in sys.path:
        sys.path.insert(0, HERMES_DIR)
    import hermes_bootstrap  # noqa: F401  (adds Hermes' venv with `mcp` to sys.path)
    from tools.mcp_oauth import build_oauth_auth

    return build_oauth_auth(SERVER, url, {"redirect_port": REDIRECT_PORT})
```

- [ ] **Step 6: Verify on raspi2 (this already passed in Task 0 with an equivalent script)**

```bash
scp fetcher/auth_hermes.py raspi2:/tmp/auth_hermes.py
ssh raspi2 'PY=$(ls -d /home/hermes/.hermes/tools/python-*/bin/python3 | sort -V | tail -1); cd /tmp && sudo -u hermes $PY -I -c "
import sys; sys.path.insert(0, \"/tmp\")
from auth_hermes import build_auth
print(type(build_auth(\"https://mcp.scalable.capital/mcp\")).__name__)"; rm /tmp/auth_hermes.py'
```
Expected: `HermesOAuthClientProvider`. If Hermes' update breaks this later, the fetcher fails loudly and the staleness alert fires; the fallback is `fetcher/auth_tokenfile.py` (Steps 1-4), which would then need `run.py` to pass `headers={"Authorization": "Bearer ..."}` instead of `auth=`.

- [ ] **Step 7: Create `fetcher/entry.py`** (run inside Hermes' interpreter with `-I`, which ignores `PYTHONPATH`, so the repo root must be added by hand)

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetcher.run import main  # noqa: E402

sys.exit(main())
```

- [ ] **Step 8: Commit** — `git add -A && git commit -m "Add fetcher auth via Hermes OAuth provider, tokenfile fallback and entry point"`


---

### Task 10: Fetcher run (MCP session, collect, push)

**Files:**
- Create: `fetcher/run.py`, `tests/test_run.py`

**Consumes:** `normalize.*`, `auth.get_access_token`.

**Produces:** `python -m fetcher.run` (exit 0 on success, non-zero on any failure). Importable pieces: `collect(call, state_dir) -> dict` where `call` is `async (tool: str, args: dict) -> dict`, and `push(body, ingest_url, ingest_token, client)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_run.py`)

```python
import json

from fetcher import run
from tests.test_normalize import LIST


def details(total="-2100", market="2100"):
    return {"transaction": {"securityTrade": {"totalAmount": total, "tradeTransactionAmounts": {"marketValuation": market}}}}


def fake_call(log):
    async def call(tool, args):
        log.append((tool, args))
        if tool == "list_portfolio_transactions":
            return LIST
        if tool == "get_transaction_details":
            return details("-2100.99") if args["transactionId"] == "i1" else details("-25.0000", "25.0000")
        if tool == "get_security_chart":
            return {"points": [{"midPrice": 100, "timestampUtc": "2026-10-09T20:59:56.000Z"}]}
        if tool == "get_security_quote":
            return {"security": {"isin": args["isin"], "quote": {"midPrice": 101, "timestampUtc": "2026-10-09T20:59:56Z", "isOutdated": False}}}
        raise AssertionError(tool)
    return call


async def test_collect_builds_ingest_body_and_caches_fees(tmp_path):
    log = []
    body = await run.collect(fake_call(log), tmp_path)
    assert [t["id"] for t in body["trades"]] == ["i1", "i2"]
    assert body["trades"][0]["fee"] == "0.99" and body["trades"][1]["fee"] == "0"
    assert {c["isin"] for c in body["closes"]} == {"IE0000000001", "IE0000000002"}
    assert len(body["quotes"]) == 2
    assert {t for t, _ in log} <= {"list_portfolio_transactions", "get_transaction_details", "get_security_chart", "get_security_quote"}
    # second run: details come from the cache
    log.clear()
    await run.collect(fake_call(log), tmp_path)
    assert not any(t == "get_transaction_details" for t, _ in log)


async def test_collect_follows_cursor_and_stops(tmp_path):
    pages = [dict(LIST, page={"nextCursor": "c1", "totalCount": 9, "returnedCount": 4}), dict(LIST, page={"nextCursor": None})]
    seen = []
    async def call(tool, args):
        if tool == "list_portfolio_transactions":
            seen.append(args.get("cursor"))
            return pages.pop(0)
        return await fake_call([])(tool, args)
    await run.collect(call, tmp_path)
    assert seen == [None, "c1"]


def test_push_sends_bearer_and_propagates_errors():
    import io
    import urllib.error

    import pytest
    captured = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def ok(req, timeout):
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data)
        return Resp(b"{}")

    run.push({"trades": []}, "https://x/api/ingest", "tok", opener=ok)
    assert captured["auth"] == "Bearer tok" and captured["body"] == {"trades": []}

    def bad(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        run.push({"trades": []}, "https://x/api/ingest", "tok", opener=bad)
```
The tool argument name for `get_transaction_details` is assumed to be `transactionId`. Before relying on it, confirm the real parameter name on raspi2 with `hermes chat --oneshot -Q -q 'Print the input schema of the scalable MCP tool get_transaction_details.'` and adjust both this test and `run.py` if it differs.

- [ ] **Step 2: Run, expect failure** — `pytest tests/test_run.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `fetcher/run.py`**

```python
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import urllib.request

from fetcher import normalize

log = logging.getLogger("fetcher")
MAX_PAGES = 50


async def collect(call, state_dir) -> dict:
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    fee_cache_path = state / "fees.json"
    fees = json.loads(fee_cache_path.read_text()) if fee_cache_path.exists() else {}

    trades, cursor = [], None
    for _ in range(MAX_PAGES):
        args = {"pageSize": 100}
        if cursor:
            args["cursor"] = cursor
        page, cursor = normalize.parse_transactions(await call("list_portfolio_transactions", args))
        trades += page
        if not cursor:
            break

    for t in trades:
        if t["id"] not in fees:
            fees[t["id"]] = str(normalize.fee_from_details(await call("get_transaction_details", {"transactionId": t["id"]})))
        t["fee"] = fees[t["id"]]
    fee_cache_path.write_text(json.dumps(fees))

    closes, quotes = [], []
    for isin in sorted({t["isin"] for t in trades}):
        chart = await call("get_security_chart", {"isin": isin, "timeframe": "year_to_date"})
        closes += [{"isin": isin, **c} for c in normalize.parse_chart(chart)]
        quotes.append(normalize.parse_quote(await call("get_security_quote", {"isin": isin})))
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "trades": trades, "closes": closes, "quotes": quotes}


def push(body: dict, ingest_url: str, ingest_token: str, opener=urllib.request.urlopen) -> None:
    """Standard library only: Hermes' interpreter has no `httpx`. HTTPError (non-2xx) propagates."""
    req = urllib.request.Request(
        ingest_url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {ingest_token}", "Content-Type": "application/json"})
    with opener(req, timeout=60) as resp:
        resp.read()


async def _main() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    from fetcher.auth_hermes import build_auth

    url = os.environ.get("MCP_URL", "https://mcp.scalable.capital/mcp")
    http_client = create_mcp_http_client(auth=build_auth(url))
    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()

                async def call(tool: str, args: dict) -> dict:
                    result = await session.call_tool(tool, args)
                    text = result.content[0].text
                    if result.is_error:
                        raise RuntimeError(f"{tool} failed: {text}")
                    return json.loads(text)

                body = await collect(call, os.environ["STATE_DIR"])
    push(body, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"])
    log.info("pushed %d trades, %d closes, %d quotes", len(body["trades"]), len(body["closes"]), len(body["quotes"]))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_main())
    except Exception:
        log.exception("fetch failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run, expect pass** — `pytest -v` → all pass.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add fetcher run: MCP collection and push to ingest"`

---

### Task 11: Dockerfile and image pipeline

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `.github/workflows/image.yml`, `README.md`

- [ ] **Step 1: `Dockerfile`**

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY portfolio ./portfolio
RUN pip install --no-cache-dir . && useradd -u 1000 -m app && mkdir /data && chown 1000 /data
USER 1000
ENV DB_PATH=/data/portfolio.db
EXPOSE 8000
CMD ["uvicorn", "portfolio.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```
`.dockerignore`: `.venv`, `.git`, `tests`, `*.db`, `fetcher`. Note: `pyproject.toml` lists `fetcher` in packages; for the image, install only `portfolio` — change `[tool.setuptools] packages` to `["portfolio", "fetcher"]` is fine because the `fetcher` directory is missing in the image build context and setuptools would fail. Avoid that by switching the `[tool.setuptools]` block to `packages = ["portfolio"]` plus `[tool.setuptools.packages.find] include = ["portfolio*", "fetcher*"]` with `exclude` unset, and having the Dockerfile `COPY fetcher ./fetcher` too (drop `fetcher` from `.dockerignore`). Pick this variant; it keeps one package definition.

- [ ] **Step 2: Build and run locally**

```bash
docker build -t portfolio-dashboard:dev .
docker run --rm -d --name pd -e INGEST_TOKEN=t -p 8000:8000 portfolio-dashboard:dev
sleep 2 && curl -fsS localhost:8000/healthz && curl -fsS localhost:8000/ | grep -c "No data yet"
docker rm -f pd
```
Expected: `{"status":"ok"}` and `1`.

- [ ] **Step 3: `.github/workflows/image.yml`**

```yaml
name: image
on:
  push:
    tags: ['v*']
permissions:
  contents: read
  packages: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=semver,pattern={{version}}
      - uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
```
Add a `test` job before `build` that runs `pip install -e '.[dev,fetcher]' && pytest` (add `needs: test`), so a tag never publishes an image with failing tests.

- [ ] **Step 4: Create the public GitHub repo and push** (outward-facing: confirm with the user first)

```bash
gh repo create umueller74/portfolio-dashboard --public --source . --push
git tag v0.1.0 && git push origin v0.1.0
gh run watch --repo umueller74/portfolio-dashboard
```
Expected: the run succeeds. Then make the ghcr package public: GitHub → profile → Packages → `portfolio-dashboard` → Package settings → Change visibility → Public (no API for this; do it in the UI). Verify: `docker pull ghcr.io/umueller74/portfolio-dashboard:0.1.0` from a machine that is not logged in to ghcr.

---

### Task 12: Kubernetes manifests (k8s-homelab)

**Files (all in `~/Workspace/k8s-homelab`, on branch `197-etf-dashboard`):**
- Create: `apps/base/portfolio/{namespace,pvc,deployment,service,middleware,ingressroute,servicemonitor,prometheusrule,backup,kustomization}.yaml`
- Create: `apps/production/portfolio/{kustomization.yaml,portfolio-secrets.sops.yaml,portfolio-basic-auth.sops.yaml,portfolio-backup-nas.sops.yaml}`
- Create: `clusters/production/portfolio.yaml`

- [ ] **Step 1: Branch and pattern check**

```bash
cd ~/Workspace/k8s-homelab && git checkout -b 197-etf-dashboard
grep -rn "influxdb" clusters/production --include=kustomization.yaml; ls clusters/production | head -30
```
Expected: shows how per-app Flux Kustomizations are picked up (directory listing or an explicit resource list). If a `kustomization.yaml` lists them, add `portfolio.yaml` there in Step 9.

- [ ] **Step 2: Base: namespace, pvc, service**

```yaml
# apps/base/portfolio/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: portfolio
```
```yaml
# apps/base/portfolio/pvc.yaml
# longhorn-retain: an accidental Kustomization delete must not take the data with it.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: portfolio
  namespace: portfolio
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn-retain
  resources:
    requests:
      storage: 1Gi
```
```yaml
# apps/base/portfolio/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: portfolio
  namespace: portfolio
  labels:
    app: portfolio
spec:
  selector:
    app: portfolio
  ports:
    - name: http
      port: 8000
      targetPort: http
```

- [ ] **Step 3: Base: deployment**

```yaml
# apps/base/portfolio/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: portfolio
  namespace: portfolio
spec:
  replicas: 1
  # ReadWriteOnce volume: never run two pods at once.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: portfolio
  template:
    metadata:
      labels:
        app: portfolio
    spec:
      enableServiceLinks: false
      securityContext:
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: portfolio
          image: ghcr.io/umueller74/portfolio-dashboard:0.1.0
          ports:
            - name: http
              containerPort: 8000
          env:
            - name: DB_PATH
              value: /data/portfolio.db
            - name: INGEST_TOKEN
              valueFrom:
                secretKeyRef:
                  name: portfolio-secrets
                  key: ingest-token
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
          securityContext:
            allowPrivilegeEscalation: false
            runAsNonRoot: true
            runAsUser: 1000
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: portfolio
```

- [ ] **Step 4: Base: middleware and ingress**

`/api` carries its own bearer token and must bypass basicAuth, otherwise the fetcher on raspi2 would need the browser credentials too.

```yaml
# apps/base/portfolio/middleware.yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: basic-auth
  namespace: portfolio
spec:
  basicAuth:
    secret: portfolio-basic-auth
```
```yaml
# apps/base/portfolio/ingressroute.yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: portfolio
  namespace: portfolio
  annotations:
    gethomepage.dev/enabled: 'true'
    gethomepage.dev/href: 'https://portfolio.homelab.cs-ol.de'
    gethomepage.dev/name: Portfolio
    gethomepage.dev/description: ETF portfolio from Scalable Capital
    gethomepage.dev/group: Primary Services
    gethomepage.dev/icon: mdi-chart-line
    gethomepage.dev/weight: '40'
spec:
  entryPoints:
    - websecure
  routes:
    # Bearer-token API (ingest, backup): no basicAuth, the app checks the token itself.
    - kind: Rule
      priority: 20
      match: Host(`portfolio.homelab.cs-ol.de`) && PathPrefix(`/api`)
      services:
        - kind: Service
          name: portfolio
          port: 8000
    - kind: Rule
      priority: 10
      match: Host(`portfolio.homelab.cs-ol.de`)
      middlewares:
        - name: basic-auth
      services:
        - kind: Service
          name: portfolio
          port: 8000
  tls: {}
```
Check `apps/base/*/ingressroute.yaml` for the weight values already used under "Primary Services" and adjust `weight` to avoid a clash: `grep -rn "group: Primary Services" -A3 apps/base | grep weight`.

- [ ] **Step 5: Base: ServiceMonitor and alert**

```yaml
# apps/base/portfolio/servicemonitor.yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: portfolio
  namespace: portfolio
  labels:
    release: prometheus
spec:
  selector:
    matchLabels:
      app: portfolio
  endpoints:
    - port: http
      path: /metrics
      interval: 60s
```
```yaml
# apps/base/portfolio/prometheusrule.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: portfolio
  namespace: portfolio
  labels:
    release: prometheus
spec:
  groups:
    - name: portfolio
      rules:
        - alert: PortfolioDown
          expr: |
            up{namespace="portfolio", service="portfolio"} == 0
              or absent(up{namespace="portfolio", service="portfolio"})
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: Portfolio dashboard unreachable
        - alert: PortfolioDataStale
          expr: time() - portfolio_last_ingest_timestamp_seconds{namespace="portfolio"} > 36 * 3600
          for: 15m
          labels:
            severity: warning
          annotations:
            summary: Portfolio data is older than 36 hours
            description: >-
              The fetcher on raspi2 has not delivered data. Check
              `systemctl status portfolio-fetcher` there; if the log says the
              token was rejected, log in again with `hermes mcp login scalable`.
```

- [ ] **Step 6: Base: nightly backup to the NAS**

Same pattern as `apps/base/influxdb/backup.yaml`; the init container pulls a consistent copy from the app's `/api/backup` (no volume access, any node).

```yaml
# apps/base/portfolio/backup.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: portfolio-backup-known-hosts
  namespace: portfolio
data:
  known_hosts: |
    <copy the single line from apps/base/influxdb/backup.yaml, verbatim>
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: portfolio-backup
  namespace: portfolio
spec:
  schedule: '50 3 * * *'
  timeZone: Europe/Berlin
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: OnFailure
          securityContext:
            seccompProfile:
              type: RuntimeDefault
          initContainers:
            - name: dump
              image: curlimages/curl:8.11.1
              command:
                - sh
                - -c
                - |
                  set -eu
                  curl -fsS -H "Authorization: Bearer ${INGEST_TOKEN}" \
                    http://portfolio.portfolio.svc.cluster.local:8000/api/backup -o /backup/portfolio.db
              env:
                - name: INGEST_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: portfolio-secrets
                      key: ingest-token
              resources:
                requests: {cpu: 10m, memory: 32Mi}
                limits: {cpu: 200m, memory: 128Mi}
              securityContext:
                allowPrivilegeEscalation: false
              volumeMounts:
                - name: backup
                  mountPath: /backup
          containers:
            - name: ship
              image: alpine:3.22
              env:
                - name: APP
                  value: portfolio
                - name: KEEP
                  value: '14'
                - name: NAS_HOST
                  value: '192.168.1.240'
                - name: NAS_PATH
                  value: /share/MD0_DATA/Backup/portfolio
                - name: NAS_USER
                  valueFrom:
                    secretKeyRef:
                      name: portfolio-backup-nas
                      key: username
                - name: SSHPASS
                  valueFrom:
                    secretKeyRef:
                      name: portfolio-backup-nas
                      key: password
              command:
                - sh
                - -c
                - |
                  set -eu
                  apk add --no-cache openssh-client sshpass tar >/dev/null
                  STAMP=$(date -u +%Y-%m-%d_%H%MZ)
                  OUT="/tmp/${APP}-${STAMP}.tar.gz"
                  tar -C /backup -czf "$OUT" .
                  SIZE=$(wc -c < "$OUT")
                  [ "$SIZE" -gt 1000 ] || { echo "FATAL: archive implausibly small ($SIZE bytes)" >&2; exit 1; }
                  SSH_OPTS="-o UserKnownHostsFile=/etc/nas/known_hosts -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa"
                  sshpass -e ssh $SSH_OPTS "${NAS_USER}@${NAS_HOST}" "mkdir -p '${NAS_PATH}'"
                  sshpass -e scp -O $SSH_OPTS "$OUT" "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/"
                  sshpass -e ssh $SSH_OPTS "${NAS_USER}@${NAS_HOST}" \
                    "cd '${NAS_PATH}' && ls -1t ${APP}-*.tar.gz | sed -n '$((KEEP + 1)),\$p' | while read -r f; do rm -f \"\$f\"; done"
                  echo "Backed up $(basename "$OUT") ($SIZE bytes) to ${NAS_HOST}:${NAS_PATH}"
              resources:
                requests: {cpu: 10m, memory: 32Mi}
                limits: {cpu: 500m, memory: 256Mi}
              securityContext:
                allowPrivilegeEscalation: false
              volumeMounts:
                - name: backup
                  mountPath: /backup
                  readOnly: true
                - name: known-hosts
                  mountPath: /etc/nas
                  readOnly: true
          volumes:
            - name: backup
              emptyDir: {}
            - name: known-hosts
              configMap:
                name: portfolio-backup-known-hosts
```
Replace the `known_hosts` placeholder line with the real line before committing: `sed -n '/known_hosts: |/,/^---/p' apps/base/influxdb/backup.yaml`. The empty SQLite file is only about 40 KB, so the 1000-byte floor is a sanity check, not a size estimate.

`apps/base/portfolio/kustomization.yaml`:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - pvc.yaml
  - deployment.yaml
  - service.yaml
  - middleware.yaml
  - ingressroute.yaml
  - servicemonitor.yaml
  - prometheusrule.yaml
  - backup.yaml
```

- [ ] **Step 7: SOPS secrets** (run in your own terminal; passwords must not appear in this transcript)

```bash
cd ~/Workspace/k8s-homelab/apps/production/portfolio
INGEST=$(openssl rand -hex 32)
cat > portfolio-secrets.sops.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: portfolio-secrets
  namespace: portfolio
stringData:
  ingest-token: ${INGEST}
EOF
sops --encrypt --in-place portfolio-secrets.sops.yaml

read -rsp "Dashboard password: " PW; echo
HASH=$(htpasswd -nbB udo "$PW")
cat > portfolio-basic-auth.sops.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: portfolio-basic-auth
  namespace: portfolio
stringData:
  users: '${HASH}'
EOF
sops --encrypt --in-place portfolio-basic-auth.sops.yaml

# NAS credentials: reuse the influxdb ones
sops -d ../influxdb/influxdb-backup-nas.sops.yaml | sed 's/namespace: influxdb/namespace: portfolio/; s/name: influxdb-backup-nas/name: portfolio-backup-nas/' > portfolio-backup-nas.sops.yaml
sops --encrypt --in-place portfolio-backup-nas.sops.yaml
echo "$INGEST" > ~/.portfolio-ingest-token && chmod 600 ~/.portfolio-ingest-token   # needed for the Ansible vault in Task 13
```
Verify nothing is in plaintext: `grep -L "ENC\[" *.sops.yaml` must print nothing. If `htpasswd` is missing: `sudo apt install apache2-utils`. If the sed leaves the decrypted `sops:` metadata block in the file, delete the whole `sops:` section before re-encrypting.

- [ ] **Step 8: Production overlay and Flux Kustomization**

```yaml
# apps/production/portfolio/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base/portfolio
  - portfolio-secrets.sops.yaml
  - portfolio-basic-auth.sops.yaml
  - portfolio-backup-nas.sops.yaml
```
```yaml
# clusters/production/portfolio.yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: portfolio
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: GitRepository
    name: flux-system
  path: ./apps/production/portfolio
  prune: true
  wait: true
  decryption:
    provider: sops
    secretRef:
      name: sops-age
  dependsOn:
    - name: infrastructure
```

- [ ] **Step 9: Validate**

```bash
kubectl kustomize apps/base/portfolio > /dev/null && echo base-ok
kubectl kustomize apps/production/portfolio | grep -c "kind: " 
flux diff kustomization portfolio --path apps/production/portfolio 2>&1 | tail -5 || true
git diff --stat
```
Expected: `base-ok`, a non-zero count, no render errors. If `clusters/production/` lists its files explicitly (Step 1), add `portfolio.yaml` there too.

- [ ] **Step 10: Commit and PR**

```bash
git add apps/base/portfolio apps/production/portfolio clusters/production/portfolio.yaml docs/superpowers/plans
git commit -m "#197 portfolio: dashboard deployment, ingress with basicAuth, backup, alerts"
git push -u origin 197-etf-dashboard
gh pr create --title "Closes #197: ETF portfolio dashboard" --body "Deploys umueller74/portfolio-dashboard:0.1.0 (see plan in docs/superpowers/plans). Needs the fetcher role from the homelab repo to receive data."
git checkout main && git pull origin main
```
Do not merge until Task 13's data path is ready, or the page shows "No data yet" (harmless).

---

### Task 13: Fetcher on raspi2 (Ansible role in `~/Workspace/homelab`)

**Files (in `~/Workspace/homelab`, branch `197-portfolio-fetcher`):**
- Create: `roles/portfolio_fetcher/tasks/main.yml`, `roles/portfolio_fetcher/templates/{portfolio-fetcher.env.j2,portfolio-fetcher.service.j2,portfolio-fetcher.timer.j2}`, `roles/portfolio_fetcher/defaults/main.yml`
- Modify: the playbook and the raspi2 host vars that already apply the Hermes role

- [ ] **Step 1: Learn the Hermes role's conventions**

```bash
cd ~/Workspace/homelab && git checkout -b 197-portfolio-fetcher
ls roles; grep -rln "hermes" roles inventory playbooks 2>/dev/null | head
sed -n 1,80p roles/hermes*/tasks/main.yml
```
Mirror its variable naming, become settings, vault variable pattern and how the playbook targets `192.168.0.54`. The inventory file `inventory/hosts.yml` is CRLF; keep CRLF when editing it and check `git diff --stat` shows only intended lines.

- [ ] **Step 2: `defaults/main.yml`**

```yaml
portfolio_fetcher_user: hermes
portfolio_fetcher_dir: /opt/portfolio-fetcher
portfolio_fetcher_state_dir: /var/lib/portfolio-fetcher
portfolio_fetcher_repo: https://github.com/umueller74/portfolio-dashboard.git
portfolio_fetcher_version: v0.1.0
portfolio_fetcher_ingest_url: https://portfolio.homelab.cs-ol.de/api/ingest
# portfolio_fetcher_ingest_token comes from the vault
```

- [ ] **Step 3: `tasks/main.yml`**

```yaml
- name: Install git
  ansible.builtin.apt:
    name: git
    state: present
  become: true

- name: Create directories
  ansible.builtin.file:
    path: "{{ item }}"
    state: directory
    owner: "{{ portfolio_fetcher_user }}"
    mode: '0750'
  loop: ["{{ portfolio_fetcher_dir }}", "{{ portfolio_fetcher_state_dir }}"]
  become: true

- name: Check out the fetcher source
  ansible.builtin.git:
    repo: "{{ portfolio_fetcher_repo }}"
    dest: "{{ portfolio_fetcher_dir }}/src"
    version: "{{ portfolio_fetcher_version }}"
  become: true
  become_user: "{{ portfolio_fetcher_user }}"
  notify: restart portfolio-fetcher timer

# No venv: the fetcher runs inside Hermes' own interpreter (it has `mcp` and Hermes' OAuth code).
# The wrapper picks the newest bundled Python, because the version string changes on Hermes updates.
- name: Install the launcher
  ansible.builtin.copy:
    dest: "{{ portfolio_fetcher_dir }}/run.sh"
    owner: "{{ portfolio_fetcher_user }}"
    mode: '0755'
    content: |
      #!/bin/sh
      set -eu
      PY=$(ls -d /home/hermes/.hermes/tools/python-*/bin/python3 | sort -V | tail -1)
      exec "$PY" -I "{{ portfolio_fetcher_dir }}/src/fetcher/entry.py"
  become: true

- name: Environment file (contains the ingest token)
  ansible.builtin.template:
    src: portfolio-fetcher.env.j2
    dest: /etc/portfolio-fetcher.env
    owner: root
    group: "{{ portfolio_fetcher_user }}"
    mode: '0640'
  become: true

- name: systemd units
  ansible.builtin.template:
    src: "{{ item }}.j2"
    dest: "/etc/systemd/system/{{ item }}"
    mode: '0644'
  loop: [portfolio-fetcher.service, portfolio-fetcher.timer]
  become: true

- name: Enable the timer
  ansible.builtin.systemd:
    name: portfolio-fetcher.timer
    enabled: true
    state: started
    daemon_reload: true
  become: true
```
Add a `handlers/main.yml` with `- name: restart portfolio-fetcher timer` that runs `ansible.builtin.systemd` with `state: restarted`, `name: portfolio-fetcher.timer`, `daemon_reload: true`.

- [ ] **Step 4: Templates**

`portfolio-fetcher.env.j2`:
```
STATE_DIR={{ portfolio_fetcher_state_dir }}
INGEST_URL={{ portfolio_fetcher_ingest_url }}
INGEST_TOKEN={{ portfolio_fetcher_ingest_token }}
```
`portfolio-fetcher.service.j2`:
```ini
[Unit]
Description=Fetch Scalable Capital data for the portfolio dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={{ portfolio_fetcher_user }}
EnvironmentFile=/etc/portfolio-fetcher.env
WorkingDirectory={{ portfolio_fetcher_dir }}/src
ExecStart={{ portfolio_fetcher_dir }}/run.sh
```
`portfolio-fetcher.timer.j2`:
```ini
[Unit]
Description=Run the portfolio fetcher twice a day

[Timer]
OnCalendar=*-*-* 08:00:00 Europe/Berlin
OnCalendar=*-*-* 23:30:00 Europe/Berlin
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 5: Vault the ingest token and wire the role**

```bash
ansible-vault edit <the same vault file the Hermes role uses>   # add: portfolio_fetcher_ingest_token: "<contents of ~/.portfolio-ingest-token>"
```
Add the role to the playbook that targets raspi2, then:
```bash
ansible-playbook <playbook> --limit 192.168.0.54 --check --diff
```
Expected: shows the role's changes, no failures. Check that the diff output does not print the token.

- [ ] **Step 6: Commit and PR in the homelab repo**

```bash
git add roles/portfolio_fetcher <playbook> <vault and vars files>
git commit -m "#197 Add portfolio_fetcher role for raspi2"
git push -u origin 197-portfolio-fetcher
gh pr create --title "Closes umueller74/k8s-homelab#197 (fetcher part): portfolio_fetcher role" --body "Systemd timer on raspi2 that pushes Scalable Capital data to the portfolio dashboard."
git checkout main && git pull origin main
```

---

### Task 14: Rollout and verification

- [ ] **Step 1: Merge the k8s-homelab PR; wait for Flux**

```bash
flux reconcile kustomization portfolio --with-source
kubectl -n portfolio get pods,pvc,ingressroute
```
Expected: pod `Running 1/1`, PVC `Bound`.

- [ ] **Step 2: Check auth boundaries**

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://portfolio.homelab.cs-ol.de/            # 401 (basicAuth)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://portfolio.homelab.cs-ol.de/api/ingest -d '{}'   # 401 (bearer), not 401 from basic auth realm
curl -s -u udo https://portfolio.homelab.cs-ol.de/ | grep -c "No data yet"                # 1
```

- [ ] **Step 3: Merge the homelab PR, apply the role, and run the fetcher once**

```bash
ansible-playbook <playbook> --limit 192.168.0.54
ssh raspi2 'sudo systemctl start portfolio-fetcher.service; journalctl -u portfolio-fetcher -n 30 --no-pager'
```
Expected: `pushed N trades, M closes, K quotes`. If it fails with an auth error, run `hermes mcp reauth scalable` on raspi2 (2FA), then retry. If the failure is in a tool call, fix the parameter name in Task 10 (see its Step 1 note).

- [ ] **Step 4: Verify against the broker (the real acceptance test)**

Open the dashboard. Compare with the Scalable app: total value within a few euros of the app, and the number of trades equal to the broker's count of executed buys and sells. Check one fee-bearing trade if any exists, and check each ETF's quantity matches the broker. Record any mismatch and fix the parser or FIFO logic with a new failing test first.

- [ ] **Step 5: Verify monitoring and backup**

```bash
kubectl -n portfolio create job --from=cronjob/portfolio-backup backup-test && kubectl -n portfolio logs job/backup-test -c ship --follow
kubectl -n monitoring get prometheusrule portfolio
```
Expected: `Backed up portfolio-...tar.gz`. Then `kubectl -n portfolio delete job backup-test`. In Prometheus (or Grafana Explore) confirm `portfolio_last_ingest_timestamp_seconds` is recent.

- [ ] **Step 6: Close out**

```bash
gh issue close 197 --repo umueller74/k8s-homelab --comment "Deployed and verified against the broker."
```
Update the second-brain entry (`topics/kubernetes-homelab/2026-10-11-etf-dashboard-design.md`) from `partial` to `done` with the final decisions, the Task 0 outcome, and any parser surprises, following `~/Workspace/second-brain/AGENTS.md`. Remember: the issue's remaining items are a Scalable re-login whenever 2FA expires, and the fee assumption, which stays unverified until a trade with a fee appears.

---

## Self-Review

**Spec coverage:** data via MCP (Tasks 8, 10); FIFO, sells, XIRR (2, 3, 5); pages and ranges 1W..All plus last-session figure (5, 6); backfill via `year_to_date` and daily extension (10, the closes table); SQLite on retained PVC and NAS backup (12); FastAPI/Jinja/Chart.js (6); ghcr image from a public repo (11); basicAuth with `/api` exempt, Homepage tile (12); fetcher on raspi2 with token handling and Task 0 spike (0, 9, 13); `/metrics` and staleness alert (7, 12); the "last updated" banner (6). The GitHub issue exists (#197).

**Known gaps to resolve during execution:** the real parameter name of `get_transaction_details` (checked in Task 10, Step 1); whether Hermes locks with `flock` (Task 0); the fee formula (assumption in Global Constraints); the exact Ansible conventions (Task 13, Step 1); `limitPrice`-based cancelled rows are skipped, so a partially filled then cancelled order (status other than `SETTLED`) is ignored; verify with the broker count in Task 14, Step 4.

---

## Implementation notes (deviations found while executing, 2026-10-11)

- **Price excludes the fee.** The list's `amount` is the broker total *after* fees, so `|amount| / quantity` double-counts a fee. The fetcher takes the price from `get_transaction_details` (`marketValuation / quantity`, `fetcher.normalize.price_from_details`) and caches `{fee, price}` per transaction in `details.json`. Cost = quantity * price + fee then equals the broker total exactly. Found by a dry run against real data on raspi2.
- **Savings-plan rounding.** The broker rounds `amount` to 4 and quantity to 6 decimals, so derived prices differ from the round execution price in the sixth decimal; this is harmless because quantity * price reproduces the broker total.
- **`mcp` 2.0 API.** `streamable_http_client(url, http_client=...)`, `create_mcp_http_client(auth=...)`, `result.is_error`. `mcp` only becomes importable after `fetcher.auth_hermes.bootstrap()`, which must run before any `mcp` import.
- **Fee strings** are normalized (`0`, not `0.0000`).
- **Test fixtures are synthetic.** Real quantities, prices, dates and ETF names were replaced before publishing, and the app repo history was squashed to one commit, because the repo is public.
- **XIRR test:** the correct value for the multi-contribution case is 20.26 %, not the range first written.
- **FIFO queues are per ISIN (found in Task 14, fixed in 0.1.1).** The first `replay()` kept one queue for all ETFs, so selling ETF A consumed ETF B's oldest lots; the single-ETF unit tests could not see it. The symptom on real data was wrong holdings quantities and an absurd XIRR (-80 % on a +0.8 % portfolio). A two-ETF regression test now covers it, and the check that caught it is the one to repeat after any FIFO change: compare each ETF's remaining quantity with the net of its trades and with the broker's `get_portfolio_holdings` (`position.filled`).
- **Verified against the broker after the fix:** remaining quantities equal the broker's for every ETF, and an ETF's unrealized P/L equals Scalable's own `SINCE_BUY` return from `get_security_quote`.
- **Rollout facts:** `ansible-vault` and `ansible-playbook` need stdout and stderr redirected to files when run from Claude Code (non-blocking handles); the ghcr package inherited public visibility from the public repo, so no UI step was needed.
