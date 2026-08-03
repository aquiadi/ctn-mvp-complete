import asyncio
from database import database
import os

async def test():
    # create a fresh db
    if os.path.exists("test.db"): os.remove("test.db")
    import databases
    db = databases.Database("sqlite:///test.db")
    await db.connect()
    await db.execute("CREATE TABLE credits (id INTEGER, status TEXT, contract_version TEXT)")
    
    total = await db.fetch_one("SELECT COUNT(*) as cnt FROM credits WHERE status = 'listed' AND contract_version = 'new'")
    print(total)
    await db.disconnect()

asyncio.run(test())
