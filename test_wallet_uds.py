#!/usr/bin/env python3

import asyncio
import httpx


async def main():
    transport = httpx.AsyncHTTPTransport(uds="/run/cashu/walletd.sock")
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://unix",
    ) as client:
        r = await client.get("/v1/balance")
        print(r.status_code)
        print(r.json())


if __name__ == "__main__":
    asyncio.run(main())

