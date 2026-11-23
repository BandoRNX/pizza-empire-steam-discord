"""Poste les nouvelles évaluations Steam de Pizza Empire dans un salon Discord.

Même principe que sync_steam.py : lancé toutes les 15 minutes par
GitHub Actions, il retient dans reviews_state.json les évaluations déjà
postées et n'envoie que les nouvelles, de la plus ancienne à la plus récente.

Source principale : la page publique des évaluations sur steamcommunity.com
(elle donne le pseudo et l'avatar). Secours : l'API JSON appreviews.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_ID = 3248550
STATE_FILE = Path("reviews_state.json")
STEAMID64_BASE = 76561197960265728
MAX_PAGES = 30            # 10 évaluations par page, les plus récentes d'abord
BATCH_SIZE = 10           # évaluations postées au maximum par passage
TEXT_LIMIT = 1500         # au-delà, le texte est coupé avec un lien vers l'avis
ATTEMPTS = 3
USER_AGENT = "PizzaEmpire-Steam-Discord/1.0"

COMMUNITY_URL = (
    "https://steamcommunity.com/app/{app}/reviews/"
    "?browsefilter=mostrecent&filterLanguage=all&l=english"
)
# Pages suivantes : Steam ignore « p= » sur /reviews/ et renvoie toujours la
# 1re page. La suite se charge par /homecontent/ avec le curseur caché
# (userreviewscursor, etc.) présent dans le formulaire de chaque page.
MORE_URL = "https://steamcommunity.com/app/{app}/homecontent/?{query}"
_HIDDEN_INPUT = re.compile(r'<input type="hidden" name="(\w+)" value="([^"]*)"')
API_URL = (
    "https://store.steampowered.com/appreviews/{app}"
    "?json=1&filter=recent&language=all&purchase_type=all"
    "&num_per_page=100&cursor=*"
)

# Même nom et même avatar que les annonces : un seul « bot » visible sur le serveur.
BOT_NAME = "Pizza Empire Steam News"
BOT_AVATAR = (
    "https://raw.githubusercontent.com/BandoRNX/pizza-empire-steam-discord/main/"
    "assets/avatar.png"
)

COLOR_UP = 0x5BA32B
COLOR_DOWN = 0xC0392B


def get_webhook():
    return os.environ.get("DISCORD_REVIEWS_WEBHOOK_URL", "").strip()


# ---------------------------------------------------------------------------
# Lecture des évaluations
# ---------------------------------------------------------------------------

def http_get(url):
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.geturl(), response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
            print(f"Steam : échec de la requête ({attempt}/{ATTEMPTS}) : {error}")
            time.sleep(5 * attempt)
    raise last


def html_to_text(fragment):
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _first(pattern, text, default=""):
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else default


def parse_community_page(page_html):
    """Extrait les évaluations d'une page steamcommunity.com."""
    reviews = []
    for card in page_html.split('class="apphub_Card ')[1:]:
        account = _first(r'data-miniprofile="(\d+)"', card)
        if not account:
            continue

        body = _first(
            r'<div class="apphub_CardTextContent">(.*?)</div>\s*</div>\s*'
            r'<div class="UserReviewCardContent_Footer">',
            card,
        )
        posted = _first(r'<div class="date_posted">(.*?)</div>', body)
        early = "early_access_review" in body
        body = re.sub(r'<div class="date_posted">.*?</div>', "", body, flags=re.DOTALL)
        body = re.sub(r'<div class="early_access_review">.*?</div>', "", body, flags=re.DOTALL)
        body = re.sub(r'<div class="received_compensation">.*?</div>', "", body, flags=re.DOTALL)

        title = html_to_text(_first(r'<div class="title">(.*?)</div>', card))
        reviews.append({
            "id": str(STEAMID64_BASE + int(account)),
            "voted_up": title.lower().startswith("recommended"),
            "hours": html_to_text(_first(r'<div class="hours">(.*?)</div>', card)),
            "posted": html_to_text(posted).replace("Posted:", "").strip(),
            "early_access": early,
            "free": "received_compensation" in card,
            "text": html_to_text(body),
            "author": html_to_text(
                _first(r'apphub_CardContentAuthorName[^"]*"><a [^>]*>(.*?)</a>', card)
            ),
            "avatar": _first(r'appHubIconHolder[^"]*"><img src="([^"]+)"', card),
            "url": _first(r'data-modal-content-url="([^"]+)"', card),
        })
    return reviews


def next_page_query(page_html):
    """Paramètres de la page suivante, ou None s'il n'y en a plus."""
    fields = [(name, html.unescape(value)) for name, value in _HIDDEN_INPUT.findall(page_html)]
    if not any(name == "userreviewscursor" for name, _ in fields):
        return None
    return urllib.parse.urlencode(fields)


def fetch_community(seen=frozenset()):
    """Lit les pages de la plus récente à la plus ancienne et s'arrête sur une
    page qui ne contient plus rien de nouveau (ou après MAX_PAGES)."""
    found = {}
    url, page_html = http_get(COMMUNITY_URL.format(app=APP_ID))
    if "steamcommunity.com" not in url:
        raise RuntimeError(f"page communautaire redirigée vers {url}")

    for _ in range(MAX_PAGES):
        batch = parse_community_page(page_html)
        fresh = [review for review in batch if review["id"] not in found]
        if not fresh:
            break
        for review in fresh:
            found[review["id"]] = review
        if all(review["id"] in seen for review in fresh):
            break
        query = next_page_query(page_html)
        if not query:
            break
        time.sleep(1)
        url, page_html = http_get(MORE_URL.format(app=APP_ID, query=query))

    return list(found.values())


def fetch_api():
    url, body = http_get(API_URL.format(app=APP_ID))
    data = json.loads(body)
    if not data.get("success"):
        raise RuntimeError("l'API appreviews a répondu success=0")

    reviews = []
    for item in data.get("reviews", []):
        steamid = item["author"]["steamid"]
        minutes = item["author"].get("playtime_at_review", 0)
        reviews.append({
            "id": steamid,
            "voted_up": bool(item.get("voted_up")),
            "hours": f"{minutes / 60:.1f} hrs at review time",
            "posted": time.strftime("%d %B %Y", time.gmtime(item["timestamp_created"])),
            "early_access": bool(item.get("written_during_early_access")),
            "free": bool(item.get("received_for_free")),
            "text": item.get("review", "").strip(),
            "author": "",
            "avatar": "",
            "url": f"https://steamcommunity.com/profiles/{steamid}/recommended/{APP_ID}/",
        })
    return reviews


def fetch_reviews(seen=frozenset()):
    """Les plus récentes d'abord."""
    try:
        reviews = fetch_community(seen)
        print(f"Steam : {len(reviews)} évaluation(s) lue(s) sur la page communautaire")
        return reviews
    except Exception as error:  # noqa: BLE001 - on tente la source de secours
        print(f"Page communautaire indisponible ({error}), essai de l'API appreviews")
    reviews = fetch_api()
    print(f"Steam : {len(reviews)} évaluation(s) lue(s) par l'API")
    return reviews


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def build_embed(review):
    text = review["text"] or "*(no text)*"
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT].rsplit(" ", 1)[0] + f"…\n\n[Read the full review]({review['url']})"

    footer = [part for part in (
        review["hours"],
        "Early Access review" if review["early_access"] else "",
        "received the game for free" if review["free"] else "",
        f"posted {review['posted']}" if review["posted"] else "",
    ) if part]

    embed = {
        "title": ("👍 Recommended" if review["voted_up"] else "👎 Not Recommended"),
        "url": review["url"],
        "description": text,
        "color": COLOR_UP if review["voted_up"] else COLOR_DOWN,
        "footer": {"text": " · ".join(footer)},
    }
    if review["author"]:
        embed["author"] = {"name": review["author"]}
        if review["avatar"]:
            embed["author"]["icon_url"] = review["avatar"]
    return embed


def post_to_discord(embed):
    webhook = get_webhook()
    payload = json.dumps({
        "username": BOT_NAME,
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    separator = "&" if "?" in webhook else "?"

    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(
            webhook + separator + "wait=true",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                print(f"Discord : évaluation envoyée (HTTP {response.status})")
                return
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", "replace")
            if error.code == 429 and attempt < ATTEMPTS:
                try:
                    wait = float(json.loads(details).get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                print(f"Discord : limite de débit, attente de {wait:.1f}s")
                time.sleep(min(wait, 60))
                continue
            print(f"Discord a refusé le message : HTTP {error.code} {details[:500]}")
            raise


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("seen_ids", [])
    return state


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help="affiche les messages sans rien envoyer ni écrire dans reviews_state.json",
    )
    parser.add_argument(
        "--mark-seen", action="store_true",
        help="marque les évaluations actuelles comme déjà postées, sans rien envoyer",
    )
    args = parser.parse_args(argv)

    if args.mark_seen:
        state = load_state()
        seen = set(state["seen_ids"])
        reviews = fetch_reviews()
        state["seen_ids"].extend(r["id"] for r in reversed(reviews) if r["id"] not in seen)
        state["last_check"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        save_state(state)
        print(f"{len(state['seen_ids'])} évaluation(s) marquée(s) comme vues.")
        return 0

    if not args.dry_run and not get_webhook():
        print("DISCORD_REVIEWS_WEBHOOK_URL n'est pas défini : rien à faire.")
        return 0

    state = load_state()
    seen = set(state["seen_ids"])
    # Tant que tout l'historique n'a pas été posté (« complete »), on lit
    # toutes les pages ; ensuite on s'arrête à la première page déjà vue.
    reviews = fetch_reviews(seen if state.get("complete") else frozenset())
    new = [review for review in reversed(reviews) if review["id"] not in seen]
    print(f"{len(new)} nouvelle(s) évaluation(s)")

    for review in new[:BATCH_SIZE]:
        embed = build_embed(review)
        if args.dry_run:
            print(json.dumps(embed, ensure_ascii=False, indent=2))
            continue
        post_to_discord(embed)
        state["seen_ids"].append(review["id"])
        save_state(state)
        time.sleep(1)

    if not args.dry_run:
        if len(new) <= BATCH_SIZE:
            state["complete"] = True
        state["last_check"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
