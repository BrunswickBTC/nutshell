from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .wallet import Wallet
from ..core.base import Unit
from ..core.settings import settings

from .crud import get_bolt11_melt_quote_row

app = FastAPI(title="nutshell-walletd", version="0.1")


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
    invoice: str
    unit: str = Field(default="sat")
    mint_url: Optional[str] = None

class MeltQuoteResp(BaseModel):
    mint_url: str
    quote: str
    amount: int
    fee_reserve: int
    unit: str

class MeltExecuteReq(BaseModel):
    invoice: str
    quote: str
    fee_reserve: int
    unit: str = Field(default="sat")
    mint_url: str

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
    quote: str
    mint_url: Optional[str] = None
    unit: str = Field(default="sat")

class MeltStatusResp(BaseModel):
    paid: bool
    status: str
    failed: bool = False
    fee_paid_sat: Optional[int] = None
    preimage: Optional[str] = None

# --------- wallet construction helpers ---------

def _db_path() -> str:
    # Match CLI: ~/.cashu/<walletname>
    return os.path.join(settings.cashu_dir, settings.wallet_name)

def _default_mint() -> str:
    # Match CLI default host
    return settings.mint_url

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

CLAIMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS minted_claims (
    mint_url TEXT NOT NULL,
    unit TEXT NOT NULL,
    quote TEXT NOT NULL,
    claimed_time INTEGER NOT NULL,
    PRIMARY KEY (mint_url, unit, quote)
)
"""

async def _ensure_claims_table(db):
    # best-effort idempotent
    try:
        await db.execute(CLAIMS_TABLE_SQL)
    except Exception:
        pass

CLAIMS_UPSERT_SQL = """
INSERT INTO minted_claims (mint_url, unit, quote, claimed_time)
VALUES (:mint_url, :unit, :quote, :claimed_time)
ON CONFLICT(mint_url, unit, quote)
DO UPDATE SET claimed_time = excluded.claimed_time
"""

async def _mark_claimed(db, mint_url: str, unit: str, quote: str, claimed_time: int) -> None:
    await _ensure_claims_table(db)
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

async def _is_claimed(db, mint_url: str, unit: str, quote: str) -> bool:
    await _ensure_claims_table(db)
    row = await db.fetchone(
        CLAIMS_SELECT_SQL,
        {"mint_url": mint_url, "unit": unit, "quote": quote},
    )
    return row is not None


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
    await _mark_claimed(w.db, mint_url, req.unit, req.quote, int(time.time()))

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

    claimed = await _is_claimed(w.db, mint_url, req.unit, req.quote)

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
    return MeltQuoteResp(
        mint_url=mint_url,
        quote=mq.quote,
        amount=mq.amount,
        fee_reserve=mq.fee_reserve,
        unit=u.name,
    )


@app.post("/v1/melt/execute", response_model=MeltExecuteResp)
async def melt_execute_uds(req: MeltExecuteReq):
    u = Unit[req.unit]
    w = await _wallet_for(req.mint_url, u, load_all_keysets=False)
    await w.load_mint()
    await w.load_proofs(reload=True)

    get_melt = getattr(w, "get_melt_quote", None)
    if not get_melt:
        raise HTTPException(status_code=501, detail="Wallet has no get_melt_quote(); needed for amount lookup")
    mq = await get_melt(req.quote)

    amount = int(getattr(mq, "amount", 0))
    total = amount + int(req.fee_reserve)

    send_proofs, _fees = await w.select_to_send(w.proofs, total, set_reserved=True)
    try:
        resp = await w.melt(send_proofs, req.invoice, req.fee_reserve, req.quote)
    finally:
        # if resp failed/raised, make best-effort to unreserve
        # (only if your wallet exposes such a call; otherwise rely on w.melt exception handler)
        pass

    row = await get_bolt11_melt_quote_row(w.db, req.quote)

    fee_paid_sat = None
    preimage = resp.payment_preimage

    if row and row.get("fee_paid") is not None:
        fee_paid_total_sat = int(row["fee_paid"])
        amount_sat = int(row.get("amount") or amount)
        fee_paid_sat = max(0, fee_paid_total_sat - amount_sat)
    
        # prefer DB preimage if present (should match resp)
        if row.get("payment_preimage"):
            preimage = row["payment_preimage"]

    state = (resp.state or "").lower()
    paid = state == "paid"
    status = state or ("paid" if paid else "pending")

    return MeltExecuteResp(
        mint_url=req.mint_url,
        quote=req.quote,
        status=status,
        paid=paid,
        fee_paid_sat=fee_paid_sat,
        preimage=preimage,
    )


@app.post("/v1/melt/status", response_model=MeltStatusResp)
async def melt_status_uds(req: MeltStatusReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)
    await w.load_mint()

    # Prefer a "get_melt_quote" style method if it exists.
    get_melt = getattr(w, "get_melt_quote", None)
    if not get_melt:
        raise HTTPException(status_code=501, detail="Wallet has no get_melt_quote(); implement melt status lookup")

    mq = await get_melt(req.quote)

    row = await get_bolt11_melt_quote_row(w.db, req.quote)

    fee_paid_sat = None
    preimage = mq.payment_preimage

    if row and row.get("fee_paid") is not None:
        fee_paid_total_sat = int(row["fee_paid"])
        amount_sat = int(row.get("amount") or getattr(mq, "amount", 0) or 0)
        fee_paid_sat = max(0, fee_paid_total_sat - amount_sat)
        if row.get("payment_preimage"):
            preimage = row["payment_preimage"]

    state = (mq.state or "").lower()
    paid = state == "paid"
    status = state or ("paid" if paid else "pending")

    return MeltStatusResp(
        paid=paid,
        status=status,
        failed=status in {"failed","expired","canceled","unpaid"},
        fee_paid_sat=fee_paid_sat,
        preimage=preimage,
    )

