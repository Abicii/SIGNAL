# SIGNAL

A quirky little draft of an app that rewrites your week when life does.

SIGNAL is meant to be sensor agnostic. A late night, sore legs, a meeting that ate the evening, or a number from whatever device you already use can all count as a signal. This repository is an early sketch of that idea.

## Today

One session, a recovery check-in, and how the week is going.

![Today](docs/images/today.png)

## Life Replan

Something changed. SIGNAL shows the week before and after, why it moved, and a short line with a bit of attitude.

![Life Replan](docs/images/life-replan.png)

## Run it locally

You need Python, Node, and a PostgreSQL database.

```powershell
$env:DATABASE_URL = "postgresql+psycopg2://USER:PASSWORD@localhost:5432/signal"
python -m pip install -r requirements.txt
alembic upgrade head
python -m uvicorn app.main:app --reload
```

In another terminal:

```bash
cd apps/web
npm install
npm run dev
```

Open the URL Vite prints. The page talks to the API at `http://127.0.0.1:8000`. The first start loads one sample week so you can click around. There is no login in this draft.
