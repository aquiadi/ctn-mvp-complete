import asyncio
import sys
import os
import json

from database import database, init_db
from routes.installer_routes import list_credits_for_sale, SellRequest
from routes.marketplace_routes import reserve_credits, ReserveRequest, finalize_purchase, PurchaseRequest

async def test_flow():
    await database.connect()

    print("\n--- Test 1: Idempotency ---")
    print("Running init_db() first time...")
    await init_db()
    c1 = dict(await database.fetch_one("SELECT COUNT(*) as c, SUM(co2_avoided_kg) as total_co2 FROM credits"))
    
    print("\nRunning init_db() second time...")
    await init_db()
    c2 = dict(await database.fetch_one("SELECT COUNT(*) as c, SUM(co2_avoided_kg) as total_co2 FROM credits"))
    
    print(f"Run 1: {c1['c']} credits, {c1['total_co2']} kg CO2")
    print(f"Run 2: {c2['c']} credits, {c2['total_co2']} kg CO2")
    
    if c1['c'] == c2['c']:
        print("✓ Idempotency verified: Credit count stayed the same.")
    else:
        print("✗ Idempotency failed: Credit count changed.")

    print("\n--- Test 2: Marketplace Flow ---")
    
    installer = dict(await database.fetch_one("SELECT id, email, wallet_address, role FROM users WHERE email='demo@installer.ctn'"))
    
    credits = await database.fetch_all("SELECT id, credit_id, status FROM credits WHERE owner_user_id = :uid LIMIT 5", {"uid": installer["id"]})
    credit_ids = [c["id"] for c in credits]
    print(f"Selected {len(credit_ids)} credits owned by {installer['email']}")
    
    # If they are already sold or reserved, let's reset them for the test
    await database.execute(f"UPDATE credits SET status='verified' WHERE id IN ({','.join(map(str, credit_ids))})")
    
    await list_credits_for_sale(SellRequest(credit_ids=credit_ids), user=installer)
    print("✓ Successfully listed credits for sale.")
    
    buyer_row = await database.fetch_one("SELECT id, email, role FROM users WHERE role='buyer' LIMIT 1")
    if not buyer_row:
        await database.execute("INSERT INTO users (email, password_hash, role) VALUES ('buyer@ctn.org', 'hash', 'buyer')")
        buyer_row = await database.fetch_one("SELECT id, email, role FROM users WHERE email='buyer@ctn.org'")
    buyer = dict(buyer_row)
    
    res = await reserve_credits(ReserveRequest(credit_ids=credit_ids), user=buyer)
    print(f"✓ Successfully reserved {res['quantity']} credits for ${res['total_usd']}.")
    
    # Purchase requires Reservation ID (which is the credit ID)
    purch = await finalize_purchase(PurchaseRequest(reservation_ids=credit_ids), user=buyer)
    print(f"✓ Successfully purchased credits. Receipt:")
    print(f"   Transaction ID: {purch['transaction_id']}")
    print(f"   Total USD: ${purch['total_usd']}")
    print(f"   Credits: {purch['credits_purchased']}")
    
    db_credits = await database.fetch_all(f"SELECT id, status, buyer_user_id FROM credits WHERE id IN ({','.join(map(str, credit_ids))})")
    all_sold = all(c["status"] == "sold" and c["buyer_user_id"] == buyer["id"] for c in db_credits)
    if all_sold:
        print("✓ Database verified: All 5 credits marked 'sold' to buyer.")
    else:
        print("✗ Database verification failed.")
        
    await database.disconnect()

if __name__ == "__main__":
    asyncio.run(test_flow())
