# Hoongora Travel (Flights + Hotels Lowest)

Flask web app that:
- Scans a month to find the cheapest flight (fast-flights)
- Searches hotels for that itinerary (LiteAPI)
- Shows **Flight + Cheapest Hotel** combined total

## Environment variables
Required:
- `LITEAPI_KEY` — LiteAPI key

Optional:
- `ACCESS_KEY` — if set, the app requires `?key=ACCESS_KEY` on every request (simple friend-only gate)

## Run locally
```bash
pip install -r requirements.txt
export LITEAPI_KEY="your_key"
python app.py
```

Open:
- http://127.0.0.1:5000

## Deploy to Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`
- Set env vars in Render: `LITEAPI_KEY`, (optional) `ACCESS_KEY`

## Custom domain
After deploying, add `hoongora.me` and `www.hoongora.me` in Render **Custom Domains**, then copy the DNS (A/CNAME) values into Gabia DNS records.
