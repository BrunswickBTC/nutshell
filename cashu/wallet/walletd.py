from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
    unit: str = "sat"
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
    unit: str = "sat"
    mint_url: Optional[str] = None

class MintExecuteResp(BaseModel):
    mint_url: str
    quote: str
    status: str  # "pending" | "paid" | "failed"
    paid: bool

class MeltQuoteReq(BaseModel):
    invoice: str
    unit: str = "sat"
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
    unit: str = "sat"
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
    unit: str = "sat"
    mint_url: Optional[str] = None

class MintStatusResp(BaseModel):
    paid: bool
    status: str
    failed: bool = False

class MeltStatusReq(BaseModel):
    quote: str
    unit: str = "sat"
    mint_url: Optional[str] = None

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

async def _wallet_for(mint_url: str, unit: Unit) -> Wallet:
    # Match CLI init: run migrations first, then load wallet normally. :contentReference[oaicite:2]{index=2}
    db_path = _db_path()
    wallet_name = settings.wallet_name
    await Wallet.with_db(
        url=mint_url,
        db=db_path,
        name=wallet_name,
        unit=unit.name,
        skip_db_read=True,
    )
    w = await Wallet.with_db(
        url=mint_url,
        db=db_path,
        name=wallet_name,
        unit=unit.name,
        skip_db_read=False,
        load_all_keysets=True,
    )
    if not w.mint_info:
        await w.load_mint()
    return w

async def _refresh_mint_state(w):
    await w.load_mint()

    # try the likely refresh methods across versions
    for name in ("load_keysets", "load_keys", "load_mint_keys", "load_mint_keysets"):
        fn = getattr(w, name, None)
        if fn:
            try:
                # some variants accept reload=True
                await fn(reload=True)
            except TypeError:
                await fn()

# walletd_claims: quote claimed locally (proofs minted + stored)
CLAIMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS walletd_claims (
  mint_url TEXT NOT NULL,
  unit     TEXT NOT NULL,
  quote    TEXT NOT NULL,
  claimed_time INTEGER NOT NULL,
  PRIMARY KEY (mint_url, unit, quote)
);
"""

async def _ensure_claims_table(db):
    # best-effort idempotent
    try:
        await db.execute(CLAIMS_TABLE_SQL)
    except Exception:
        pass

async def _mark_claimed(db, mint_url: str, unit: str, quote: str, ts: int):
    await _ensure_claims_table(db)
    await db.execute(
        "INSERT OR REPLACE INTO walletd_claims (mint_url, unit, quote, claimed_time) VALUES (?, ?, ?, ?)",
        (mint_url, unit, quote, ts),
    )

async def _is_claimed(db, mint_url: str, unit: str, quote: str) -> bool:
    await _ensure_claims_table(db)
    row = await db.fetchone(
        "SELECT 1 FROM walletd_claims WHERE mint_url = ? AND unit = ? AND quote = ? LIMIT 1",
        (mint_url, unit, quote),
    )
    return bool(row)

# --------- endpoints ---------

@app.get("/v1/balance", response_model=BalanceResp)
async def balance(unit: Optional[str] = None):
    u = Unit[unit or settings.wallet_unit]
    w = await _wallet_for(_default_mint(), u)
    await w.load_proofs(reload=True, all_keysets=True)
    per_mint = await w.balance_per_minturl(unit=u)  # dict keyed by minturl :contentReference[oaicite:6]{index=6}
    total_avail = sum(int(v["available"]) for v in per_mint.values()) if per_mint else 0

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
async def mint_quote(req: MintQuoteReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)
    await w.load_mint()
    q = await w.request_mint(req.amount, memo=req.memo)  # stores quote to DB :contentReference[oaicite:7]{index=7}
    return MintQuoteResp(
        mint_url=mint_url, quote=q.quote, request=q.request, amount=req.amount, unit=u.name
    )

@app.post("/v1/mint/execute", response_model=MintExecuteResp)
async def mint_execute(req: MintExecuteReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)

    # Always start from fresh local view
    await _refresh_mint_state(w)
    await w.load_proofs(reload=True)

    # Confirm paid
    q = await w.get_mint_quote(req.quote)
    if not getattr(q, "paid", False):
        return MintExecuteResp(
            mint_url=mint_url,
            quote=req.quote,
            status="pending",
            paid=False,
        )

    async def _do_mint():
        # Make sure mint/keysets are current immediately before minting outputs
        await _refresh_mint_state(w)
        await w.mint(init(q.amount), quote_id=str(q.quote))

    try:
        await _do_mint()
    except Exception as e:
        msg = str(e)

        # Handle the exact class/message variants you've observed
        is_keyset_unknown = ("keyset id unknown" in msg.lower()) or ("11000" in msg)

        if not is_keyset_unknown:
            raise

        # One forced refresh + single retry
        await _refresh_mint_state(w)
        await _do_mint()

    # Now that mint succeeded, update local proofs/balance view
    await w.load_proofs(reload=True)

    # Only mark claimed after successful mint
    await _mark_claimed(w.db, mint_url, req.unit, req.quote, int(time.time()))

    return MintExecuteResp(
        mint_url=mint_url,
        quote=req.quote,
        status="paid",
        paid=True,
    )

@app.post("/v1/mint/status", response_model=MintStatusResp)
async def mint_status(req: MintStatusReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)
    await w.load_mint()
    # q = await w.get_mint_quote(req.quote)

    claimed = await _is_claimed(w.db, mint_url, req.unit, req.quote)

    state_str = str(getattr(q, "state", "")) if getattr(q, "state", None) else ""
    mint_paid = (state_str == "paid)

    if claimed:
        status = "paid"
        paid = True
    elif mint_paid:
        # status = "claimable"
        status = "pending"
        paid = False
    else:
        status = "pending"
        paid = False

    return MintStatusResp(paid=paid, status=status, failed=status in {"failed","expired","canceled"})

@app.post("/v1/melt/quote", response_model=MeltQuoteResp)
async def melt_quote(req: MeltQuoteReq):
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
async def melt_execute(req: MeltExecuteReq):
    u = Unit[req.unit]
    w = await _wallet_for(req.mint_url, u)
    await w.load_mint()
    await w.load_proofs(reload=True)

    get_melt = getattr(w, "get_melt_quote", None)
    if not get_melt:
        raise HTTPException(status_code=501, detail="Wallet has no get_melt_quote(); needed for amount lookup")
    mq = await get_melt(req.quote)

    amount = int(getattr(mq, "amount", 0))
    total = amount + int(req.fee_reserve)

    send_proofs, _fees = await w.select_to_send(w.proofs, total, set_reserved=True)

    resp = await w.melt(send_proofs, req.invoice, req.fee_reserve, req.quote)

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
async def melt_status(req: MeltStatusReq):
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

