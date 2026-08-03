import asyncio
from database import database
async def test():
    await database.connect()
    res = await database.fetch_all("SELECT c.owner_user_id, COUNT(c.id) as credit_count, SUM(c.total_kwh) as total_kwh, SUM(c.co2_avoided_kg) as co2_avoided_kg, GROUP_CONCAT(c.id) as credit_ids FROM credits c LEFT JOIN users u ON c.owner_user_id = u.id WHERE c.status = 'listed' AND c.contract_version = 'new' GROUP BY c.owner_user_id, c.location, u.email ORDER BY MAX(c.listed_at) DESC LIMIT 20 OFFSET 0")
    print(res)
    await database.disconnect()
asyncio.run(test())
