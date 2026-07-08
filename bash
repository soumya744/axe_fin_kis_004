# Author@6360513624
# CAUT-1576 – Onboard ESXi
python3 api_oneapi_consumer.py -x addHost -l myhost.phx.aexp.com -t cisco -pr myproject -loc phx -e dev --token "your-token"

# CAUT-1576 – Onboard Cluster
python3 api_oneapi_consumer.py -x addCluster -l cluster-e1 -e dev --token "your-token"

# CAUT-1577 – Disable
python3 api_oneapi_consumer.py -x disableHost -l myhost.phx.aexp.com --token "your-token"

# CAUT-1578 – Schedule Silence
python3 api_oneapi_consumer.py -x scheduleSilence -l myhost.phx.aexp.com --start "08/07/2026 10:00" --end "08/07/2026 11:00" --comment "maintenance" --token "your-token"

# CAUT-1579 – Remove Silence
python3 api_oneapi_consumer.py -x removeSilence -l myhost.phx.aexp.com --job_id "abc123" --token "your-token"

# CAUT-1580 – Enable
python3 api_oneapi_consumer.py -x enableHost -l myhost.phx.aexp.com --token "your-token"

















