# Wallet on the Run — setup

You'll do this once (~15 minutes). After that it runs itself. No coding.

You do **not** need to understand the code. You're just uploading files and flipping a few switches on GitHub.

---

## What you're setting up

- A free GitHub repo holds the files.
- A scheduled job checks the wallet's location **every 30 minutes** and saves it.
- A free web page shows the map + dispatch log, and refreshes itself.
- GitHub **emails you automatically if a run ever fails**, so it's never silently broken.

---

## Step 1 — Make a GitHub account
If you don't have one: go to https://github.com and sign up. Free plan is all you need.

## Step 2 — Create the repository
1. Click the **+** (top right) → **New repository**.
2. Name it something like `wallet-on-the-run`.
3. Choose **Public**. Click **Create repository**.

## Step 3 — Upload these files
1. Unzip the folder I gave you.
2. On the empty repo page, click **uploading an existing file**.
3. Drag **everything from inside the unzipped folder** into the box — including the
   `.github` folder (it holds the automation). Wait for all files to appear in the list.
4. Click **Commit changes**.

> If the `.github` folder won't drag in, that's the one thing to watch — the automation
> lives there. If it's missing, tell me and I'll give you a one-line alternative.

## Step 4 — Add your Tile login (kept secret)
1. In the repo: **Settings** → left sidebar **Secrets and variables** → **Actions**.
2. Click **New repository secret**. Add two, one at a time:
   - Name: `TILE_EMAIL` → Secret: your Tile account email
   - Name: `TILE_PASSWORD` → Secret: your Tile account password
3. These are encrypted. They are never shown in the code, the site, or to anyone (including me).

## Step 5 — Do the one-time history backfill (while Premium is active)
This pulls your existing ~167 points in one shot. **Do this before your Premium ends.**
1. Go to the **Actions** tab. If it asks, click to **enable workflows**.
2. Click **Track wallet** on the left → **Run workflow** (right side).
3. In the little dropdown, choose **backfill** → **Run workflow**.
4. Wait a few minutes, then refresh. A green check means it worked.
5. Open `wallet.json` in your repo — it should now have lots of entries.

## Step 6 — Turn on the website
1. **Settings** → **Pages**.
2. Under **Source**, pick **Deploy from a branch**.
3. Branch: **main**, folder: **/ (root)** → **Save**.
4. Wait 1–2 minutes. Your site is live at:
   `https://YOUR-USERNAME.github.io/wallet-on-the-run/`

That's it. From now on it updates every 30 minutes on its own.

---

## Good to know

**Premium ending is fine.** Backfill (Step 5) is the only part that needs Premium, and it's
one-time. The every-30-minutes updates use plain current-location, which does not need Premium.

**Privacy.** Because the repo is public, `wallet.json` and the map are publicly viewable.
For a wandering lost wallet that's usually harmless, but if you'd rather not publish exact
spots: go to **Settings → Secrets and variables → Actions → Variables** tab, add a variable
named `JITTER_METERS` with value `150`. That fuzzes each point by ~150 m. (Want the map fully
private instead? Ask me for the Netlify-from-private-repo version.)

**Pick the right tile.** The code grabs the tile whose name contains "wallet." If yours is
named something else, add a **Variable** named `TILE_NAME` with a word from its name.

---

## If something goes wrong

- **A run shows a red X / you got a failure email** — open the run, but honestly just send me
  a screenshot of the red step and I'll tell you the fix.
- **Login failed** — most likely two-factor auth on your Tile account, or a typo in the
  secrets. Re-check `TILE_EMAIL` / `TILE_PASSWORD` (no quotes, no spaces).
- **Backfill said it found 0 points** — open `history_raw.json` in the repo and send it to me.
  It means Tile's history format needs one small tweak to the parser, which I'll do for you.
- **Site only shows 5 stops** — the backfill hasn't added the rest yet; re-run Step 5.
