import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_ID = 3248550
STATE_FILE = Path("state.json")
BATCH_SIZE = 5
API_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
CLAN_IMAGE_BASE = "https://clan.akamai.steamstatic.com/images"

# Même nom et même avatar pour les annonces et les évaluations.
BOT_NAME = "Pizza Empire Steam News"
BOT_AVATAR = (
    "https://raw.githubusercontent.com/BandoRNX/pizza-empire-steam-discord/main/"
    "assets/avatar.png"
)

# Limites Discord : 4096 caractères par description, 6000 par message
# (titres, descriptions et footers cumulés), 10 embeds par message.
CHUNK_SIZE = 4000
MESSAGE_BUDGET = 5500

API_ATTEMPTS = 3
DISCORD_ATTEMPTS = 3
MAX_RETRY_WAIT = 60


def get_webhook():
    return os.environ["DISCORD_WEBHOOK_URL"].strip()


# ---------------------------------------------------------------------------
# BBCode Steam -> Markdown Discord
# ---------------------------------------------------------------------------

_LIST_TOKEN = re.compile(r"\s*(\[/?o?list\]|\[/?\*\])\s*", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>\]]*>")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)

# Balises BBCode sans équivalent Discord : on garde seulement leur contenu.
# Liste blanche, car "[Fix]" ou "[WIP]" doivent rester dans le texte.
_STRIP_TAGS = re.compile(
    r"\[/?(?:p|center|left|right|size|color|font|table|tr|td|th|"
    r"tableheader|tablecell|tablerow|noparse|dynamiclink|steamid)"
    r"(?:[= ][^\]]*)?\]",
    re.IGNORECASE,
)


def _resolve_image(url):
    url = url.strip()
    url = re.sub(r"\{STEAM_CLAN_(?:LOC_)?IMAGE\}", CLAN_IMAGE_BASE, url)
    return url if url.startswith(("http://", "https://")) else None


def _wrap(open_mark, close_mark=None):
    close_mark = close_mark or open_mark

    def repl(match):
        inner = match.group(1)
        stripped = inner.strip()

        if not stripped:
            return inner

        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{open_mark}{stripped}{close_mark}{trail}"

    return repl


def _convert_lists(text):
    stack = []  # un compteur par niveau : None = liste à puces, int = numérotée

    def repl(match):
        token = match.group(1).lower()

        # Fermeture de puce [/*] (anciennes annonces) : rien à afficher.
        if token == "[/*]":
            return ""

        if token in ("[list]", "[olist]"):
            stack.append(0 if token == "[olist]" else None)
            return ""

        if token in ("[/list]", "[/olist]"):
            if stack:
                stack.pop()
            return "" if stack else "\n\n"

        if not stack:
            return "\n- "

        indent = "  " * (len(stack) - 1)

        if stack[-1] is None:
            return f"\n{indent}- "

        stack[-1] += 1
        return f"\n{indent}{stack[-1]}. "

    return _LIST_TOKEN.sub(repl, text)


def _convert_quote(match):
    author = (match.group(1) or "").strip().strip("\"'")
    inner = match.group(2).strip()
    lines = [f"> {line}" if line.strip() else ">" for line in inner.splitlines()]

    if author:
        lines.insert(0, f"> **{author}**")

    return "\n" + "\n".join(lines) + "\n"


def _convert_url(match):
    target = match.group(1).strip().strip("\"'")
    label = match.group(2).strip()
    target = target.replace(")", "%29").replace(" ", "%20")
    return f"[{label}]({target})" if label else target


def bbcode_to_markdown(value):
    """Convertit le BBCode/HTML d'une annonce Steam en Markdown Discord.

    Retourne (texte, images) : `images` liste les URL des [img] trouvées,
    dans l'ordre.
    """
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    images = []

    # Blocs de code mis de côté pour que rien ne soit converti à l'intérieur.
    code_blocks = []

    def stash_code(match):
        code_blocks.append(match.group(1).strip("\n"))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = re.sub(
        r"\[code\](.*?)\[/code\]", stash_code, text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = _BR.sub("\n", text)
    text = _HTML_TAG.sub("", text)

    def grab_image(match):
        url = _resolve_image(match.group(1))

        if url and url not in images:
            images.append(url)

        return ""

    text = re.sub(
        r"\[img\](.*?)\[/img\]", grab_image, text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"\{STEAM_CLAN_(?:LOC_)?IMAGE\}\S*", "", text)

    for level in range(1, 7):
        marks = "#" * min(level, 3)
        text = re.sub(
            rf"\[h{level}\](.*?)\[/h{level}\]",
            lambda m, marks=marks: f"\n\n{marks} {' '.join(m.group(1).split())}\n",
            text, flags=re.IGNORECASE | re.DOTALL,
        )

    for tag, mark in (
        ("b", "**"), ("i", "*"), ("u", "__"),
        ("strike", "~~"), ("spoiler", "||"),
    ):
        text = re.sub(
            rf"\[{tag}\](.*?)\[/{tag}\]", _wrap(mark), text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    text = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]", _convert_url, text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[url\](.*?)\[/url\]", lambda m: m.group(1).strip(), text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[previewyoutube=([\w-]+)[^\]]*\]\s*\[/previewyoutube\]",
        lambda m: f"https://youtu.be/{m.group(1)}", text, flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\[quote(?:=([^\]]*))?\](.*?)\[/quote\]", _convert_quote, text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"\[hr\]\s*(?:\[/hr\])?", "\n──────────\n", text,
                  flags=re.IGNORECASE)

    text = _convert_lists(text)
    text = _STRIP_TAGS.sub("", text)

    text = html.unescape(text)

    for index, code in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{index}\x00", f"\n```\n{code}\n```\n")

    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text, images


def clean_title(value):
    text, _ = bbcode_to_markdown(value)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Découpage et construction des messages Discord
# ---------------------------------------------------------------------------

def split_text(text, limit=CHUNK_SIZE):
    """Coupe `text` en morceaux de `limit` caractères au plus, de préférence
    sur un saut de ligne, en gardant les blocs de code équilibrés."""
    chunks = []
    reopen = False

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)

        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)

        if cut <= 0:
            cut = limit

        chunk = text[:cut].rstrip()
        text = text[cut:].lstrip()

        if reopen:
            chunk = "```\n" + chunk

        reopen = chunk.count("```") % 2 == 1

        if reopen:
            chunk += "\n```"

        chunks.append(chunk)

    if reopen:
        text = "```\n" + text

    chunks.append(text)
    return chunks


def build_messages(item):
    """Retourne la liste des messages (chacun : liste d'embeds) à envoyer."""
    title = clean_title(item.get("title", "")) or "Steam announcement"
    body, images = bbcode_to_markdown(item.get("contents", ""))

    timestamp = time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime(int(item["date"]))
    )
    footer = {"text": f"Pizza Empire · Steam News · {timestamp}"}

    chunks = split_text(body) if body else [
        "Read the full announcement on Steam."
    ]

    embeds = []

    for index, chunk in enumerate(chunks):
        embed = {"description": chunk, "color": 0xE8A33D}

        if index == 0:
            embed["title"] = title[:256]
            embed["url"] = item.get("url", "")

            if images:
                embed["image"] = {"url": images[0]}

        if index == len(chunks) - 1:
            embed["footer"] = footer

        embeds.append(embed)

    messages = []
    current = []
    size = 0

    for embed in embeds:
        weight = (
            len(embed["description"])
            + len(embed.get("title", ""))
            + len(embed.get("footer", {}).get("text", ""))
        )

        if current and (size + weight > MESSAGE_BUDGET or len(current) >= 10):
            messages.append(current)
            current, size = [], 0

        current.append(embed)
        size += weight

    messages.append(current)
    return messages


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------

def api_news(enddate=None):
    params = {
        "appid": APP_ID,
        "count": 100,
        "maxlength": 0,
        "feeds": "steam_community_announcements",
    }

    if enddate is not None:
        params["enddate"] = enddate

    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PizzaEmpire-Steam-Discord/1.0"},
    )

    for attempt in range(1, API_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))

            return data.get("appnews", {}).get("newsitems", [])
        except urllib.error.HTTPError as error:
            retryable = error.code >= 500 or error.code == 429
            problem = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError) as error:
            retryable = True
            problem = str(getattr(error, "reason", error))

        if not retryable or attempt == API_ATTEMPTS:
            raise RuntimeError(f"API Steam indisponible : {problem}")

        wait = 2 ** attempt
        print(f"Steam : {problem}, nouvel essai dans {wait}s "
              f"({attempt}/{API_ATTEMPTS})")
        time.sleep(wait)


def fetch_all_news():
    """Récupère l'historique disponible, des plus récentes aux plus anciennes."""
    all_news = []
    seen = set()
    cursor = None

    while True:
        items = api_news(cursor)

        if not items:
            break

        new_items = []

        for item in items:
            gid = str(item.get("gid", ""))

            if gid and gid not in seen:
                seen.add(gid)
                new_items.append(item)

        if not new_items:
            break

        all_news.extend(new_items)

        oldest_date = min(int(item["date"]) for item in items)
        print(f"Historique récupéré : {len(all_news)} annonces")

        if len(items) < 100:
            break

        next_cursor = oldest_date - 1

        if cursor is not None and next_cursor >= cursor:
            break

        cursor = next_cursor
        time.sleep(0.3)

    all_news.sort(
        key=lambda item: (int(item["date"]), str(item["gid"]))
    )

    return all_news


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {
            "initialized": False,
            "seen_ids": [],
            "pending": [],
        }

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Attention : état de synchronisation illisible.")
        raise

    state.setdefault("initialized", False)
    state.setdefault("seen_ids", [])
    state.setdefault("pending", [])

    return state


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def enqueue(state, items):
    seen = set(state["seen_ids"])
    added = 0

    pending_ids = {
        str(item.get("gid", ""))
        for item in state["pending"]
    }

    for item in items:
        gid = str(item.get("gid", ""))

        if not gid or gid in seen or gid in pending_ids:
            continue

        state["pending"].append(item)
        state["seen_ids"].append(gid)
        seen.add(gid)
        pending_ids.add(gid)
        added += 1

    state["pending"].sort(
        key=lambda item: (int(item["date"]), str(item["gid"]))
    )

    return added


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_to_discord(embeds):
    payload = json.dumps({
        "username": BOT_NAME,
        "avatar_url": BOT_AVATAR,
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")

    # Le paramètre wait=true permet à Discord de confirmer
    # la réception du message.
    webhook = get_webhook()
    separator = "&" if "?" in webhook else "?"

    for attempt in range(1, DISCORD_ATTEMPTS + 1):
        request = urllib.request.Request(
            webhook + separator + "wait=true",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "PizzaEmpire-Steam-Discord/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                print(
                    f"Discord : message envoyé "
                    f"(HTTP {response.status})"
                )
                return
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")

            if error.code == 429 and attempt < DISCORD_ATTEMPTS:
                wait = _retry_after(error, details)
                print(f"Discord : limite de débit (429), attente de "
                      f"{wait:.1f}s ({attempt}/{DISCORD_ATTEMPTS})")
                time.sleep(wait)
                continue

            print(f"Discord a refusé le message : HTTP {error.code}")
            print(f"Détail Discord : {details[:1000]}")
            raise
        except urllib.error.URLError as error:
            print(f"Erreur réseau pendant l'envoi Discord : {error.reason}")
            raise


def _retry_after(error, details):
    wait = None

    try:
        wait = float(json.loads(details).get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        pass

    if wait is None:
        try:
            wait = float(error.headers.get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            wait = 5.0

    return min(max(wait, 0.0) + 0.5, MAX_RETRY_WAIT)


def print_dry_run(item, messages):
    print(f"--- [dry-run] {item.get('title', item.get('gid'))} "
          f"({len(messages)} message(s)) ---")

    for number, embeds in enumerate(messages, 1):
        print(f"[message {number}/{len(messages)}]")
        print(json.dumps(embeds, ensure_ascii=False, indent=2))


def publish(item, state, dry_run):
    """Envoie une annonce. La progression (messages déjà envoyés) est
    sauvegardée dans l'élément en file pour ne rien renvoyer en double
    si le run échoue entre deux messages."""
    messages = build_messages(item)

    if dry_run:
        print_dry_run(item, messages)
        return

    start = int(item.get("sent_parts", 0))

    for index in range(start, len(messages)):
        post_to_discord(messages[index])
        item["sent_parts"] = index + 1
        save_state(state)

        if index + 1 < len(messages):
            time.sleep(1)


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help="affiche les embeds sans rien envoyer ni écrire dans state.json",
    )
    parser.add_argument(
        "--mark-seen", action="store_true",
        help="marque tout l'historique comme déjà publié, sans rien envoyer",
    )
    args = parser.parse_args(argv)
    dry_run = args.dry_run

    state = load_state()

    if args.mark_seen:
        history = fetch_all_news()
        seen = set(state["seen_ids"])
        state["seen_ids"].extend(
            str(i["gid"]) for i in history if str(i["gid"]) not in seen
        )
        state["pending"] = []
        state["initialized"] = True
        state["last_check"] = time.strftime("%Y-%m-%d", time.gmtime())
        save_state(state)
        print(f"{len(state['seen_ids'])} annonce(s) marquée(s) comme publiées.")
        return

    # Garde le dépôt actif (GitHub désactive les crons après 60 jours sans
    # activité) : un commit par jour au plus, car la valeur ne change qu'une
    # fois par jour.
    state["last_check"] = time.strftime("%Y-%m-%d", time.gmtime())

    if not state["initialized"]:
        print("Première synchronisation : récupération de l'historique...")

        history = fetch_all_news()
        added = enqueue(state, history)
        state["initialized"] = True

        # Sauvegarder la file avant toute tentative d'envoi.
        if not dry_run:
            save_state(state)

        print(f"{added} annonces historiques ajoutées à la file.")

    else:
        print("Recherche des nouvelles annonces Steam...")

        latest = api_news()
        added = enqueue(state, latest)

        if added:
            print(f"{added} nouvelle(s) annonce(s) ajoutée(s).")

    batch = state["pending"][:BATCH_SIZE]

    if not batch:
        print("Aucune annonce en attente.")

        if not dry_run:
            save_state(state)

        return

    sent = 0

    for item in batch:
        publish(item, state, dry_run)

        # Retiré de la file et sauvegardé tout de suite : un échec sur
        # l'annonce suivante ne provoquera pas de doublon.
        if not dry_run:
            gid = str(item.get("gid", ""))
            state["pending"] = [
                pending for pending in state["pending"]
                if str(pending.get("gid", "")) != gid
            ]
            save_state(state)

        sent += 1
        print(f"Publié : {item.get('title', item.get('gid'))}")

        if not dry_run:
            time.sleep(2)

    print(
        f"Terminé : {sent} annonce(s) "
        f"{'simulée(s)' if dry_run else 'publiée(s)'}, "
        f"{len(state['pending']) - (sent if dry_run else 0)} en attente."
    )


if __name__ == "__main__":
    main()
