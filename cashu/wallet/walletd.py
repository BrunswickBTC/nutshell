from __future__ import annotations

import os
import time, secrets
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field

from .wallet import Wallet
from ..core.base import Unit
from ..core.settings import settings
from ..core.db import Database
from ..core.models import PostMeltQuoteResponse

#from .crud import get_bolt11_melt_quote_row


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- INIT CODE ----
    app.state.db = Database(settings.wallet_name, _db_path())
    await app.state.db.connect()

    await _ensure_melts_table(app.state.db)
    await _ensure_claims_table(app.state.db)

    yield

    # ---- SHUTDOWN CODE ----
    await app.state.db.close()


app = FastAPI(title="nutshell-walletd", version="0.1", lifespan=lifespan)

# --------- request/response models ---------

class BalanceResp(BaseModel):
    wallet: str
    unit: str
    available: int
    balance: int
    default_mint: str
    per_mint: Dict[str, Dict[str, Any]]

class MintQuoteReq(BaseModel):
    amount: int
    unit: str = Field(default="sat")
    mint_url: Optional[str] = None
    memo: Optional[str] = None

class MintQuoteResp(BaseModel):
    mint_url: str
    quote: str
    request: str  # bolt11 invoice
    amount: int
    unit: str

class MintExecuteReq(BaseModel):
    quote: str
    unit: str = Field(default="sat")
    mint_url: Optional[str] = None

class MintExecuteResp(BaseModel):
    mint_url: str
    quote: str
    status: str  # "pending" | "paid" | "failed"
    paid: bool

class MeltQuoteReq(BaseModel):
    invoice: str # bolt11
    unit: str = Field(default="sat") # ex: sat
    mint_url: Optional[str] = None # ex: "https://mint.minibits.cash/Bitcoin"
    payment_hash: str # 64hex

class MeltQuoteResp(BaseModel):
    mint_url: str
    quote: str
    amount: int
    fee_reserve: int
    unit: str

class MeltExecuteReq(BaseModel):
    payment_hash: str # 64hex
    #invoice: str
    #quote: str
    #fee_reserve: int
    #unit: str = Field(default="sat")
    #mint_url: str

class MeltExecuteResp(BaseModel):
    mint_url: str
    quote: str
    status: str  # "paid" | "pending" | "failed"
    paid: bool
    fee_paid_sat: Optional[int] = None
    preimage: Optional[str] = None

class MintStatusReq(BaseModel):
    quote: str
    mint_url: Optional[str] = None
    unit: str = Field(default="sat")

class MintStatusResp(BaseModel):
    paid: bool
    status: str # "paid" | "claimable" | "failed" | "expired" | "canceled" | "pending"
    failed: bool = False

class MeltStatusReq(BaseModel):
    payment_hash: str # 64hex
    #quote: str
    #mint_url: Optional[str] = None
    #unit: str = Field(default="sat")

class MeltStatusResp(BaseModel):
    mint_url: str
    quote: str
    status: str  # "paid" | "pending" | "failed"
    paid: bool
    fee_paid_sat: Optional[int] = None
    preimage: Optional[str] = None
    payment_hash: str
    state: str

# --------- wallet construction helpers ---------

def _db_path() -> str:
    # Match CLI: ~/.cashu/<walletname>
    return os.path.join(settings.cashu_dir, settings.wallet_name)


def _default_mint() -> str:
    # Match CLI default host
    return settings.mint_url

# --------- wallet helpers ---------
async def _wallet_for(mint_url: str, unit: Unit, *, load_all_keysets: bool = True) -> Wallet:
    db_path = _db_path()
    wallet_name = settings.wallet_name

    w = await Wallet.with_db(
        url=mint_url,
        db=db_path,
        name=wallet_name,
        unit=unit.name,
        skip_db_read=False,
        load_all_keysets=load_all_keysets,
    )
    if not getattr(w, "mint_info", None):
        await w.load_mint()
    return w


# -------- CLAIMS DATABASE --------
CLAIMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS minted_claims (
    mint_url TEXT NOT NULL,
    unit TEXT NOT NULL,
    quote TEXT NOT NULL,
    claimed_time INTEGER NOT NULL,
    PRIMARY KEY (mint_url, unit, quote)
)
"""

async def _ensure_claims_table(db: Database):
    await db.execute(CLAIMS_TABLE_SQL)


CLAIMS_UPSERT_SQL = """
INSERT INTO minted_claims (mint_url, unit, quote, claimed_time)
VALUES (:mint_url, :unit, :quote, :claimed_time)
ON CONFLICT(mint_url, unit, quote)
DO UPDATE SET claimed_time = excluded.claimed_time
"""

async def _mark_claimed(db: Database, mint_url: str, unit: str, quote: str, claimed_time: int) -> None:
    #await _ensure_claims_table(db)
    await db.execute(
        CLAIMS_UPSERT_SQL,
        {
            "mint_url": mint_url,
            "unit": unit,
            "quote": quote,
            "claimed_time": claimed_time,
        },
    )


CLAIMS_SELECT_SQL = """
SELECT claimed_time
FROM minted_claims
WHERE mint_url = :mint_url AND unit = :unit AND quote = :quote
LIMIT 1
"""

async def _is_claimed(db: Database, mint_url: str, unit: str, quote: str) -> bool:
    #await _ensure_claims_table(db)
    row = await db.fetchone(
        CLAIMS_SELECT_SQL,
        {"mint_url": mint_url, "unit": unit, "quote": quote},
    )
    return row is not None


# ------ MELT DATABASE ------

EXECUTION_STALE_SECS = 120  # tune
TTL_SUCCESS_SECS = 30 * 24 * 3600
TTL_FAIL_SECS = 7 * 24 * 3600

def _status_from_state(state: str) -> tuple[str, bool]: # returns tuple (status, paid)
    s = (state or "").upper()
    if s == "SUCCEEDED":
        return "paid", True
    if s in ("FAILED", "CANCELED"):
        return "failed", False
    if s == "EXECUTING":
        return "pending", False
    if s == "PENDING":
        return "pending", False
    return (state or "pending").lower(), False



MELTS_TABLE_SQL_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS melt_map (
      payment_hash TEXT PRIMARY KEY,
      mint_url TEXT NOT NULL,
      unit TEXT NOT NULL,
      quote TEXT NOT NULL,
      bolt11 TEXT NOT NULL,
      amount INTEGER NOT NULL,
      fee_reserve INTEGER NOT NULL,

      state TEXT NOT NULL DEFAULT 'PENDING', -- PENDING|EXECUTING|SUCCEEDED|FAILED|CANCELED
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      completed_at INTEGER,
      gc_after INTEGER,

      executing_lock_id TEXT,
      executing_started_at INTEGER,

      preimage TEXT,
      fee_paid_sat INTEGER,
      failure_code TEXT,
      failure_detail TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS melt_map_quote_idx ON melt_map(quote);",
    "CREATE INDEX IF NOT EXISTS melt_map_state_idx ON melt_map(state);",
    "CREATE INDEX IF NOT EXISTS melt_map_gc_after_idx ON melt_map(gc_after);",
]

class MeltMapResp(BaseModel):
    mint_url: str
    unit: str
    quote: str
    bolt11: str
    amount: int
    fee_reserve: int

    state: str
    #created_at: int
    #updated_at: int
    #completed_at: int
    #gc_after: int

    #executing_lock_id: str
    executing_started_at: Optional[int] = None

    preimage: Optional[str] = None
    fee_paid_sat: Optional[int] = None
    #failure_code: str
    #failure_detail: str

async def _ensure_melts_table(db: Database):
    for stmt in MELTS_TABLE_SQL_STMTS:
        await db.execute(stmt)

async def _store_melt_map(
    db: Database,
    payment_hash: str,
    mint_url: str,
    unit: str,
    quote: str,
    bolt11: str,
    amount: int,
    fee_reserve: int,
) -> None:
    #await _ensure_melts_table(db)
    now = int(time.time())
    await db.execute(
        """
        INSERT INTO melt_map(
            payment_hash, mint_url, unit, quote, bolt11,
            amount, fee_reserve,
            state, created_at, updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(payment_hash) DO UPDATE SET
            mint_url=excluded.mint_url,
            unit=excluded.unit,
            quote=excluded.quote,
            bolt11=excluded.bolt11,
            amount=excluded.amount,
            fee_reserve=excluded.fee_reserve,
            updated_at=excluded.updated_at
        """,
        (payment_hash, mint_url, unit, quote, bolt11,
         int(amount), int(fee_reserve),
         "PENDING", now, now),
    )

async def _get_melt_map_by_hash(db: Database, payment_hash: str) -> Optional[MeltMapResp]:
    def _rget(row, key, default=None):
        try:
            return row[key]
        except Exception:
            return getattr(row, key, default)

    #await _ensure_melts_table(db)
    row = await db.fetchone(
        "SELECT * FROM melt_map WHERE payment_hash=?",
        (payment_hash,)
    )
    if not row: return None
    fee_paid = _rget(row, "fee_paid_sat", None)
    return MeltMapResp(
        mint_url=_rget(row,    "mint_url"),
        unit=_rget(row,        "unit"),
        quote=_rget(row,       "quote"),
        bolt11=_rget(row,      "bolt11"), # a.k.a. invoice
        amount=int(_rget(row,      "amount", 0) or 0),
        fee_reserve=int(_rget(row, "fee_reserve", 0) or 0),
        state=str(_rget(row,       "state", "PENDING") or "PENDING").upper(),
        executing_started_at=int(_rget(row, "executing_started_at", 0) or 0),
        preimage=_rget(row,    "preimage", None),
        fee_paid_sat=None if fee_paid is None else int(fee_paid),
    )

# Assumptions:
# - _get_melt_map_by_hash(payment_hash) uses Database directly (no Wallet) and returns a record with:
#   mint_url, unit, quote, bolt11, amount, fee_reserve, state, preimage, fee_paid_sat (optional)
# - _wallet_for(mint_url, unit, load_all_keysets=False) returns a Wallet with methods:
#   load_mint(), load_proofs(reload=True), select_to_send(...), melt(...)
# - You have Database available as in your snippet: Database(wallet_name, db_path)
# - You have a way to update melts by payment_hash in your DB layer (implemented below as helper calls)

async def _melt_try_lock(db: Database, payment_hash: str) -> bool:
    def _rget(row, key, default=None):
        try:
            return row[key]
        except Exception:
            return getattr(row, key, default)

    """
    Atomically transition PENDING -> EXECUTING.
    Returns True if this call acquired execution, False otherwise.
    """
    now = int(time.time())
    lock_id = secrets.token_hex(16)

    # execute() return semantics vary by Nutshell version; adjust if needed.
    # Many implementations return cursor or rowcount; we handle both.
    res = await db.execute(
        """
        UPDATE melt_map
        SET state = 'EXECUTING',
            executing_lock_id = ?,
            executing_started_at = ?,
            updated_at = ?
        WHERE payment_hash = ?
          AND state = 'PENDING'
        """,
        (lock_id, now, now, payment_hash),
    )
    # Normalize "rows affected"
    rowcount = getattr(res, "rowcount", None)
    if rowcount is None and isinstance(res, int):
        rowcount = res
    if rowcount is None:
        # Fallback: query state to infer
        row = await db.fetchone(
            "SELECT state FROM melt_map WHERE payment_hash = ?",
            (payment_hash,),
        )
        #return bool(row and (row["state"] == "EXECUTING"))
        st = row["state"] if isinstance(row, dict) else getattr(row, "state", None)
        return bool(st == "EXECUTING")
    return rowcount > 0


async def _melt_set_succeeded(db: Database, payment_hash: str, preimage: Optional[str], fee_paid_sat: Optional[int] = None) -> None:
    now = int(time.time())
    gc_after = now + TTL_SUCCESS_SECS

    await db.execute(
        """
        UPDATE melt_map
        SET state = 'SUCCEEDED',
            preimage = ?,
            fee_paid_sat = COALESCE(?, fee_paid_sat),
            completed_at = ?,
            gc_after = ?,
            updated_at = ?,
            executing_lock_id = NULL,
            executing_started_at = NULL
        WHERE payment_hash = ?
        """,
        (preimage, fee_paid_sat, now, gc_after, now, payment_hash),
    )


async def _melt_set_failed(db: Database, payment_hash: str, code: str, detail: str) -> None:
    now = int(time.time())
    gc_after = now + TTL_FAIL_SECS

    await db.execute(
        """
        UPDATE melt_map
        SET state = 'FAILED',
            failure_code = ?,
            failure_detail = ?,
            completed_at = ?,
            gc_after = ?,
            updated_at = ?,
            executing_lock_id = NULL,
            executing_started_at = NULL
        WHERE payment_hash = ?
        """,
        (code, detail[:2048], now, gc_after, now, payment_hash),
    )


def _normalize_melt_result(
    melt_map: MeltMapResp,
    melt_quote: PostMeltQuoteResponse,
) -> Dict[str, Any]:
#) -> Dict[str, Optional[int]]:
    """
    Returns normalized values:
        {
            "amount_sat": int,
            "fee_paid_sat": Optional[int],     # routing fee only
            "total_spent_sat": Optional[int],  # amount + fee - change
            "preimage": Optional[str],
            "state": str,                      # PAID|PENDING|UNPAID|UNKNOWN
        }
    """

    # ---- authoritative amount ----
    amount = int(getattr(melt_map, "amount", 0) or 0)

    # ---- normalize state ----
    state_u = ((getattr(melt_quote, "state", None) or "")
               .strip()
               .upper())

    # ---- preimage ----
    preimage = getattr(melt_quote, "payment_preimage", None)

    # ---- fee normalization ----
    raw_fee_paid = getattr(melt_quote, "fee_paid", None)

    fee_only = None
    total_spent = None

    if raw_fee_paid is not None:
        raw_fee_paid = int(raw_fee_paid)

        # Case A: upstream stored total consumed (>= amount)
        if raw_fee_paid >= amount:
            total_spent = raw_fee_paid
            fee_only = raw_fee_paid - amount

        # Case B: upstream stored fee-only (< amount)
        else:
            fee_only = raw_fee_paid
            total_spent = amount + raw_fee_paid

    return {
        "amount_sat": amount,
        "fee_paid_sat": fee_only,
        "total_spent_sat": total_spent,
        "preimage": preimage,
        "state": state_u or "UNKNOWN",
    }
# --------- endpoints ---------


@app.get("/v1/balance", response_model=BalanceResp)
async def balance_uds(unit: Optional[str] = None):
    u = Unit[unit or settings.wallet_unit]
    w = await _wallet_for(_default_mint(), u)
    await w.load_proofs(reload=True, all_keysets=True)
    per_mint = await w.balance_per_minturl(unit=u)  # dict keyed by minturl
    total_avail = sum(int(v.get("available", 0)) for v in (per_mint or {}).values())

    default_mint = _default_mint()
    return BalanceResp(
        wallet=settings.wallet_name,
        unit=u.name,
        available=total_avail,
        balance=total_avail,
        default_mint=default_mint,
        per_mint=per_mint,
    )

@app.post("/v1/mint/quote", response_model=MintQuoteResp)
async def mint_quote_uds(req: MintQuoteReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)
    await w.load_mint()
    q = await w.request_mint(req.amount, memo=req.memo)  # stores quote to DB
    return MintQuoteResp(
        mint_url=mint_url, quote=q.quote, request=q.request, amount=req.amount, unit=u.name
    )


@app.post("/v1/mint/execute", response_model=MintExecuteResp)
async def mint_execute_uds(req: MintExecuteReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u, load_all_keysets=False)

    async def _refresh_keysets(wallet: Wallet):
        # Always reload mint info + keysets immediately before minting outputs
        await wallet.load_mint()
        for name in ("load_keysets", "load_keys", "load_mint_keys", "load_mint_keysets"):
            fn = getattr(wallet, name, None)
            if not fn:
                continue
            try:
                await fn(reload=True)
            except TypeError:
                await fn()
            return

    async def _load_proofs_all(wallet: Wallet):
        # Handle signature drift across versions
        try:
            await wallet.load_proofs(reload=True, all_keysets=True)
        except TypeError:
            try:
                await wallet.load_proofs(reload=True)
            except TypeError:
                await wallet.load_proofs()

    async def _get_quote(wallet: Wallet, quote_id: str, unit: Unit):
        # Nutshell version drift: some require (quote, unit), some (quote)
        for args in ((quote_id, unit), (quote_id,)):
            try:
                return await wallet.get_mint_quote(*args)
            except TypeError:
                continue
        # re-raise the last TypeError (or whatever happened)
        return await wallet.get_mint_quote(quote_id)

    def _quote_keyset_id(qobj) -> str | None:
        return (
            getattr(qobj, "keyset", None)
            or getattr(qobj, "keyset_id", None)
            or getattr(qobj, "keysetid", None)
        )

    async def _bind_keyset(wallet: Wallet, qobj):
        ks = _quote_keyset_id(qobj)
        if not ks:
            return
        # Best-effort across versions
        if hasattr(wallet, "keyset_id"):
            wallet.keyset_id = ks
        set_keyset = getattr(wallet, "set_keyset", None)
        if set_keyset:
            try:
                await set_keyset(ks)
            except TypeError:
                set_keyset(ks)

    async def _mint_paid_quote(wallet: Wallet, amount: int, quote_id: str):
        # Version drift: some use quote_id=, some quote=
        for kwargs in ({"quote_id": quote_id}, {"quote": quote_id}):
            try:
                return await wallet.mint(amount, **kwargs)
            except TypeError:
                continue
        return await wallet.mint(amount, quote_id=quote_id)

    # Fresh view
    await _refresh_keysets(w)
    await _load_proofs_all(w)

    q = await _get_quote(w, req.quote, u)

    state = str(getattr(q, "state", "") or "").lower()
    paid = bool(getattr(q, "paid", False)) or (state == "paid")

    if not paid:
        return MintExecuteResp(
            mint_url=mint_url,
            quote=req.quote,
            status=(state or "pending"),
            paid=False,
        )

    amount = int(getattr(q, "amount", 0) or 0)
    if amount <= 0:
        # fallback fields some versions use
        amount = int(getattr(q, "quote_amount", 0) or 0)

    # IMPORTANT: bind wallet to quote’s keyset (if mint provides it)
    await _bind_keyset(w, q)

    quote_id = str(getattr(q, "quote", None) or getattr(q, "id", None) or req.quote)

    try:
        # Mint proofs for the paid quote
        await _refresh_keysets(w)
        await _bind_keyset(w, q)
        await _mint_paid_quote(w, amount, quote_id)

    except Exception as e:
        msg = str(e).lower()
        if ("keyset id unknown" not in msg) and ("11000" not in msg):
            raise

        # One forced refresh + re-fetch quote + re-bind keyset + retry
        await _refresh_keysets(w)
        q2 = await _get_quote(w, req.quote, u)
        await _bind_keyset(w, q2)
        quote_id2 = str(getattr(q2, "quote", None) or getattr(q2, "id", None) or req.quote)
        amount2 = int(getattr(q2, "amount", amount) or amount)
        await _mint_paid_quote(w, amount2, quote_id2)

    await _load_proofs_all(w)
    db = app.state.db
    await _mark_claimed(db, mint_url, req.unit, req.quote, int(time.time()))

    return MintExecuteResp(
        mint_url=mint_url,
        quote=req.quote,
        status="paid",
        paid=True,
    )


@app.post("/v1/mint/status", response_model=MintStatusResp)
async def mint_status_uds(req: MintStatusReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)
    await w.load_mint()

    q = await w.get_mint_quote(req.quote)

    db = app.state.db
    claimed = await _is_claimed(db, mint_url, req.unit, req.quote)

    state = str(getattr(q, "state", "") or "")
    state_l = state.lower()

    # If we've already minted proofs locally, it's settled from walletd's perspective.
    if claimed:
        return MintStatusResp(paid=True, status="paid", failed=False)

    # Mint says invoice is paid, but we have not claimed proofs yet.
    if state_l == "paid" or bool(getattr(q, "paid", False)):
        return MintStatusResp(paid=False, status="claimable", failed=False)

    failed = state_l in {"failed", "expired", "canceled"}
    return MintStatusResp(paid=False, status=(state_l or "pending"), failed=failed)


@app.post("/v1/melt/quote", response_model=MeltQuoteResp)
async def melt_quote_uds(req: MeltQuoteReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)

    await w.load_mint()
    mq = await w.melt_quote(req.invoice)

    db = app.state.db
    await _store_melt_map(db, payment_hash=req.payment_hash, mint_url=mint_url, unit=u.name, amount=mq.amount, fee_reserve=mq.fee_reserve, quote=mq.quote, bolt11=req.invoice)

    return MeltQuoteResp(
        mint_url=mint_url,
        quote=mq.quote,
        amount=mq.amount,
        fee_reserve=mq.fee_reserve,
        unit=u.name,
    )


# ---- The rewritten endpoint ----

@app.post("/v1/melt/execute", response_model=MeltExecuteResp)
async def melt_execute_uds(req: MeltExecuteReq):

    def _resp_from_melt(melt_map: MeltMapResp) -> "MeltExecuteResp":
        status, paid = _status_from_state(melt_map.state)

        return MeltExecuteResp(
            mint_url=melt_map.mint_url,
            quote=melt_map.quote,
            status=status,
            paid=paid,
            fee_paid_sat=melt_map.fee_paid_sat,
            preimage=melt_map.preimage
        )
    db = app.state.db

    # 1) Load lifecycle record by payment_hash (DB-only; no Wallet needed)
    melt_map = await _get_melt_map_by_hash(db, req.payment_hash)
    if not melt_map:
        raise HTTPException(status_code=404, detail="Unknown payment_hash")

    # 2) Idempotency: if terminal, return immediately
    state = melt_map.state
    if state in ("SUCCEEDED", "FAILED", "CANCELED"):
        return _resp_from_melt(melt_map)

    mint_url = melt_map.mint_url
    u = Unit[melt_map.unit]
    quote = melt_map.quote
    invoice = melt_map.bolt11

    # 3) Acquire execution lock (prevents double spend / double reserve)
    acquired = await _melt_try_lock(db, req.payment_hash)
    if not acquired:
        # Someone else is executing, or it already became terminal; re-read and return current view
        melt_map = await _get_melt_map_by_hash(db, req.payment_hash)
        if not melt_map:
            raise HTTPException(status_code=404, detail="Unknown payment_hash")
        return _resp_from_melt(melt_map)

    # 5) Use server-authoritative terms from melt_map (do NOT call get_melt_quote)
    amount = melt_map.amount
    fee_reserve = melt_map.fee_reserve
    if amount <= 0:
        await _melt_set_failed(db, req.payment_hash, "MISSING_AMOUNT", "melt record missing amount")
        melt_map = await _get_melt_map_by_hash(db, req.payment_hash)
        return _resp_from_melt(melt_map)

    total = amount + fee_reserve

    # Optional: ignore client-provided fee reserve; if you want strictness, reject mismatch instead.
    # if hasattr(req, "fee_reserve") and req.fee_reserve is not None and int(req.fee_reserve) != fee_reserve:
    #     await _melt_set_failed(db, req.payment_hash, "FEE_MISMATCH", "client fee_reserve mismatch")
    #     melt_map = await _get_melt_map_by_hash(db, req.payment_hash)
    #     return _resp_from_melt(melt_map)

    # 5) Execute: reserve proofs, melt, then terminalize lifecycle record
    w = await _wallet_for(mint_url, u, load_all_keysets=False)

    await w.load_mint()
    await w.load_proofs(reload=True)  # needed because we are selecting/spending proofs now

    send_proofs, _fees = await w.select_to_send(w.proofs, total, set_reserved=True)

    try:
        resp = await w.melt(send_proofs, invoice, fee_reserve, quote)
    except Exception as e:
        # Best-effort unreserve: only if your Wallet exposes it. If not, you need a separate cleanup path.
        # Example (if exists): await w.unreserve_proofs(send_proofs)
        await _melt_set_failed(db, req.payment_hash, "MELT_EXCEPTION", repr(e))
        melt_map = await _get_melt_map_by_hash(db, req.payment_hash)
        return _resp_from_melt(melt_map)

    normalized = _normalize_melt_result(melt_map, resp)

    # 6) Determine success from response; persist terminal truth keyed by payment_hash
    state_u = ((resp.state or "").strip().upper())
    if "PAID" == state_u:
        # terminal success
        await _melt_set_succeeded(db, req.payment_hash, normalized["preimage"], fee_paid_sat=normalized["fee_paid_sat"])
    elif "UNPAID" == state_u:
        # terminal failure
        await _melt_set_failed(db, req.payment_hash, "MELT_NOT_PAID", f"state={state_u}")
    elif "PENDING" == state_u:
        await db.execute(
            "UPDATE melt_map SET updated_at=? WHERE payment_hash=?",
            (int(time.time()), req.payment_hash),
        )

        # leave EXECUTING (do not fail)
        pass
    else:
        # unknown -> conservative failure
        await _melt_set_failed(db, req.payment_hash, "UNKNOWN_QUOTE_STATE", f"state={state_u}")

    # 7) Return from lifecycle record (idempotent truth)
    melt_map = await _get_melt_map_by_hash(db, req.payment_hash)
    if not melt_map:
        raise HTTPException(status_code=404, detail="Unknown payment_hash")
    return _resp_from_melt(melt_map)



@app.get("/v1/melt/status/{payment_hash}", response_model=MeltStatusResp)
async def melt_status_uds(payment_hash: str):
    db = app.state.db

    melt_map = await _get_melt_map_by_hash(db, payment_hash)
    if not melt_map:
        raise HTTPException(status_code=404, detail="Unknown payment_hash")

    state = (melt_map.state or "PENDING").upper()

    # 1) Terminal: return immediately
    if state in ("SUCCEEDED", "FAILED", "CANCELED"):
        status, paid = _status_from_state(state)
        return MeltStatusResp(
            payment_hash=payment_hash,
            mint_url=melt_map.mint_url,
            quote=melt_map.quote,
            status=status,
            paid=paid,
            fee_paid_sat=melt_map.fee_paid_sat,
            preimage=melt_map.preimage,
            state=state.lower(),
        )

    # 2) Non-terminal but not executing: return as-is (do not auto-execute)
    if state != "EXECUTING":
        status, paid = _status_from_state(state)
        return MeltStatusResp(
            payment_hash=payment_hash,
            mint_url=melt_map.mint_url,
            quote=melt_map.quote,
            status=status,
            paid=paid,
            fee_paid_sat=None,
            preimage=None,
            state=state.lower(),
        )

    # 3) EXECUTING: if not stale, return pending
    now = int(time.time())
    started = int(melt_map.executing_started_at or 0)
    if started and (now - started) < EXECUTION_STALE_SECS:
        return MeltStatusResp(
            payment_hash=payment_hash,
            mint_url=melt_map.mint_url,
            quote=melt_map.quote,
            status="pending",
            paid=False,
            fee_paid_sat=None,
            preimage=None,
            state="executing",
        )

    # 4) Stale EXECUTING: attempt bounded reconciliation (READ-ONLY)
    try:
        u = Unit[melt_map.unit]
        w = await _wallet_for(melt_map.mint_url, u, load_all_keysets=False)
        await w.load_mint()

        get_melt = getattr(w, "get_melt_quote", None)
        if not get_melt:
            # Can't reconcile: return pending
            return MeltStatusResp(
                payment_hash=payment_hash,
                mint_url=melt_map.mint_url,
                quote=melt_map.quote,
                status="pending",
                paid=False,
                fee_paid_sat=None,
                preimage=None,
                state="executing",
            )

        mq = await get_melt(melt_map.quote)

        # Normalize mq into PostMeltQuoteResponse shape if it's a MeltQuote
        if isinstance(mq, PostMeltQuoteResponse):
            quote_resp = mq
        else:
            # Most likely mq is MeltQuote; convert using the classmethod you showed
            quote_resp = PostMeltQuoteResponse.from_melt_quote(mq)

        norm = _normalize_melt_result(melt_map, quote_resp)
        qs = norm["state"]  # "PAID" | "PENDING" | "UNPAID" | "UNKNOWN"

    except Exception:
        # Any failure to reconcile: stay executing
        return MeltStatusResp(
            payment_hash=payment_hash,
            mint_url=melt_map.mint_url,
            quote=melt_map.quote,
            status="pending",
            paid=False,
            fee_paid_sat=None,
            preimage=None,
            state="executing",
        )

    # 5) Terminalize only when proven
    if qs == "PAID":
        await _melt_set_succeeded(
            db,
            payment_hash,
            norm["preimage"],
            fee_paid_sat=norm["fee_paid_sat"],
        )
    elif qs == "UNPAID":
        await _melt_set_failed(
            db,
            payment_hash,
            "RECONCILED_UNPAID",
            "wallet quote state=UNPAID",
        )
    else:
        # PENDING/UNKNOWN: keep EXECUTING
        return MeltStatusResp(
            payment_hash=payment_hash,
            mint_url=melt_map.mint_url,
            quote=melt_map.quote,
            status="pending",
            paid=False,
            fee_paid_sat=None,
            preimage=None,
            state="executing",
        )

    # 6) Return terminalized record
    melt_map2 = await _get_melt_map_by_hash(db, payment_hash)
    state2 = (melt_map2.state or "PENDING").upper()
    status, paid = _status_from_state(state2)
    return MeltStatusResp(
        payment_hash=payment_hash,
        mint_url=melt_map2.mint_url,
        quote=melt_map2.quote,
        status=status,
        paid=paid,
        fee_paid_sat=melt_map2.fee_paid_sat,
        preimage=melt_map2.preimage,
        state=state2.lower(),
    )
