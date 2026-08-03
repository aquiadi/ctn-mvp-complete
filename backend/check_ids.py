import asyncio
from database import database
async def run():
    await database.connect()
    await database.execute("DELETE FROM credits WHERE credit_id = 9999")
    res = await database.fetch_one("SELECT COUNT(*) as cnt, MIN(credit_id) as min_id, MAX(credit_id) as max_id FROM credits")
    print(dict(res))
    await database.disconnect()
asyncio.run(run())
