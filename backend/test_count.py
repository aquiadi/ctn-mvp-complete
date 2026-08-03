import asyncio
from database import database
async def test():
    await database.connect()
    res = await database.fetch_one("SELECT COUNT(*) as cnt FROM credits WHERE status = 'listed' AND contract_version = 'new'")
    print(dict(res))
    await database.disconnect()
asyncio.run(test())
