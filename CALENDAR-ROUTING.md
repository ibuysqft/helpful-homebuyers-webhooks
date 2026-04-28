# Appointment Calendar Routing

This webhook server now resolves appointment calendars through a routing layer instead of hardcoding every booking to a single `CALENDAR_ID`.

## Live Production Mode

Helpful Home Buyers USA is currently configured for a single owner:

```bash
CALENDAR_ROUTING_MODE=fixed_owner
CALENDAR_FIXED_OWNER_ID=2xJXutj4eTskFPYx8AeL
CALENDAR_FIXED_OWNER_KEY=jeffrey_bord
CALENDAR_FIXED_OWNER_NAME=Jeffrey Bord
```

That keeps all live appointment checks and bookings on Jeffrey Bord's personal calendar.

## Why This Exists

The routing layer makes two things possible without changing the public routes:

1. Keep Jeffrey as the default owner today.
2. Move to a clean round-robin model later without rewriting the Retell tool URLs.

## Current Route Behavior

The routes stay the same:

- `POST /{agent}-check-calendar`
- `POST /{agent}-book-appointment`

They now accept optional routing hints:

- `contact_id`
- `routing_mode`
- `routing_owner_key`
- `calendar_id`

They also return routing metadata:

- `calendar_id`
- `calendar_owner_key`
- `calendar_owner_name`
- `routing_mode`
- `routing_reason`

## Future Round Robin Setup

When multiple reps are ready, add a JSON rep pool and switch the mode:

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
    "calendar_id": "replace-with-harper-calendar-id",
    "location_id": "your-ghl-location-id",
    "active": true
  }
]'
```

## Safe Booking Pattern

For multi-rep mode, use the same route pair but pin the booking to the selected rep:

1. Call `/{agent}-check-calendar`
2. Read `calendar_id` and `calendar_owner_key` from the response
3. Pass those same fields into `/{agent}-book-appointment`

That avoids checking one rep's availability and then accidentally booking another rep.

## Verification

Check for an available slot:

```bash
python webhook-server/scripts/verify_jeffrey_calendar.py --days 14
```

Book the first open slot the script finds:

```bash
python webhook-server/scripts/verify_jeffrey_calendar.py --days 14 --book
```

Force a specific routed owner during tests:

```bash
python webhook-server/scripts/verify_jeffrey_calendar.py \
  --routing-mode round_robin \
  --routing-owner-key harper \
  --days 14
```
