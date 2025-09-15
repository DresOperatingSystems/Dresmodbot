# Dresmodbot
A security hardened telegram group moderation bot that has a web search feature leveraging DuckDuckGo search engine

(SECURITY HARDENED FEATURES)
Privacy-aware HTTP session: all outbound DuckDuckGo requests use a custom requests.Session that:
Sends Do-Not-Track and a minimal, fixed User-Agent header.

Injects a static FAKE_IP into the X-Forwarded-For header to avoid exposing the host IP.

Removes Cookie headers on outbound requests and strips Set-Cookie/Set-Cookie2 from responses via a custom HTTPAdapter to prevent cookie-based tracking.

Uses a retry policy (3 retries, exponential backoff) and restricts retries to safe methods to improve reliability without enabling unsafe resubmissions.

IP disclosure blocking: queries that appear to ask for IP address information are detected via a regex and explicitly refused.

Command authorization: sensitive commands (blacklist management, moderation actions, welcome config) check bot ownership/admin status before executing.

User blacklist: a runtime blacklist prevents specified user IDs from using the bot.

Minimal external footprint: search requests are limited (timeout, limited fields read from API response) to reduce data exposure and resource usage.

Safe defaults: timeouts and error handling on external calls prevent hangs and return safe, generic error messages on failure rather than leaking internal errors.

(FEATURES)
Moderation commands: kick, ban, unban, mute, unmute, warn.

Blacklist management: /blacklist, /unblacklist, /list_blacklist (owner-only).

DuckDuckGo-powered search via /search with privacy-preserving HTTP session and IP-query filtering.

Welcome message configuration: /setwelcome and /clearwelcome (owner/admin).

Basic help and /start command support.

Simple file-backed store for welcome messages (STORE_FILE).
GETTING STARTED:::::

First please go to @BotFather over on telegram and create a bot taking down the api key to then add into the scripts.

Secondly go into your terminal then copy and paste the steps below.

git clone https://<i></i>github.com/DresOperatingSystems/Dresmodbot

cd Dresmodbot

pip install -r requirements.txt

nano Dresbot.py (change the api token and admin id)

python Dresbot.py

Optionally you can create a venv env then run the bot with nohup & to successfully host it from your machine also if you run your machine through a private dns server like qaud9 along with TOR and randomising the mac address it encrypts the backend even more

Thank you for checking this project out for any queries please contact the DresOS team below via telegram or email

Telegram: https://t.me/dresossupport
Email: DresOS@tutamail.com

Thank you 

The DresOS team
