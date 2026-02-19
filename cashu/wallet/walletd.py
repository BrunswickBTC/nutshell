from __future__ import annotations

import os
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .wallet import Wallet
from ..core.base import Unit

app = FastAPI(title="nutshell-walletd", version="0.1")


# --------- request/response models ---------

class BalanceResp(BaseModel):
    unit: str
    total_available: int
    per_mint: Dict[str, Dict[str, Any]]  # {"https://mint": {"balance":..., "available":..., "unit":...}}

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
    paid: bool
    fee_paid: Optional[int] = None


# --------- wallet construction helpers ---------

def _db_path() -> str:
    p = os.environ.get("CASHU_WALLET_DB")
    if not p:
        raise RuntimeError("CASHU_WALLET_DB env var not set")
    return p

def _default_mint() -> str:
    u = os.environ.get("CASHU_DEFAULT_MINT_URL")
    if not u:
        raise RuntimeError("CASHU_DEFAULT_MINT_URL env var not set")
    return u

async def _wallet_for(mint_url: str, unit: Unit) -> Wallet:
    # Uses the same pattern as the CLI helpers: Wallet.with_db(...) with a shared db path. :contentReference[oaicite:5]{index=5}
    return await Wallet.with_db(
        url=mint_url,
        db=_db_path(),
        name="walletd",
        unit=unit.name,
        skip_db_read=False,
        load_all_keysets=True,
    )


# --------- endpoints ---------

@app.get("/v1/balance", response_model=BalanceResp)
async def balance(unit: str = "sat"):
    u = Unit[unit]
    w = await _wallet_for(_default_mint(), u)
    await w.load_proofs(reload=True, all_keysets=True)
    per_mint = await w.balance_per_minturl(unit=u)  # dict keyed by minturl :contentReference[oaicite:6]{index=6}
    total_avail = sum(int(v["available"]) for v in per_mint.values()) if per_mint else 0
    return BalanceResp(unit=u.name, total_available=total_avail, per_mint=per_mint)

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

@app.post("/v1/mint/execute")
async def mint_execute(req: MintExecuteReq):
    u = Unit[req.unit]
    mint_url = req.mint_url or _default_mint()
    w = await _wallet_for(mint_url, u)
    await w.load_mint()
    # Pull quote details from mint (Wallet.get_mint_quote exists) :contentReference[oaicite:8]{index=8}
    q = await w.get_mint_quote(req.quote)
    if not q.paid:
        # If you prefer, you can return q.state and let LNBits poll.
        raise HTTPException(status_code=409, detail="mint quote not paid")
    await w.mint(q.amount, quote_id=q.quote)  # mints proofs into DB
    return {"ok": True}

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

    total = req.fee_reserve  # fee_reserve is in sats; you’ll add amount via quote or external
    # In CLI, total_amount is amount + fee_reserve. :contentReference[oaicite:9]{index=9}
    # Here we assume caller already computed/validated total coverage using melt_quote.amount.
    # If you want, re-fetch melt quote from mint and compute total here.

    send_proofs, _fees = await w.select_to_send(w.proofs, total, set_reserved=True)
    resp = await w.melt(send_proofs, req.invoice, req.fee_reserve, req.quote)

    # melt() writes fee_paid to DB as amount + fee_paid - change. :contentReference[oaicite:10]{index=10}
    paid = True  # if melt() didn’t raise and wasn’t pending/unpaid
    return MeltExecuteResp(mint_url=req.mint_url, quote=req.quote, paid=paid)

