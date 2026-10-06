import asyncio
import logging
import os
import threading
import time
from collections import defaultdict
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer

import feedparser
import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("football-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "")
API_BASE = "https://api.football-data.org/v4"

# Short code -> football-data.org competition code (free tier)
LEAGUES = {
    "pl": ("PL", "Premier League"),
    "laliga": ("PD", "La Liga"),
    "seriea": ("SA", "Serie A"),
    "bundesliga": ("BL1", "Bundesliga"),
    "ligue1": ("FL1", "Ligue 1"),
    "ucl": ("CL", "Champions League"),
}

# News sources (RSS). Swap or add feeds as you like.
NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.theguardian.com/football/rss",
]
TRANSFER_WORDS = (
    "transfer", "signing", "signs", "sign ", "loan", "bid", "deal",
    "contract", "move", "join", "agree", "fee", "release clause",
)

# ---------- tiny TTL cache so we don't hammer APIs ----------
_cache: dict = {}


def cache_get(key, ttl):
    item = _cache.get(key)
    if item and time.time() - item[0] < ttl:
        return item[1]
    return None


def cache_set(key, value):
    _cache[key] = (time.time(), value)
    return value


# ---------- data helpers ----------
async def api_get(path: str, params: dict | None = None, ttl: int = 60):
    key = ("api", path, tuple(sorted((params or {}).items())))
    hit = cache_get(key, ttl)
    if hit is not None:
        return hit
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{API_BASE}{path}",
            params=params,
            headers={"X-Auth-Token": FOOTBALL_API_KEY},
        )
    if r.status_code == 429:
        raise RuntimeError("Rate limit reached. Try again in a minute.")
    if r.status_code in (401, 403):
        raise RuntimeError("API key missing or not allowed for this data.")
    r.raise_for_status()
    return cache_set(key, r.json())


def _parse_feeds():
    entries = []
    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            entries.extend(feed.entries)
        except Exception as e:  # noqa: BLE001
            log.warning("Feed failed %s: %s", url, e)
    entries.sort(key=lambda e: e.get("published_parsed") or time.gmtime(0), reverse=True)
    return entries


async def get_news():
    hit = cache_get("news", 300)
    if hit is not None:
        return hit
    entries = await asyncio.to_thread(_parse_feeds)
    return cache_set("news", entries)


def fmt_links(entries, limit=8):
    lines = []
    for e in entries[:limit]:
        title = escape(e.get("title", "Untitled"))
        link = e.get("link", "")
        lines.append(f'• <a href="{link}">{title}</a>')
    return "\n".join(lines) or "Nothing found right now."


def fmt_time(iso: str) -> str:
    # "2026-10-06T19:00:00Z" -> "10-06 19:00 UTC"
    return f"{iso[5:10]} {iso[11:16]} UTC"


# ---------- command handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ <b>Football News Bot</b>\n\n"
        "Latest news, scores, fixtures, tables and transfers.\n\n"
        "/news – latest headlines\n"
        "/scores – today's matches\n"
        "/fixtures &lt;league&gt; – upcoming games\n"
        "/table &lt;league&gt; – league standings\n"
        "/transfers – transfer news\n"
        "/leagues – available league codes",
        parse_mode=ParseMode.HTML,
    )


async def leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"<code>{k}</code> – {v[1]}" for k, v in LEAGUES.items()]
    await update.message.reply_text(
        "🏆 <b>League codes</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = await get_news()
    await update.message.reply_text(
        "📰 <b>Latest football news</b>\n\n" + fmt_links(entries),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def transfers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = await get_news()
    picked = [
        e for e in entries
        if any(w in (e.get("title", "") + " " + e.get("summary", "")).lower()
               for w in TRANSFER_WORDS)
    ]
    await update.message.reply_text(
        "🔁 <b>Transfer news</b>\n\n" + fmt_links(picked),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def scores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_get("/matches", ttl=30)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"⚠️ {e}")
        return
    matches = data.get("matches", [])
    if not matches:
        await update.message.reply_text("No matches today in the covered leagues.")
        return

    grouped = defaultdict(list)
    for m in matches:
        grouped[m["competition"]["name"]].append(m)

    out = ["⚽ <b>Today's matches</b>"]
    for comp, ms in grouped.items():
        out.append(f"\n🏆 <b>{escape(comp)}</b>")
        for m in ms:
            home = escape(m["homeTeam"].get("shortName") or m["homeTeam"]["name"])
            away = escape(m["awayTeam"].get("shortName") or m["awayTeam"]["name"])
            status = m["status"]
            ft = m["score"]["fullTime"]
            if status in ("SCHEDULED", "TIMED"):
                out.append(f"🕒 {m['utcDate'][11:16]} UTC  {home} vs {away}")
            else:
                tag = {"IN_PLAY": "🔴 LIVE", "PAUSED": "⏸ HT", "FINISHED": "✅ FT"}.get(
                    status, status
                )
                out.append(f"{tag}  {home} {ft['home']}–{ft['away']} {away}")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


def _league_from_args(context):
    code = (context.args[0].lower() if context.args else "pl")
    return code, LEAGUES.get(code)


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, league = _league_from_args(context)
    if not league:
        await update.message.reply_text("Unknown league. Send /leagues to see codes.")
        return
    try:
        data = await api_get(
            f"/competitions/{league[0]}/matches",
            params={"status": "SCHEDULED"},
            ttl=300,
        )
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"⚠️ {e}")
        return
    ms = data.get("matches", [])[:10]
    if not ms:
        await update.message.reply_text("No upcoming fixtures found.")
        return
    out = [f"📅 <b>{league[1]} – upcoming</b>\n"]
    for m in ms:
        home = escape(m["homeTeam"].get("shortName") or m["homeTeam"]["name"])
        away = escape(m["awayTeam"].get("shortName") or m["awayTeam"]["name"])
        out.append(f"{fmt_time(m['utcDate'])}  {home} vs {away}")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


async def table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, league = _league_from_args(context)
    if not league:
        await update.message.reply_text("Unknown league. Send /leagues to see codes.")
        return
    try:
        data = await api_get(f"/competitions/{league[0]}/standings", ttl=600)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"⚠️ {e}")
        return
    rows = data["standings"][0]["table"]
    lines = [f"{'#':>2} {'Team':<14} {'P':>2} {'GD':>3} {'Pts':>3}"]
    for r in rows:
        name = (r["team"].get("shortName") or r["team"]["name"])[:14]
        lines.append(
            f"{r['position']:>2} {name:<14} {r['playedGames']:>2} "
            f"{r['goalDifference']:>3} {r['points']:>3}"
        )
    await update.message.reply_text(
        f"📊 <b>{league[1]}</b>\n<pre>{escape(chr(10).join(lines))}</pre>",
        parse_mode=ParseMode.HTML,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


# ---------- tiny health server (lets Render's Web Service stay "healthy") ----------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server on port %s", port)


def main():
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("leagues", leagues))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("transfers", transfers))
    app.add_handler(CommandHandler("scores", scores))
    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("table", table))
    app.add_error_handler(on_error)
    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
