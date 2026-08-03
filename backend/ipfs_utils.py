import os
import requests

PINATA_API_KEY = os.getenv("PINATA_API_KEY")
PINATA_SECRET = os.getenv("PINATA_SECRET")

def upload_credit_to_ipfs(credit: dict) -> str:
    """
    Upload a credit certificate to IPFS via Pinata.
    Returns the IPFS hash (CID).
    """
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "pinata_api_key": PINATA_API_KEY,
        "pinata_secret_api_key": PINATA_SECRET
    }
    payload = {
        "pinataContent": credit,
        "pinataMetadata": {"name": f"credit_{credit.get('credit_id', 'new')}"}
    }
    
    # If no credentials are provided, return a simulated hash for local dev
    if not PINATA_API_KEY or not PINATA_SECRET:
        print("⚠ No Pinata credentials found, simulating IPFS upload")
        return f"simulated_hash_{credit.get('credit_id', 'new')}"
        
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    return r.json()["IpfsHash"]
