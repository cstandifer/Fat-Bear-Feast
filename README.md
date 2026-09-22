# Fat Bear Feast

A Hungry Hungry Hippos–style party game for Fat Bear Week. Up to four bears per table
(black bear, grizzly, panda, polar bear) lunge for salmon in a river pool. Everyone plays
on their own phone or computer; empty seats can be filled by computer bears.

It's two files — `server.py` (the game server, Python standard library only) and
`index.html` (the page players open).

## Play on your home Wi‑Fi

```bash
python3 server.py
```

Then open the address it prints on each device.

## Put it online (free, on Render)

1. **GitHub:** create a new repository (it can be public or private) and upload
   `server.py`, `index.html`, `render.yaml`, `requirements.txt` and this README
   (Add file → Upload files → Commit).
2. **Render:** sign in at render.com with your GitHub account, choose
   **New → Blueprint**, pick the repository, and click **Apply**. Render reads
   `render.yaml` and starts the game.
3. When it's live, Render shows an address like `https://fat-bear-feast.onrender.com`.
   Share that. Each table in the waiting room has its own link and QR code.

Notes on Render's free plan: the game goes to sleep after about 15 minutes with nobody
playing, and the first visit afterwards takes up to a minute to wake it. Any change you
upload to GitHub redeploys automatically.
