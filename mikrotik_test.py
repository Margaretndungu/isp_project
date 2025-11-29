from routeros_api import RouterOsApiPool

# MikroTik details
host = '10.10.0.1'             # Your MikroTik router's IP address
username = 'admin'             # Change if you’re using a different user
password = 'TheLion'           # The correct password for that user

try:
    # Connect using plaintext login (safe for most MikroTik setups)
    api_pool = RouterOsApiPool(
        host=host,
        username=username,
        password=password,
        plaintext_login=True
    )

    api = api_pool.get_api()

    # Fetch active hotspot users
    hotspot_users = api.get_resource('/ip/hotspot/active')
    users = hotspot_users.get()

    print("✅ Connected to MikroTik @ 10.10.0.1")
    if users:
        print("📡 Active Hotspot Users:")
        for user in users:
            print(f"- User: {user.get('user')} | IP: {user.get('address')} | Uptime: {user.get('uptime')}")
    else:
        print("ℹ️ No active hotspot users at the moment.")

    api_pool.disconnect()

except Exception as e:
    print("❌ Failed to connect:", e)
