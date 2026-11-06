from datetime import datetime, timedelta
from polygon import RESTClient

import os
poly = os.environ["POLYGON_API_KEY"]
client = RESTClient(api_key=poly, trace=True, pagination=False)  # fixed-size, debuggable
today = datetime.now().strftime("%Y-%m-%d")
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

bars = [a for a in client.list_aggs("SPY", 1, "minute", from_=today, to=tomorrow, limit=50000)]
print("bars:", len(bars), bars[:2])