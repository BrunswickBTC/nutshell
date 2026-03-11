# Nutshell walletd UDS API

This document describes the Unix Domain Socket (UDS) HTTP API exposed by
`cashu.wallet.walletd:app`.

The API is designed for local integration with clients such as LNbits
and follows this handle model:

-   **Mint operations** are keyed by:
    -   `mint_url`
    -   `unit`
    -   `quote`
-   **Melt operations** are keyed by:
    -   `payment_hash`

The API is intended to support asynchronous clients that may manage
multiple mints and multiple units.

This version of the document assumes the API has been simplified to use
a **single monotonic lifecycle `status` field** and **does not return
redundant `paid` or `failed` booleans**.

------------------------------------------------------------------------

# 1. Transport

walletd is served over an HTTP API bound to a Unix Domain Socket.

Example socket path:

``` text
/run/cashu/walletd.sock
```

Example server start:

``` bash
uvicorn cashu.wallet.walletd:app --uds /run/cashu/walletd.sock
```

All examples below use `curl --unix-socket`.

Base URL convention for curl:

``` text
http://localhost
```

------------------------------------------------------------------------

# 2. General Notes

## 2.1 Units

`unit` is currently expected to be something like:

-   `sat`

## 2.2 Mint handles

Mint quote, execute, and status all use the same external handle:

``` json
{
  "mint_url": "...",
  "unit": "sat",
  "quote": "..."
}
```

## 2.3 Melt handles

Melt quote returns the external handle:

``` json
{
  "payment_hash": "64 hex chars"
}
```

That `payment_hash` is then used for melt execute and melt status.

## 2.4 BOLT11

Melt quote accepts a **BOLT11 invoice string** in the `invoice` field.

The API derives the canonical `payment_hash` from that invoice
internally.

## 2.5 Idempotency

-   `mint/execute` is safe to retry.
-   `melt/execute` is safe to retry.
-   `mint/status` is read-only.
-   `melt/status` is read-only from the client's point of view.
    Internally it may reconcile stale execution against the mint, but it
    does not initiate a new melt payment.

## 2.6 Monotonic lifecycle status

The `status` field is the authoritative lifecycle indicator.

For a given handle, `status` is intended to be **monotonic**: once an
operation advances to a later lifecycle stage, walletd should not later
return an earlier one.

### Mint lifecycle ordering

Valid mint lifecycle progression is intended to follow:

``` text
pending -> claimable -> paid
pending -> failed
pending -> expired
pending -> canceled
claimable -> paid
claimable -> failed
```

### Melt lifecycle ordering

Valid melt lifecycle progression is intended to follow:

``` text
pending -> paid
pending -> failed
```

Because `status` carries the lifecycle meaning, the API no longer
returns separate `paid` or `failed` booleans.

------------------------------------------------------------------------

# 3. How LNbits Is Intended to Use This API

This section describes the **high-level algorithm**, not just the
request and response shapes.

The core idea is:

-   **Mint side**: LNbits is receiving funds and later claiming ecash
    tokens.
-   **Melt side**: LNbits is spending ecash tokens to pay a Lightning
    invoice.

These two flows have different handles and different execution timing.

------------------------------------------------------------------------

## 3.1 Mint lifecycle from LNbits' point of view

Minting is a **two-phase** operation:

1.  Ask the mint for a Lightning invoice.
2.  After that invoice is paid, claim tokens from the mint.

### Step-by-step algorithm

#### Step 1: Request a mint quote

LNbits calls:

-   `POST /v1/mint/quote`

and receives:

-   `mint_url`
-   `unit`
-   `quote`
-   `request` (the BOLT11 invoice)
-   `amount`

LNbits stores the tuple:

``` text
(mint_url, unit, quote)
```

as the handle for this minting operation.

#### Step 2: Wait for the invoice to be paid

LNbits presents the returned BOLT11 invoice to the payer, or otherwise
waits for settlement.

LNbits does **not** call `mint/execute` immediately.

#### Step 3: Poll mint status

LNbits periodically calls:

-   `POST /v1/mint/status`

using the same handle.

Interpretation of the result:

-   `status = "pending"`\
    Invoice not yet paid. Continue polling.

-   `status = "claimable"`\
    Invoice has been paid, but tokens have not yet been claimed locally.
    LNbits should now call `mint/execute`.

-   `status = "paid"`\
    Tokens have already been claimed and minted locally. Treat the
    incoming payment as settled.

-   `status = "failed"`, `expired`, or `canceled`\
    Terminal failure or expiration. Stop polling and treat as
    unsuccessful.

#### Step 4: Claim tokens

When `mint/status` returns `claimable`, LNbits calls:

-   `POST /v1/mint/execute`

with the same handle.

This is the moment walletd actually mints proofs into the local wallet.

#### Step 5: Retry safely if needed

If LNbits crashes or times out after calling `mint/execute`, it may
safely retry.

The intended behavior is:

-   unpaid quote -\> no side effects
-   already claimed quote -\> stable `paid` result
-   paid-but-unclaimed quote -\> tokens are claimed exactly once

### Mint flow summary

**Rule:**\
Call `mint/execute` **only after** `mint/status` returns `claimable`.

**Do not use** `mint/execute` as a probe for payment.

Use `mint/status` as the decision point and `mint/execute` as the claim
step.

------------------------------------------------------------------------

## 3.2 Melt lifecycle from LNbits' point of view

Melting is also a two-phase operation:

1.  Ask walletd for a quote to pay an invoice.
2.  Execute that payment after approval.

### Step-by-step algorithm

#### Step 1: Request a melt quote

LNbits calls:

-   `POST /v1/melt/quote`

with:

-   `mint_url`
-   `unit`
-   `invoice` (BOLT11)

walletd:

-   decodes the invoice
-   derives the canonical `payment_hash`
-   obtains a melt quote from the mint
-   stores a lifecycle row internally
-   returns:
    -   `mint_url`
    -   `unit`
    -   `payment_hash`
    -   `amount`
    -   `fee_reserve`

LNbits stores:

``` text
payment_hash
```

as the handle for this outgoing payment.

#### Step 2: Operator or policy approves the quote

The `fee_reserve` allows LNbits or the operator to decide whether the
quoted maximum fee is acceptable.

#### Step 3: Execute the melt

When approved, LNbits calls:

-   `POST /v1/melt/execute`

with:

``` json
{
  "payment_hash": "..."
}
```

This is the moment walletd actually:

-   locks the lifecycle row
-   selects proofs
-   calls `w.melt()`
-   persists the result

#### Step 4: Poll melt status

LNbits then polls:

-   `GET /v1/melt/status/{payment_hash}`

Interpretation:

-   `status = "pending"`\
    Payment is still in progress or not yet proven terminal. Continue
    polling.

-   `status = "paid"`\
    Payment succeeded. `preimage` and `fee_paid_sat` may be available.

-   `status = "failed"`\
    Payment failed terminally.

#### Step 5: Retry safely if needed

If LNbits times out or loses connection while executing, it may retry
`melt/execute` with the same `payment_hash`.

walletd is designed so that:

-   terminal melts return terminal state
-   concurrent execution is suppressed by DB locking
-   stale executing states can be reconciled during `melt/status`

### Melt flow summary

**Rule:**\
Call `melt/execute` immediately after `melt/quote` once the quoted fee
reserve is accepted.

Then poll `melt/status/{payment_hash}` until terminal.

------------------------------------------------------------------------

# 4. Endpoints

------------------------------------------------------------------------

# 4.1 GET `/v1/balance`

Return the current wallet balance summary.

## Query Parameters

  ------------------------------------------------------------------------
  Field                        Type        Required         Meaning
  -------------- ------------------ ----------------------- --------------
  `unit`                     string           no            Unit to query.
                                                            Defaults to
                                                            the wallet's
                                                            configured
                                                            default unit.

  ------------------------------------------------------------------------

## Response Type

``` json
{
  "wallet": "wallet-name",
  "unit": "sat",
  "available": 1234,
  "balance": 1234,
  "default_mint": "https://mint.example/Bitcoin",
  "per_mint": {
    "https://mint.example/Bitcoin": {
      "available": 1234
    }
  }
}
```

## Response Fields

  ------------------------------------------------------------------------
  Field                                         Type Meaning
  --------------------- ---------------------------- ---------------------
  `wallet`                                    string Wallet name from
                                                     configuration.

  `unit`                                      string Unit used for this
                                                     query.

  `available`                                integer Total available
                                                     balance across mints
                                                     for the selected
                                                     unit.

  `balance`                                  integer Same value as
                                                     `available`.

  `default_mint`                              string Default configured
                                                     mint URL.

  `per_mint`                                  object Mint-by-mint balance
                                                     breakdown as returned
                                                     by the underlying
                                                     wallet.
  ------------------------------------------------------------------------

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  http://localhost/v1/balance
```

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  "http://localhost/v1/balance?unit=sat"
```

------------------------------------------------------------------------

# 4.2 POST `/v1/mint/quote`

Request a mint quote and receive a Lightning invoice to be paid in order
to mint tokens.

## Request Type: `MintQuoteReq`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "amount": 1000,
  "memo": "optional note"
}
```

## Request Fields

  ------------------------------------------------------------------------
  Field                        Type        Required         Meaning
  -------------- ------------------ ----------------------- --------------
  `mint_url`                 string           no            Mint URL to
                                                            receive tokens
                                                            from. Defaults
                                                            to the
                                                            configured
                                                            default mint.

  `unit`                     string           no            Unit to mint.
                                                            Defaults to
                                                            `sat`.

  `amount`                  integer           yes           Amount of
                                                            tokens to
                                                            mint.

  `memo`                     string           no            Optional memo
                                                            passed through
                                                            to the mint
                                                            quote request.
  ------------------------------------------------------------------------

## Response Type: `MintQuoteResp`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "quote": "mint-quote-id",
  "amount": 1000,
  "request": "lnbc..."
}
```

## Response Fields

  ------------------------------------------------------------------------
  Field                                         Type Meaning
  --------------------- ---------------------------- ---------------------
  `mint_url`                                  string Mint that issued the
                                                     quote.

  `unit`                                      string Unit for the quote.

  `quote`                                     string Mint-generated quote
                                                     handle. This is part
                                                     of the external
                                                     handle for mint
                                                     execute and mint
                                                     status.

  `amount`                                   integer Requested mint
                                                     amount.

  `request`                                   string BOLT11 Lightning
                                                     invoice that must be
                                                     paid before tokens
                                                     can be claimed.
  ------------------------------------------------------------------------

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/mint/quote \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "amount": 1000
  }'
```

------------------------------------------------------------------------

# 4.3 POST `/v1/mint/status`

Check whether a mint quote is pending, claimable, already claimed, or
failed.

## Request Type: `MintStatusReq`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "quote": "mint-quote-id"
}
```

## Request Fields

  -----------------------------------------------------------------------------
  Field                        Type        Required         Meaning
  -------------- ------------------ ----------------------- -------------------
  `mint_url`                 string           no            Mint URL. Defaults
                                                            to the configured
                                                            default mint.

  `unit`                     string           no            Unit. Defaults to
                                                            `sat`.

  `quote`                    string           yes           Mint quote handle
                                                            returned by
                                                            `/v1/mint/quote`.
  -----------------------------------------------------------------------------

## Response Type: `MintStatusResp`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "quote": "mint-quote-id",
  "status": "claimable"
}
```

## Response Fields

  ------------------------------------------------------------------------
  Field                                         Type Meaning
  --------------------- ---------------------------- ---------------------
  `mint_url`                                  string Mint associated with
                                                     the quote.

  `unit`                                      string Unit associated with
                                                     the quote.

  `quote`                                     string Mint quote handle.

  `status`                                    string One of: `paid`,
                                                     `claimable`,
                                                     `failed`, `expired`,
                                                     `canceled`,
                                                     `pending`.
  ------------------------------------------------------------------------

## Status Meanings

  -----------------------------------------------------------------------
  Status                              Meaning
  ----------------------------------- -----------------------------------
  `pending`                           Invoice has not yet been paid, or
                                      the mint does not yet report
                                      payment.

  `claimable`                         Invoice is paid, but the tokens
                                      have not yet been claimed locally.
                                      Client should call
                                      `/v1/mint/execute`.

  `paid`                              Tokens have already been claimed
                                      and minted locally.

  `failed`                            Mint quote failed.

  `expired`                           Mint quote expired.

  `canceled`                          Mint quote canceled.
  -----------------------------------------------------------------------

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/mint/status \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "quote": "mint-quote-id"
  }'
```

------------------------------------------------------------------------

# 4.4 POST `/v1/mint/execute`

Claim tokens for a mint quote **after** the invoice has been paid and
`mint/status` reports `claimable`.

This call has no side effects if the quote is not yet paid.

## Request Type: `MintExecuteReq`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "quote": "mint-quote-id"
}
```

## Request Fields

  ------------------------------------------------------------------------
  Field                        Type        Required         Meaning
  -------------- ------------------ ----------------------- --------------
  `mint_url`                 string           no            Mint URL.
                                                            Defaults to
                                                            configured
                                                            default mint.

  `unit`                     string           no            Unit. Defaults
                                                            to `sat`.

  `quote`                    string           yes           Mint quote
                                                            handle.
  ------------------------------------------------------------------------

## Response Type: `MintExecuteResp`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "quote": "mint-quote-id",
  "status": "paid"
}
```

## Response Fields

  Field            Type Meaning
  ------------ -------- ---------------------------------------
  `mint_url`     string Mint associated with this mint quote.
  `unit`         string Unit associated with this mint quote.
  `quote`        string Mint quote handle.
  `status`       string `pending`, `paid`, or `failed`.

## Execute Semantics

-   If the quote is still unpaid:
    -   returns `status: pending`
    -   has no minting side effects
-   If the quote is paid:
    -   mints proofs
    -   records the quote as claimed
    -   returns `status: paid`

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/mint/execute \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "quote": "mint-quote-id"
  }'
```

------------------------------------------------------------------------

# 4.5 POST `/v1/melt/quote`

Request a quote to pay a BOLT11 invoice from the selected Cashu wallet.

This call derives and returns the `payment_hash` handle that is used for
subsequent melt execute and melt status calls.

## Request Type: `MeltQuoteReq`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "invoice": "lnbc..."
}
```

## Request Fields

  ------------------------------------------------------------------------
  Field                        Type        Required         Meaning
  -------------- ------------------ ----------------------- --------------
  `mint_url`                 string           no            Mint URL from
                                                            which proofs
                                                            will be spent.
                                                            Defaults to
                                                            configured
                                                            default mint.

  `unit`                     string           no            Unit to spend.
                                                            Defaults to
                                                            `sat`.

  `invoice`                  string           yes           BOLT11
                                                            Lightning
                                                            invoice to
                                                            pay.
  ------------------------------------------------------------------------

## Response Type: `MeltQuoteResp`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "payment_hash": "64hex...",
  "amount": 1000,
  "fee_reserve": 10
}
```

## Response Fields

  ------------------------------------------------------------------------
  Field                                         Type Meaning
  --------------------- ---------------------------- ---------------------
  `mint_url`                                  string Mint from which
                                                     proofs will be spent.

  `unit`                                      string Unit used for the
                                                     melt.

  `payment_hash`                              string Canonical external
                                                     handle for this melt
                                                     lifecycle, derived
                                                     from the BOLT11
                                                     invoice.

  `amount`                                   integer Invoice amount to be
                                                     paid.

  `fee_reserve`                              integer Maximum fee reserve
                                                     estimated by the mint
                                                     for this payment.
  ------------------------------------------------------------------------

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/melt/quote \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "invoice": "lnbc..."
  }'
```

------------------------------------------------------------------------

# 4.6 POST `/v1/melt/execute`

Execute a melt operation using the `payment_hash` handle returned by
`/v1/melt/quote`.

This is typically called immediately after the operator or client
approves the quote.

## Request Type: `MeltExecuteReq`

``` json
{
  "payment_hash": "64hex..."
}
```

## Request Fields

  -------------------------------------------------------------------------------
  Field                          Type        Required         Meaning
  ---------------- ------------------ ----------------------- -------------------
  `payment_hash`               string           yes           Melt lifecycle
                                                              handle returned by
                                                              `/v1/melt/quote`.

  -------------------------------------------------------------------------------

## Response Type: `MeltExecuteResp`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "payment_hash": "64hex...",
  "fee_paid_sat": 7,
  "preimage": "hex-preimage",
  "status": "paid"
}
```

## Response Fields

  ------------------------------------------------------------------------
  Field                                         Type Meaning
  --------------------- ---------------------------- ---------------------
  `mint_url`                                  string Mint associated with
                                                     this melt.

  `unit`                                      string Unit associated with
                                                     this melt.

  `payment_hash`                              string Melt lifecycle
                                                     handle.

  `fee_paid_sat`                     integer or null Actual routing fee
                                                     paid in satoshis, if
                                                     known.

  `preimage`                          string or null Lightning preimage if
                                                     the payment
                                                     succeeded.

  `status`                                    string `paid`, `pending`, or
                                                     `failed`.
  ------------------------------------------------------------------------

## Execute Semantics

-   If the melt is already terminal:
    -   returns terminal state immediately
-   If another execution is already in progress:
    -   returns current DB-tracked state
-   If execution proceeds:
    -   reserves proofs
    -   attempts payment
    -   persists terminal or pending state
    -   returns DB-derived truth

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/melt/execute \
  -H "Content-Type: application/json" \
  -d '{
    "payment_hash": "64hex..."
  }'
```

------------------------------------------------------------------------

# 4.7 GET `/v1/melt/status/{payment_hash}`

Query melt payment status by `payment_hash`.

## Path Parameters

  -------------------------------------------------------------------------------
  Field                          Type        Required         Meaning
  ---------------- ------------------ ----------------------- -------------------
  `payment_hash`               string           yes           Melt lifecycle
                                                              handle returned by
                                                              `/v1/melt/quote`.

  -------------------------------------------------------------------------------

## Response Type: `MeltStatusResp`

``` json
{
  "mint_url": "https://mint.example/Bitcoin",
  "unit": "sat",
  "payment_hash": "64hex...",
  "fee_paid_sat": 7,
  "preimage": "hex-preimage",
  "status": "paid"
}
```

## Response Fields

  ------------------------------------------------------------------------
  Field                                         Type Meaning
  --------------------- ---------------------------- ---------------------
  `mint_url`                                  string Mint associated with
                                                     this melt.

  `unit`                                      string Unit associated with
                                                     this melt.

  `payment_hash`                              string Melt lifecycle
                                                     handle.

  `fee_paid_sat`                     integer or null Actual routing fee
                                                     paid in satoshis, if
                                                     known.

  `preimage`                          string or null Lightning preimage if
                                                     available.

  `status`                                    string `paid`, `pending`, or
                                                     `failed`.
  ------------------------------------------------------------------------

## Status Semantics

-   If terminal:
    -   returns final status immediately
-   If non-terminal and not executing:
    -   returns `pending`
-   If executing and not stale:
    -   returns `pending`
-   If executing and stale:
    -   performs internal reconciliation using the stored internal mint
        quote
    -   if reconciliation proves success or failure, terminalizes and
        returns terminal state
    -   otherwise continues to return `pending`

## Example

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  http://localhost/v1/melt/status/64hex...
```

------------------------------------------------------------------------

# 5. Typical curl Flows

## 5.1 Mint flow

### Request a quote

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/mint/quote \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "amount": 1000
  }'
```

### Poll until `claimable`

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/mint/status \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "quote": "mint-quote-id"
  }'
```

### Execute claim

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/mint/execute \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "quote": "mint-quote-id"
  }'
```

## 5.2 Melt flow

### Request a quote

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/melt/quote \
  -H "Content-Type: application/json" \
  -d '{
    "mint_url": "https://mint.example/Bitcoin",
    "unit": "sat",
    "invoice": "lnbc..."
  }'
```

### Execute payment

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  -X POST http://localhost/v1/melt/execute \
  -H "Content-Type: application/json" \
  -d '{
    "payment_hash": "64hex..."
  }'
```

### Poll payment status

``` bash
curl --unix-socket /run/cashu/walletd.sock \
  http://localhost/v1/melt/status/64hex...
```

------------------------------------------------------------------------

# 6. Error Handling

walletd uses standard FastAPI HTTP errors.

Typical error body:

``` json
{
  "detail": "Unknown payment_hash"
}
```

## Common cases

    HTTP Status Meaning
  ------------- ------------------------------------------------
          `404` Unknown `payment_hash`
          `422` Validation error in request body or parameters
          `500` Internal server error

------------------------------------------------------------------------

# 7. Internal vs External Identifiers

## Mint

External identifier: - `quote`

Internal meaning: - mint-provided handle for invoice settlement and
claim

## Melt

External identifier: - `payment_hash`

Internal identifiers also stored: - mint melt `quote` - original BOLT11
invoice

The internal melt quote is not exposed through the API. It is stored
only so walletd can reconcile stale melt execution states with the mint.

------------------------------------------------------------------------

# 8. Summary

This API intentionally uses two different handle patterns:

## Mint

Use: - `mint_url` - `unit` - `quote`

## Melt

Use: - `payment_hash`

This allows clients to: - support multiple wallets and mints - operate
asynchronously - safely retry execute operations - poll status
deterministically - distinguish receiving-token workflows from
paying-invoice workflows

The single `status` field is the authoritative lifecycle indicator.
Clients should interpret lifecycle exclusively from `status`, not from
additional booleans.

