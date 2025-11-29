import requests
import datetime
import base64
from requests.auth import HTTPBasicAuth

def get_access_token(consumer_key, consumer_secret):
    """
    Generate OAuth token from Safaricom Daraja API
    """
    url = "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"

    try:
        response = requests.get(url, auth=HTTPBasicAuth(consumer_key, consumer_secret))
        if response.status_code == 200:
            token = response.json().get('access_token')
            print("✅ Access Token generated successfully.")
            return token
        else:
            print("❌ Failed to get access token")
            print("Status Code:", response.status_code)
            print("Response:", response.text)
            return None
    except Exception as e:
        print("⚠️ Exception during access token generation:", str(e))
        return None


def initiate_stk_push(
    consumer_key,
    consumer_secret,
    business_short_code,
    passkey,
    amount,
    phone_number,
    callback_url,
    account_reference="Customer",  # ✅ New param for user name
    transaction_desc="Internet Package Purchase"  # ✅ New param for better description
):
    """
    Initiate STK Push Request to Safaricom Daraja API
    """
    access_token = get_access_token(consumer_key, consumer_secret)
    if not access_token:
        return {"error": "Failed to get token"}

    # Create timestamp
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')

    # Encode password
    data_to_encode = business_short_code + passkey + timestamp
    encoded_password = base64.b64encode(data_to_encode.encode()).decode('utf-8')

    stk_url = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # ✅ Safaricom requires AccountReference <= 20 chars
    safe_reference = account_reference[:20]

    payload = {
        "BusinessShortCode": business_short_code,
        "Password": encoded_password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": business_short_code,
        "PhoneNumber": phone_number,
        "CallBackURL": callback_url,
        "AccountReference": safe_reference,  # ✅ Use client name here
        "TransactionDesc": transaction_desc[:100]
    }

    try:
        response = requests.post(stk_url, json=payload, headers=headers)
        print("📤 STK Push Request Sent. Payload:", payload)
        return response.json()
    except Exception as e:
        print("❌ Error during STK push:", str(e))
        return {"error": "STK Push failed"}
