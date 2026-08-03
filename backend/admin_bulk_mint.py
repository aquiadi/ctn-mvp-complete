import asyncio
import os
import sys

# Load environment variables if needed
from database import database
from routes.admin_routes import mint_credit, MintReason

async def bulk_mint():
    # 1. Connect to DB
    await database.connect()
    
    # 2. Get the admin user ID (assuming admin is id=1 from init_db)
    admin_email = os.getenv("ADMIN_EMAIL", "admin@ctn.org")
    admin = await database.fetch_one("SELECT id FROM users WHERE email = :email", {"email": admin_email})
    if not admin:
        print("Admin user not found. Run init_db first.")
        await database.disconnect()
        return
    
    admin_id = admin["id"]
    admin_user = {"id": admin_id, "email": admin_email, "role": "admin"}
    
    # 3. Find all verified, unminted credits
    # Using contract_version = 'new' because we updated the logic to use 'new'
    unminted = await database.fetch_all(
        query="SELECT credit_id FROM credits WHERE status = 'verified' AND contract_version = 'new'"
    )
    
    print(f"Found {len(unminted)} verified, unminted credits. Minting...")
    
    success_count = 0
    error_count = 0
    
    reason = MintReason(reason="Bulk mint for Minerva MVP demo")
    
    for row in unminted:
        cid = row["credit_id"]
        try:
            # We call the route handler function directly
            await mint_credit(credit_id=cid, req=reason, admin=admin_user)
            success_count += 1
            if success_count % 10 == 0:
                print(f"Minted {success_count}/{len(unminted)}...")
        except Exception as e:
            print(f"Failed to mint credit {cid}: {e}")
            error_count += 1
            
    print(f"\nDone. Successfully minted: {success_count}. Failed: {error_count}.")
    
    await database.disconnect()

if __name__ == "__main__":
    asyncio.run(bulk_mint())
