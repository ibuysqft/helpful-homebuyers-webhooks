# Render Deployment — Helpful Homebuyers Webhook Server

## Current Status: DEPLOYED AND LIVE

**Service URL:** https://helpful-homebuyers-webhooks.onrender.com
**Health check:** `GET /health` → `{"status":"ok","version":"2.0.0","pipeline_stages_cached":254}`
**Render repo:** https://github.com/ibuysqft/helpful-homebuyers-webhooks
**Render dashboard:** https://dashboard.render.com (owner: tea-d1ur6iemcj7s73epbps0)

The service is active, not suspended, and responding correctly. No redeployment needed.

---

## Service Configuration

| Setting | Value |
|---------|-------|
| Service type | Web Service |
| Runtime | Python |
| Region | Oregon (US West) |
| Plan | Free |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |

---

## Environment Variables (already set on Render)

| Variable | Value |
|----------|-------|
| `GHL_API_KEY` | `your-ghl-api-key` |
| `GHL_LOCATION_ID` | `your-ghl-location-id` |
| `CALENDAR_ID` | `2xJXutj4eTskFPYx8AeL` |
| `CALENDAR_ROUTING_MODE` | `fixed_owner` |
| `CALENDAR_FIXED_OWNER_ID` | `2xJXutj4eTskFPYx8AeL` |
| `CALENDAR_FIXED_OWNER_KEY` | `jeffrey_bord` |
| `CALENDAR_FIXED_OWNER_NAME` | `Jeffrey Bord` |
| `CALENDAR_REP_CONFIG_JSON` | blank until round robin is enabled |
| `APPT_DURATION_MIN` | `30` |
| `COMP_PULLER_URL` | `https://helpful-homebuyers-comp-puller.onrender.com` |

These are defined in `render.yaml` at the repo root. Render reads this file on deploy.

`CALENDAR_ID` remains in place for backward compatibility, but the webhook now resolves appointment calendars through the routing layer. In production today that still resolves to Jeffrey Bord's personal calendar.

---

## Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check — returns version + cache stats |
| POST | `/shelby-check-calendar` | Retell: check available slots |
| POST | `/alex-check-calendar` | Retell: check available slots |
| POST | `/cole-check-calendar` | Retell: check available slots |
| POST | `/jordan-check-calendar` | Retell: check available slots |
| POST | `/shelby-book-appointment` | Retell: book calendar event |
| POST | `/alex-book-appointment` | Retell: book calendar event |
| POST | `/cole-book-appointment` | Retell: book calendar event |
| POST | `/jordan-book-appointment` | Retell: book calendar event |
| POST | `/shelby-send-sms` | Retell: send SMS mid-call |
| POST | `/alex-send-sms` | Retell: send SMS mid-call |
| POST | `/cole-send-sms` | Retell: send SMS mid-call |
| POST | `/jordan-send-sms` | Retell: send SMS mid-call |
| POST | `/retell-call-outcome` | Post-call: update stage, send SMS, apply tags |
| POST | `/retell-call-started` | Call start logging |

---

## Appointment Routing

Current live mode is `fixed_owner`, which routes all appointment checks and bookings to Jeffrey Bord:

- `CALENDAR_ROUTING_MODE=fixed_owner`
- `CALENDAR_FIXED_OWNER_ID=2xJXutj4eTskFPYx8AeL`
- `CALENDAR_FIXED_OWNER_KEY=jeffrey_bord`
- `CALENDAR_FIXED_OWNER_NAME=Jeffrey Bord`

The check and booking routes now also return routing metadata so operators can confirm which calendar handled the request:

- `calendar_id`
- `calendar_owner_key`
- `calendar_owner_name`
- `routing_mode`
- `routing_reason`

### Future Round Robin

When you are ready to enable multi-rep scheduling, keep the same routes and set:

```bash
CALENDAR_ROUTING_MODE=round_robin
CALENDAR_REP_CONFIG_JSON='[
  {
    "owner_key": "jeffrey_bord",
    "owner_name": "Jeffrey Bord",
    "calendar_id": "2xJXutj4eTskFPYx8AeL",
    "location_id": "your-ghl-location-id",
    "active": true
  },
  {
    "owner_key": "harper",
    "owner_name": "Harper",
    "calendar_id": "replace-with-ghl-calendar-id",
    "location_id": "your-ghl-location-id",
    "active": true
  }
]'
```

The safest future client flow is:
1. Call `/{agent}-check-calendar`
2. Read the returned `calendar_id` and `calendar_owner_key`
3. Pass those same values into `/{agent}-book-appointment`

That pins the booking to the same calendar chosen during the availability check.

---

## Free Tier Gotchas

- **Spin-down:** Service sleeps after 15 min of inactivity. First request takes 30–45 seconds to wake.
- **No background workers on free tier.** All processing must be synchronous within the web request. This server already handles this correctly.

---

## Keepalive — Prevent Cold Starts on Free Tier

The `/health` endpoint is ready. Wire up an external ping every 10 minutes via one of:

**Option A — cron-job.org (free, zero config)**
1. Go to https://cron-job.org and create a free account
2. Add job: `GET https://helpful-homebuyers-webhooks.onrender.com/health`
3. Schedule: every 10 minutes
4. Enable "Save responses" to detect failures

**Option B — Vercel cron (already in helpfulhomebuyersusa project)**
Add to `vercel.json`:
```json
{
  "crons": [
    {
      "path": "/api/keepalive",
      "schedule": "*/10 * * * *"
    }
  ]
}
```
Create `app/api/keepalive/route.ts` that pings all Render service `/health` endpoints.

**Option C — GHL scheduled workflow**
Add a scheduled trigger (every 10 min) → Webhook action → `GET https://helpful-homebuyers-webhooks.onrender.com/health`

All other Render services that need keepalive:
- `https://helpful-homebuyers-comp-puller.onrender.com/health`
- `https://helpful-homebuyers-lead-to-call.onrender.com/health`
- `https://helpful-homebuyers-scraper.onrender.com/health`

Or upgrade to Render Starter ($7/mo) for always-on.

---

## How to Redeploy (if needed)

### Option A: Push to GitHub (preferred)
```bash
cd /Users/jeffbord/claude/claude-ghl-retell-mcp/webhook-server
git add -A
git commit -m "Update webhook server"
git push origin main
```
Render auto-deploys on push to `main`.

### Option B: Manual deploy via Render API
```bash
# Get service ID first
curl -H "Authorization: Bearer rnd_a2JbvoxGO30HOT5XyoB8I5OuqYoX" \
  https://api.render.com/v1/services | jq '.[] | select(.service.name=="helpful-homebuyers-webhooks") | .service.id'

# Then trigger deploy
curl -X POST -H "Authorization: Bearer rnd_a2JbvoxGO30HOT5XyoB8I5OuqYoX" \
  https://api.render.com/v1/services/{SERVICE_ID}/deploys \
  -H "Content-Type: application/json" -d '{}'
```

### Option C: Render dashboard
1. Go to https://dashboard.render.com
2. Click `helpful-homebuyers-webhooks`
3. Click "Manual Deploy" → "Deploy latest commit"

---

## Repository Mismatch Note

The local code lives at `/Users/jeffbord/claude/claude-ghl-retell-mcp/webhook-server/`
but the Render service pulls from `https://github.com/ibuysqft/helpful-homebuyers-webhooks`
(separate repo from the parent `claude-ghl-retell-mcp` repo).

If local changes are not reflected on the live service, push them to `ibuysqft/helpful-homebuyers-webhooks`.

---

## Dependencies (requirements.txt)

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
requests==2.32.3
python-dotenv==1.0.1
```

No additional system packages needed. Python 3.x runtime on Render handles all of these.
