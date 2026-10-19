# Blogger Auto-Blog Agent

This project publishes **exactly one blog post per day** on your Blogger blog, automatically.

It runs once a day at **08:00 IST**, and it does the whole job by itself:

1. **Groq** picks a fresh topic (it reads your last 50 post titles first, so it never repeats itself).
2. **Groq** writes a full 1000-1500 word article as clean HTML.
3. **Pixabay** finds three related photos and they are placed inside the article.
4. **Blogger API v3** publishes the post (or saves it as a draft).

You never have to write anything by hand after the first setup.

---

## Table of contents

1. [How it works](#how-it-works)
2. [What you need before you start](#what-you-need-before-you-start)
3. [Step A - Get the Groq key](#step-a---get-the-groq-key)
4. [Step B - Get the Pixabay key](#step-b---get-the-pixabay-key)
5. [Step C - Set up Google Cloud, Blogger API and the refresh token](#step-c---set-up-google-cloud-blogger-api-and-the-refresh-token)
6. [Step D - Find your Blog ID](#step-d---find-your-blog-id)
7. [Step E - Create your .env file](#step-e---create-your-env-file)
8. [Step F - Test it](#step-f---test-it)
9. [Step G - Run it on GitHub Actions every day](#step-g---run-it-on-github-actions-every-day)
10. [Alternative - Windows Task Scheduler](#alternative---windows-task-scheduler)
11. [All settings](#all-settings)
12. [Safety features](#safety-features)
13. [Cost](#cost)
14. [Troubleshooting](#troubleshooting)
15. [Note about Pixabay images](#note-about-pixabay-images)

---

## How it works

```
                    ┌──────────────────────────────┐
                    │  GitHub Actions  02:30 UTC  │  (or Windows Task Scheduler)
                    │          08:00 IST           │
                    └───────────────┬──────────────┘
                                    │
                          python -m src.main
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
  ┌───────────┐             ┌─────────────┐             ┌─────────────┐
  │  Blogger  │  last 50    │    Groq     │             │  Pixabay    │
  │  API v3   │────titles──▶│  topic JSON │             │  3 photos   │
  │           │             └──────┬──────┘             └──────┬──────┘
  │ posts.list│                    │  article HTML             │
  │ posts.    │◀───────────────────┴──────────────┐            │
  │  insert   │        compose.py merges them    │            │
  └───────────┘                                  ▼            ▼
        ▲                                  final HTML + 3 images
        │                                            │
        └──────────────── one post, once a day ─────┘
```

### Project files

| File | What it does |
|---|---|
| `src/config.py` | Reads `.env`, checks every key is present, sets up logging with IST timestamps |
| `src/auth.py` | Google OAuth: turns the stored refresh token into a valid access token, gives you the Blogger service |
| `src/groq_client.py` | Picks the topic and writes the article (with retries and quality checks) |
| `src/pixabay_client.py` | Searches Pixabay and returns three different photos |
| `src/blogger_client.py` | Reads your post history, checks "did we already post today?", publishes the post |
| `src/composer.py` | Puts the images into the article and builds the final HTML |
| `src/main.py` | Runs everything in order (the whole pipeline) |
| `scripts/get_refresh_token.py` | Run once by hand to get the Google refresh token |
| `.github/workflows/daily-post.yml` | The daily schedule on GitHub Actions |

---

## What you need before you start

- Python 3.11 or newer (check with `python --version`)
- A Blogger blog
- A Google account that owns that blog
- About 30 minutes for the one-time setup

---

## Step A - Get the Groq key

Groq writes the topic and the article. It has a free tier.

1. Go to <https://console.groq.com/keys>
2. Sign up (a Google or GitHub account works).
3. Click **Create API Key**, pick a name like `blogger-agent`, and copy the key.

Put it in your `.env` as `GROQ_API_KEY=`.

**About the model:** `llama-3.3-70b-versatile` was switched off by Groq on 2026-08-16, so the
default here is `openai/gpt-oss-120b` (Groq's own recommendation). You can change it any time
with the `GROQ_MODEL` setting - the newest list is always at
<https://api.groq.com/openai/v1/models>.

---

## Step B - Get the Pixabay key

Pixabay gives you free stock photos.

1. Go to <https://pixabay.com/api/docs/>
2. Click **Get a free API key**.
3. Sign up and copy the key.

Put it in your `.env` as `PIXABAY_API_KEY=`.

---

## Step C - Set up Google Cloud, Blogger API and the refresh token

This is the part that sounds hard. Do it slowly, in this order.

### C1. Create a Google Cloud project

1. Open <https://console.cloud.google.com/>
2. Click the project picker at the top and choose **New Project**.
3. Name it `blogger-agent` and click **Create**.
4. Make sure the new project is selected at the top of the screen.

### C2. Turn on the Blogger API

1. With that project selected, go to **APIs & Services -> Library**.
2. Search for **Blogger API**.
3. Click it, then click **Enable**.

### C3. Set up the OAuth consent screen

1. Go to **APIs & Services -> OAuth consent screen**.
2. Choose **External** (unless the Google account is a Workspace account, then pick the Workspace option).
3. Fill in your app name and support email, then click **Save and Continue**.
4. Skip the "Scopes" and "Test users" pages (we only add a scope later, in code).
5. Click **Publish**.

> **Important:** on the consent screen the status must be **"In production"**.
> While it is in **"Testing"** mode, Google deletes your refresh token after **7 days** and the
> agent stops working with the error `invalid_grant`. The "Publish" button is what stops that.

### C4. Create the OAuth client (Desktop app)

1. Go to **APIs & Services -> Credentials**.
2. Click **Create Credentials -> OAuth client ID**.
3. Application type: **Desktop app**.
4. Name it `blogger-agent-local`.
5. Click **Create**.
6. Click **Download JSON** and save the file as `client_secret.json` in this project folder.

### C5. Get the refresh token

Open a terminal **in this project folder** and run:

```bash
python scripts/get_refresh_token.py
```

What happens: your browser opens, you sign in with the Google account that owns your blog,
you click **Allow**, and the script prints three lines:

```
GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxx
GOOGLE_REFRESH_TOKEN=1//0eXXXXXXXXXXXXXXXX
```

Copy those three lines into your `.env`. You only do this once. If it ever stops working,
run the script again to get a fresh refresh token.

---

## Step D - Find your Blog ID

1. Open <https://www.googleapis.com/blogger/v3/users/self/blogs>
2. Sign in with the Google account that owns your blog.
3. You get JSON like this:

```json
{
  "kind": "blogger#blog",
  "id": "1234567890123456789",
  "name": "My Blog",
  "url": "https://myblog.blogspot.com/"
}
```

The long number next to `"id"` is your `BLOG_ID`.

---

## Step E - Create your .env file

```bash
cp .env.example .env      # Windows PowerShell:  Copy-Item .env.example .env
notepad .env              # Windows     |  nano .env   (Linux/macOS)
```

Fill in all seven values:

```ini
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-120b
PIXABAY_API_KEY=...
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=1//0e...
BLOG_ID=1234567890123456789
BLOG_NICHE=Technology and AI tools for beginners
BLOG_LANGUAGE=English
PUBLISH_AS_DRAFT=true
```

The `.env` file is in `.gitignore`, so it will never be pushed to GitHub. That is what we want.

---

## Step F - Test it

### F1. Set up Python

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux / macOS
pip install -r requirements.txt
```

### F2. Dry run (publishes nothing)

```bash
python -m src.main --dry-run
```

This generates a real topic, a real article and real images, prints the final HTML to the
console, and saves it to `output/preview.html`. Nothing is published.

Open `output/preview.html` in your browser to read the post as your readers will see it.

### F3. First real test as a draft

```bash
# Windows PowerShell
$env:PUBLISH_AS_DRAFT="true"; python -m src.main

# Linux / macOS
PUBLISH_AS_DRAFT=true python -m src.main
```

The post is created in your Blogger dashboard under **Drafts**. Read it, check the images and
the formatting, then edit or delete it if you do not like it.

### F4. Go live

Change `PUBLISH_AS_DRAFT=false` in `.env` (or remove the line - the safe default is `true`).
Now the daily post is published straight away.

Useful extra flags:

| Flag | Meaning |
|---|---|
| `--dry-run` | Generate everything, publish nothing |
| `--force` | Ignore the "one post per day" check (testing only) |
| `--log-level DEBUG` | Show every detail, useful when something fails |

---

## Step G - Run it on GitHub Actions every day

### G1. Push the code

```bash
git init
git add .
git commit -m "Blogger auto-blog agent"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/blogger-agent.git
git push -u origin main
```

### G2. Add the secrets

Open the repository on GitHub, then
**Settings -> Secrets and variables -> Actions -> New repository secret**.
Add these seven secrets (the green **New repository secret** button, not the tab):

| Secret | Where it came from |
|---|---|
| `GROQ_API_KEY` | Step A |
| `PIXABAY_API_KEY` | Step B |
| `GOOGLE_CLIENT_ID` | Step C5 |
| `GOOGLE_CLIENT_SECRET` | Step C5 |
| `GOOGLE_REFRESH_TOKEN` | Step C5 |
| `BLOG_ID` | Step D |

GitHub encrypts them. They are never shown again after you save them, so keep your own `.env` safe.

### G3. Add the variables (not secret)

Go to **Settings -> Secrets and variables -> Actions -> Variables tab** and add:

| Variable | Example | If you skip it |
|---|---|---|
| `BLOG_NICHE` | `Technology and AI tools for beginners` | Uses the default value |
| `BLOG_LANGUAGE` | `English` | `English` |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | `openai/gpt-oss-120b` |
| `PUBLISH_AS_DRAFT` | `true` | `true` (safe - posts stay as drafts) |

### G4. Turn the schedule on

The schedule is already in `.github/workflows/daily-post.yml`:

```yaml
on:
  schedule:
    - cron: "30 2 * * *"   # 08:00 IST
  workflow_dispatch:       # lets you run it by hand
```

Once the workflow is in your repository's default branch, GitHub runs it automatically every day
at 02:30 UTC = **08:00 IST**.

Test it by hand first: **Actions -> Daily Blog Post -> Run workflow**. The `dry_run` box is on by
default, so your first manual run only shows you the preview (download it as an artifact).

Turn on live posting by setting the variable `PUBLISH_AS_DRAFT` to `false`.

> GitHub may start scheduled runs up to about 15 minutes late, so expect the post between
> 08:00 and 08:15 IST. If you need an exact 08:00, use Windows Task Scheduler (next section)
> or a free cron service that calls the GitHub API.

---

## Alternative - Windows Task Scheduler

Good if you want the post to run at exactly 08:00 IST on your own computer.

1. Fill in `.env` and test `python -m src.main --dry-run` until it works.
2. Open **Task Scheduler** (`taskschd.msc`) as an admin.
3. **Create Task** (not "Create Basic Task") -> **General** tab:
   - Name: `Blogger Daily Post`
   - Select **Run whether user is logged on or not**.
   - Tick **Do not start a new instance** under Settings.
4. **Triggers** tab -> **New...**:
   - Start daily at **08:00**, recurrence **every 1 day**.
5. **Actions** tab -> **New...**:
   - Action: **Start a program**
   - Program/script: `C:\Users\YOUR-NAME\blogger-agent\venv\Scripts\python.exe`
   - Add arguments: `-m src.main`
   - Start in: `C:\Users\YOUR-NAME\blogger-agent`
6. **Conditions** tab: untick *Start only if the computer is on AC power*.
7. Click **OK**, then right-click the task and choose **Run** to test it.

A one-line equivalent if you prefer the `schtasks` command:

```bat
schtasks /Create /TN "Blogger Daily Post" /SC DAILY /ST 08:00 ^
  /TR "\"C:\Users\YOUR-NAME\blogger-agent\venv\Scripts\python.exe\" -m src.main" ^
  /RL LIMITED /F
```

Use `/RL HIGHEST` if the refresh token ever stops working (Windows sometimes protects credential files).

---

## All settings

Required (no default):

| Setting | Meaning |
|---|---|
| `GROQ_API_KEY` | Groq API key |
| `PIXABAY_API_KEY` | Pixabay API key |
| `GOOGLE_CLIENT_ID` | OAuth client id from the Desktop app |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | Refresh token from `get_refresh_token.py` |
| `BLOG_ID` | Your Blogger blog id |
| `BLOG_NICHE` | What your blog is about. The writer follows this closely |

Optional (safe defaults):

| Setting | Default | Meaning |
|---|---|---|
| `BLOG_LANGUAGE` | `English` | Language of every article |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Which Groq model writes |
| `GROQ_MAX_TOKENS` | `8192` | Output room per request |
| `PUBLISH_AS_DRAFT` | `true` | `true` = draft, `false` = publish live |
| `BLOG_IMAGE_COUNT` | `3` | How many photos per post |
| `RECENT_TITLES_LIMIT` | `50` | How many old titles Groq sees |
| `HTTP_TIMEOUT_SECONDS` | `60` | Timeout for API calls |
| `MAX_ATTEMPTS` | `3` | Retries per API call |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows everything |

---

## Safety features

- **One post per day, guaranteed.** Before publishing, the agent asks Blogger whether a post was
  already created today in IST. If yes, it stops and exits. It never posts twice.
- **Nothing secret is logged.** Keys are masked (`gsk_a***12`) in the logs and never printed.
- **Retries.** Groq, Pixabay and Blogger calls are retried up to 3 times with growing waits
  (2s, 4s, 8s) when the network or the servers are busy.
- **Quality checks on the article.** The agent rejects articles with an `<h1>`, markdown,
  missing image placeholders, fewer than 4 sections, or that are far too short, and asks for a
  rewrite.
- **Draft by default.** `PUBLISH_AS_DRAFT=true` means a new article waits for your review.
- **Nothing important is deleted.** The agent only ever adds posts.

---

## Cost

- **GitHub Actions:** free for public repositories. For private repositories you get a small free
  monthly minutes allowance, and this workflow uses about 1-2 minutes per day.
- **Groq:** the free developer tier is enough for one article per day.
- **Pixabay:** free.
- **Google Blogger API:** free, and the daily quota is far above one post per day.

---

## Troubleshooting

### `Missing required configuration`

The message lists every missing key and where to get it. Copy `.env.example` to `.env` and fill in
the values.

### `invalid_grant` / "Google rejected the refresh token"

Your refresh token is expired or revoked. This is the most common problem, and there are three
common causes:

1. The OAuth consent screen is still in **Testing** mode. Fix: set it to **In production**
   (Step C3), then run `python scripts/get_refresh_token.py` again.
2. The Google password changed, or the app was removed from
   <https://myaccount.google.com/connections>. Fix: run the script again.
3. The token was copied with extra spaces or a missing character. Fix: copy it again carefully.

### `403` - "insufficient permissions" or "insufficient scope"

The token does not have the Blogger scope. Fix: run `python scripts/get_refresh_token.py` again
and sign in with the Google account that owns the blog.

### `404` on the blog, or "Blogger does not know this blog id"

`BLOG_ID` is wrong, or it belongs to a different Google account than the one you signed in with.
Check it at <https://www.googleapis.com/blogger/v3/users/self/blogs>.

### `HTTP 404` from Groq: "model does not exist"

Groq switched off `llama-3.3-70b-versatile` on 2026-08-16. Set `GROQ_MODEL=openai/gpt-oss-120b`
(or any model from <https://api.groq.com/openai/v1/models>).

### `HTTP 429` from Groq or Pixabay

You hit a rate or daily limit. The agent already retries with growing waits. If it keeps
failing, wait for the limit to reset or use another account. Pixabay's free tier allows about
100 requests per hour and 20,000 per month - this project uses one request per day.

### "Groq returned an empty answer"

The model used its whole token budget thinking. Raise `GROQ_MAX_TOKENS` (for example `12288`).

### No images in the article

Pixabay had no photos for those keywords. The agent then tries the next keyword and finally the
niche words. If all fail, the article is still published, just without photos.

### The article is short or has a weird heading

Check `output/preview.html`. The agent logs `Minor notes:` when the article is slightly outside
the target length. Raising the temperature or changing `GROQ_MODEL` usually helps.

### GitHub Actions did not run at 08:00

- Scheduled runs only work from the **default branch** of the repository.
- Check the **Actions** tab: a failed run shows the exact error.
- GitHub can delay scheduled runs by up to ~15 minutes.
- Forks do not run scheduled workflows at all (GitHub disables the schedule on forks).
- Add the secrets again if the repository was renamed or transferred.

---

## Note about Pixabay images

This project hotlinks Pixabay photos (`webformatURL`) directly from Pixabay's servers, as the
Pixabay API is meant to be used. Pixabay asks that you **do not hotlink permanently** and that
you keep traffic on their pages.

If your blog grows and you want full control, the recommended upgrade is to **self-host the
images**: download the three files during the run (Pixabay returns the file URLs), save them
where you host static files, and upload them to Blogger through
`posts.insert(mediaUpload=...)` so Blogger serves the image itself.

That change only touches `src/pixabay_client.py` and the `<figure>` block in
`src/composer.py`, so it is a small, contained upgrade when you want it.

---

## Project layout

```
blogger-agent/
├── src/
│   ├── config.py
│   ├── auth.py
│   ├── groq_client.py
│   ├── pixabay_client.py
│   ├── blogger_client.py
│   ├── composer.py
│   └── main.py
├── scripts/
│   └── get_refresh_token.py
├── .github/workflows/daily-post.yml
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

`output/` and `client_secret.json` are ignored by git on purpose: one is generated, the other is
a secret.