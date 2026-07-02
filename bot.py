import os
import re
import asyncio
import tempfile
import logging
import http.cookiejar
import base64
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import yt_dlp
import instaloader

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

ANY_LINK = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)[^\s]*",
    re.IGNORECASE,
)
POST_RE          = re.compile(r"instagram\.com/(?:p|reel|tv|r)/([A-Za-z0-9_-]+)", re.IGNORECASE)
STORY_RE         = re.compile(r"instagram\.com/stories/([^/]+)/(\d+)",              re.IGNORECASE)
HIGHLIGHT_RE     = re.compile(r"instagram\.com/stories/highlights/(\d+)",           re.IGNORECASE)
HIGHLIGHT_B64_RE = re.compile(r"instagram\.com/s/([A-Za-z0-9+/=_-]+)",             re.IGNORECASE)

MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mov"}

INSTAGRAM_HEADERS = {
    "User-Agent": "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; pt_BR; 453709483)",
    "X-IG-App-ID": "936619743392459",
    "X-IG-Capabilities": "3brTvw==",
    "Accept-Language": "pt-BR",
    "Accept": "*/*",
}

# ─── Sessão Instagram ──────────────────────────────────────────────────────────

_session = None

def get_session():
    global _session
    if _session is not None:
        return _session

    if not os.path.isfile(COOKIES_FILE):
        raise FileNotFoundError("cookies.txt não encontrado.")

    import requests
    s = requests.Session()
    s.headers.update(INSTAGRAM_HEADERS)

    jar = http.cookiejar.MozillaCookieJar()
    jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    for c in jar:
        if "instagram.com" in c.domain:
            s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)

    cookies = {c.name: c.value for c in jar if "instagram.com" in c.domain}
    required = ["sessionid", "csrftoken", "ds_user_id"]
    missing  = [k for k in required if k not in cookies]
    if missing:
        raise ValueError(f"Cookies faltando: {missing}. Exporte um novo cookies.txt.")

    s.headers["X-CSRFToken"] = cookies["csrftoken"]
    logger.info(f"Sessão Instagram criada (user_id={cookies['ds_user_id']})")
    _session = s
    return _session


def reset_session():
    global _session
    _session = None


def shortcode_to_mediaid(shortcode: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + alphabet.index(char)
    return str(media_id)


def decode_highlight_b64(encoded: str) -> int | None:
    try:
        encoded = encoded.replace("-", "+").replace("_", "/")
        pad = 4 - len(encoded) % 4
        if pad != 4:
            encoded += "=" * pad
        decoded = base64.b64decode(encoded).decode("utf-8")
        m = re.search(r"highlight:(\d+)", decoded)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def collect_files(directory: str) -> list[str]:
    return sorted(
        str(f) for f in Path(directory).rglob("*")
        if f.suffix.lower() in MEDIA_EXTS and f.is_file()
    )


def download_file(session, url: str, dest: str):
    r = session.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1024 * 256):
            f.write(chunk)

# ─── Download: Post / Reel ─────────────────────────────────────────────────────

def _dl_post(shortcode: str, out: str) -> list[str]:
    s        = get_session()
    media_id = shortcode_to_mediaid(shortcode)

    resp = s.get(f"https://www.instagram.com/api/v1/media/{media_id}/info/", timeout=30)
    if resp.status_code == 401:
        reset_session()
        raise PermissionError("Sessão expirada. Exporte um novo cookies.txt.")
    resp.raise_for_status()

    data  = resp.json()
    items = data.get("items", [])
    if not items:
        raise ValueError("Nenhuma mídia encontrada para este post.")

    item     = items[0]
    medias   = item.get("carousel_media", [item])

    for i, media in enumerate(medias):
        if media.get("video_versions"):
            best = sorted(media["video_versions"], key=lambda v: v.get("width", 0), reverse=True)[0]
            dest = os.path.join(out, f"{shortcode}_{i}.mp4")
            download_file(s, best["url"], dest)
        else:
            candidates = media.get("image_versions2", {}).get("candidates", [])
            if not candidates:
                continue
            best = sorted(candidates, key=lambda v: v.get("width", 0), reverse=True)[0]
            dest = os.path.join(out, f"{shortcode}_{i}.jpg")
            download_file(s, best["url"], dest)

    return collect_files(out)

# ─── Download: Story ───────────────────────────────────────────────────────────

def _get_user_id(s, username: str) -> str:
    """
    Busca o user_id numérico de um username via GraphQL público.
    Não requer autenticação e não tem rate limit agressivo.
    """
    import urllib.parse
    variables = urllib.parse.quote('{"username":"' + username + '","include_reel":false}')
    url = f"https://www.instagram.com/graphql/query/?query_hash=c9100bf9110dd6361671f113dd02e7d&variables={variables}"
    headers_bkp = dict(s.headers)
    # Usa User-Agent de browser para o GraphQL público
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    s.headers["Referer"] = f"https://www.instagram.com/{username}/"
    try:
        resp = s.get(url, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            user_id = data.get("data", {}).get("user", {}).get("id")
            if user_id:
                return user_id
    except Exception:
        pass
    finally:
        s.headers.update(headers_bkp)

    # Fallback: tenta web_profile_info com pausa
    import time
    time.sleep(3)
    resp = s.get(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
        timeout=20,
    )
    if resp.status_code == 200:
        user_id = resp.json().get("data", {}).get("user", {}).get("id")
        if user_id:
            return user_id

    raise ValueError(f"Não consegui obter o ID do usuário @{username}. Tente novamente em instantes.")


def _dl_story(username: str, mediaid: int, out: str) -> list[str]:
    """
    Baixa story via /api/v1/feed/reels_media/?reel_ids={user_id} —
    o endpoint exige user_id numérico, não username.
    """
    s = get_session()

    user_id = _get_user_id(s, username)

    resp = s.get(
        f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}",
        timeout=30,
    )
    if resp.status_code == 429:
        raise ValueError("Instagram bloqueou temporariamente. Tente em alguns minutos.")
    if resp.status_code != 200:
        resp.raise_for_status()

    reels_dict = resp.json().get("reels", {})
    reel = next(iter(reels_dict.values()), None) if reels_dict else None

    if not reel or not reel.get("items"):
        raise ValueError("Nenhum story ativo encontrado. Pode ter expirado (stories duram 24h).")

    # Procura o mediaid exato; se não achar, pega o mais recente
    item = None
    for it in reel["items"]:
        pk = str(it.get("pk") or it.get("id", "").split("_")[0])
        if pk == str(mediaid):
            item = it
            break
    if item is None:
        item = reel["items"][-1]

    if item.get("video_versions"):
        best = sorted(item["video_versions"], key=lambda v: v.get("width", 0), reverse=True)[0]
        dest = os.path.join(out, f"{mediaid}.mp4")
        download_file(s, best["url"], dest)
    else:
        candidates = item.get("image_versions2", {}).get("candidates", [])
        if not candidates:
            raise ValueError("Nenhuma mídia encontrada neste story.")
        best = sorted(candidates, key=lambda v: v.get("width", 0), reverse=True)[0]
        dest = os.path.join(out, f"{mediaid}.jpg")
        download_file(s, best["url"], dest)

    return collect_files(out)

# ─── Download: Destaque ────────────────────────────────────────────────────────

def _dl_highlight(highlight_id: int, out: str) -> list[str]:
    s = get_session()

    # Tenta os dois hosts — i.instagram.com tem menos rate limit
    data = None
    for base in ["https://i.instagram.com", "https://www.instagram.com"]:
        resp = s.get(
            f"{base}/api/v1/feed/reels_media/?reel_ids=highlight:{highlight_id}",
            timeout=30,
        )
        if resp.status_code == 429:
            raise ValueError("Instagram bloqueou temporariamente. Tente em alguns minutos.")
        if resp.status_code == 200:
            data = resp.json()
            break

    if not data:
        resp.raise_for_status()

    reels_dict = data.get("reels", {})
    reel = reels_dict.get(f"highlight:{highlight_id}")
    if not reel or not reel.get("items"):
        raise ValueError("Destaque não encontrado ou privado.")

    for i, item in enumerate(reel["items"]):
        if item.get("video_versions"):
            best = sorted(item["video_versions"], key=lambda v: v.get("width", 0), reverse=True)[0]
            dest = os.path.join(out, f"highlight_{i}.mp4")
            download_file(s, best["url"], dest)
        else:
            candidates = item.get("image_versions2", {}).get("candidates", [])
            if candidates:
                best = sorted(candidates, key=lambda v: v.get("width", 0), reverse=True)[0]
                dest = os.path.join(out, f"highlight_{i}.jpg")
                download_file(s, best["url"], dest)

    return collect_files(out)

# ─── Download: TikTok ──────────────────────────────────────────────────────────

def _dl_tiktok(url: str, out: str) -> list[str]:
    tmpl = os.path.join(out, "%(id)s.%(ext)s")
    opts = {
        "outtmpl": tmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,
        "socket_timeout": 60,
        "retries": 5,
    }
    before = set(Path(out).iterdir())
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    after = set(Path(out).iterdir())
    return sorted(str(f) for f in (after - before))

# ─── Async wrapper ─────────────────────────────────────────────────────────────

async def run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

# ─── Envio ─────────────────────────────────────────────────────────────────────

async def send_files(update: Update, files: list[str]) -> None:
    LIMIT       = 50 * 1024 * 1024
    PHOTO_LIMIT = 10 * 1024 * 1024
    for fp in files:
        if not os.path.isfile(fp):
            continue
        size    = os.path.getsize(fp)
        size_mb = size / (1024 * 1024)
        ext     = Path(fp).suffix.lower()
        if size > LIMIT:
            await update.message.reply_text(f"⚠️ Arquivo com *{size_mb:.1f} MB* excede 50 MB do Telegram.", parse_mode=ParseMode.MARKDOWN)
            continue
        with open(fp, "rb") as f:
            if ext in (".mp4", ".mov", ".mkv", ".webm"):
                await update.message.reply_video(video=f, supports_streaming=True, caption=f"✅ Vídeo ({size_mb:.1f} MB)")
            elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                if size <= PHOTO_LIMIT:
                    await update.message.reply_photo(photo=f, caption=f"✅ Foto ({size_mb:.1f} MB)")
                else:
                    await update.message.reply_document(document=f, caption=f"✅ Foto original ({size_mb:.1f} MB)")
            else:
                await update.message.reply_document(document=f, caption=f"✅ Arquivo ({size_mb:.1f} MB)")

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cookies_ok = "✅" if os.path.isfile(COOKIES_FILE) else "❌"
    await update.message.reply_text(
        "👋 *Bot de Downloads*\n\n"
        "Envie um link do *Instagram* ou *TikTok*!\n\n"
        "• Posts, Reels, Fotos\n• Stories\n• Destaques\n• TikTok\n\n"
        f"🍪 Cookies: {cookies_ok}  |  ⚠️ Limite: 50 MB",
        parse_mode=ParseMode.MARKDOWN,
    )


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Testa a conexão com o Instagram e mostra o resultado detalhado."""
    await update.message.reply_text("🔍 Testando conexão com Instagram...")

    if not os.path.isfile(COOKIES_FILE):
        await update.message.reply_text("❌ cookies.txt não encontrado.")
        return

    import http.cookiejar as hcj, requests as req

    jar = hcj.MozillaCookieJar()
    jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    s = req.Session()
    cookies = {}
    for c in jar:
        if "instagram.com" in c.domain:
            s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
            cookies[c.name] = c.value

    s.headers.update({
        "User-Agent": "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; pt_BR; 453709483)",
        "X-IG-App-ID": "936619743392459",
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "Accept-Language": "pt-BR",
    })

    results = []

    # Teste 1: story pelo mediaid (URL que você enviou)
    mediaid = "3927525379527280424"
    try:
        r = s.get(f"https://www.instagram.com/api/v1/media/{mediaid}/info/", timeout=15)
        results.append(f"*Story /media/info/* → {r.status_code}\n{r.text[:200]}")
    except Exception as e:
        results.append(f"*Story /media/info/* → erro: `{e}`")

    # Teste 2: story pelo mediaid no i.instagram.com
    try:
        r = s.get(f"https://i.instagram.com/api/v1/media/{mediaid}/info/", timeout=15)
        results.append(f"*Story i.ig /media/info/* → {r.status_code}\n{r.text[:200]}")
    except Exception as e:
        results.append(f"*Story i.ig /media/info/* → erro: `{e}`")

    # Teste 3: highlight
    highlight_id = "18042166463437142"
    try:
        r = s.get(f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids=highlight:{highlight_id}", timeout=15)
        results.append(f"*Highlight reels_media/* → {r.status_code}\n{r.text[:200]}")
    except Exception as e:
        results.append(f"*Highlight reels_media/* → erro: `{e}`")

    # Teste 4: busca de user_id via endpoint público (__a=1)
    try:
        r = s.get(
            "https://www.instagram.com/af.existe/",
            params={"__a": "1", "__d": "dis"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        results.append(f"*Perfil __a=1* → {r.status_code}\n{r.text[:300]}")
    except Exception as e:
        results.append(f"*Perfil __a=1* → erro: {e}")

    # Teste 5: busca de user_id via web_profile_info (com pausa)
    try:
        import time
        time.sleep(2)
        r = s.get("https://www.instagram.com/api/v1/users/web_profile_info/?username=af.existe", timeout=15)
        results.append(f"*web_profile_info* → {r.status_code}\n{r.text[:300]}")
    except Exception as e:
        results.append(f"*web_profile_info* → erro: {e}")

    for res in results:
        # Sem parse_mode para evitar que caracteres do JSON quebrem o Markdown
        plain = res.replace("*", "").replace("`", "")
        await update.message.reply_text(plain)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    m = ANY_LINK.search(update.message.text)
    if not m:
        await update.message.reply_text("🔗 Nenhum link do Instagram ou TikTok encontrado.")
        return

    url   = m.group(0)
    is_ig = any(d in url.lower() for d in ["instagram.com", "instagr.am"])
    logger.info(f"Download: {url}")

    if is_ig and not os.path.isfile(COOKIES_FILE):
        await update.message.reply_text("⚠️ cookies.txt não encontrado no repositório.")
        return

    status = await update.message.reply_text("⏳ Baixando...")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            files: list[str] = []

            if is_ig:
                if (hm := HIGHLIGHT_RE.search(url)):
                    await status.edit_text("⏳ Baixando destaque...")
                    files = await run(_dl_highlight, int(hm.group(1)), tmp)

                elif (bm := HIGHLIGHT_B64_RE.search(url)):
                    hid = decode_highlight_b64(bm.group(1))
                    if not hid:
                        await status.edit_text("❌ Link de destaque inválido.")
                        return
                    await status.edit_text("⏳ Baixando destaque...")
                    files = await run(_dl_highlight, hid, tmp)

                elif (sm := STORY_RE.search(url)):
                    await status.edit_text("⏳ Baixando story...")
                    files = await run(_dl_story, sm.group(1), int(sm.group(2)), tmp)

                elif (pm := POST_RE.search(url)):
                    await status.edit_text("⏳ Baixando post...")
                    files = await run(_dl_post, pm.group(1), tmp)

                else:
                    await status.edit_text("❌ Tipo de link não reconhecido.")
                    return

            else:
                await status.edit_text("⏳ Baixando TikTok...")
                files = await run(_dl_tiktok, url, tmp)

            if not files:
                await status.edit_text("❌ Nenhum arquivo baixado.\n• Conteúdo privado?\n• Story expirado?\n• Link inválido?")
                return

            await status.edit_text(f"📤 Enviando {len(files)} arquivo(s)...")
            await send_files(update, files)
            await status.delete()

        except (PermissionError, ValueError) as e:
            await status.edit_text(f"❌ {e}")
        except FileNotFoundError as e:
            await status.edit_text(f"⚠️ {e}")
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "private" in msg.lower():
                await status.edit_text("🔒 Conteúdo privado.")
            else:
                await status.edit_text(f"❌ Erro TikTok:\n`{msg[:200]}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.exception(f"Erro: {e}")
            reset_session()
            await status.edit_text(f"❌ Erro:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)

# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Configure TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   start))
    app.add_handler(CommandHandler("debug",  debug_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 Bot iniciado!")
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
