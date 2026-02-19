#!/usr/bin/python3

import httpx

transport = httpx.AsyncHTTPTransport(uds="/run/cashu/walletd.sock")
client = httpx.AsyncClient(transport=transport, base_url="http://unix")

r = await client.get("/v1/balance")

