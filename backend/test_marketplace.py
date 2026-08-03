import asyncio
from database import database

async def test():
    await database.connect()
    # Insert a dummy user
    await database.execute("INSERT INTO users (id, email, password_hash, role) VALUES (99, 'test@seller', 'hash', 'installer') ON CONFLICT DO NOTHING")
    # Insert a listed credit
    await database.execute("""
        INSERT INTO credits (credit_id, device_id, owner_user_id, status, contract_version, total_kwh, co2_avoided_kg, listed_at)
        VALUES (9999, 'dev1', 99, 'listed', 'new', 100, 50, 123456789)
        ON CONFLICT(credit_id) DO UPDATE SET status='listed'
    """)
    
    # Run query
    batches = await database.fetch_all(
        query="""SELECT c.owner_user_id, u.email as seller_email, c.location,
                 COUNT(c.id) as credit_count,
                 SUM(c.total_kwh) as total_kwh,
                 SUM(c.co2_avoided_kg) as co2_avoided_kg,
                 GROUP_CONCAT(c.id) as credit_ids
                 FROM credits c
                 LEFT JOIN users u ON c.owner_user_id = u.id
                 WHERE c.status = 'listed' AND c.contract_version = 'new'
                 GROUP BY c.owner_user_id, c.location, u.email
                 ORDER BY MAX(c.listed_at) DESC
                 LIMIT 20 OFFSET 0"""
    )
    
    formatted_batches = []
    for b in batches:
        b_dict = dict(b)
        credit_ids = [int(i) for i in b_dict["credit_ids"].split(",")] if b_dict["credit_ids"] else []
        print(b_dict)
        print(credit_ids)
        
    await database.disconnect()

asyncio.run(test())
