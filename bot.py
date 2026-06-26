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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Configuração ──────────────────────────────────────────────────────────────

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

# ─── Padrões de URL ────────────────────────────────────────────────────────────

ANY_LINK = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)[^\s]*",
    re.IGNORECASE,
)
POST_RE      = re.compile(r"instagram\.com/(?:p|reel|tv|r)/([A-Za-z0-9_-]+)", re.IGNORECASE)
STORY_RE     = re.compile(r"instagram\.com/stories/([^/]+)/(\d+)",              re.IGNORECASE)
HIGHLIGHT_RE = re.compile(r"instagram\.com/stories/highlights/(\d+)",           re.IGNORECASE)
# Links de destaque compartilhados via /s/ com ID em base64
# Ex: instagram.com/s/aGlnaGxpZ2h0OjE4MDQy...?story_media_id=...
HIGHLIGHT_B64_RE = re.compile(r"instagram\.com/s/([A-Za-z0-9+/=_-]+)", re.IGNORECASE)

MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mov"}

# ─── Loader compartilhado ──────────────────────────────────────────────────────

_loader: instaloader.Instaloader | None = None

def get_loader() -> instaloader.Instaloader:
    """
    Autentica o Instaloader via cookies.txt (formato Netscape/Mozilla).
    Autentica via cookies.txt sem nenhuma requisição ao Instagram.
    """
    global _loader
    if _loader is not None:
        return _loader

    if not os.path.isfile(COOKIES_FILE):
        raise FileNotFoundError("cookies.txt não encontrado. Faça upload no repositório GitHub.")

    # Lê o cookies.txt e monta um dicionário nome→valor
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    cookies = {c.name: c.value for c in jar if "instagram.com" in c.domain}

    # Campos obrigatórios para o instaloader funcionar autenticado
    required = ["sessionid", "csrftoken", "ds_user_id"]
    missing  = [k for k in required if k not in cookies]
    if missing:
        raise instaloader.exceptions.LoginRequiredException(
            f"Os seguintes cookies estão faltando no cookies.txt: {missing}. "
            "Certifique-se de exportar os cookies estando logado no Instagram."
        )

    L = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    # Injeta todos os cookies diretamente na sessão requests interna.
    # Não faz nenhuma requisição ao Instagram aqui — zero risco de checkpoint.
    for c in jar:
        if "instagram.com" in c.domain:
            L.context._session.cookies.set(
                name=c.name,
                value=c.value,
                domain=c.domain,
                path=c.path,
            )

    # ds_user_id é o identificador numérico do usuário — o instaloader usa
    # context.username apenas internamente para logs e arquivos de sessão
    L.context.username = cookies["ds_user_id"]

    logger.info(f"Instagram autenticado via cookies (ds_user_id={cookies['ds_user_id']})")
    _loader = L
    return _loader


def reset_loader():
    global _loader
    _loader = None


def decode_highlight_b64(encoded: str) -> int | None:
    """Decodifica o ID de destaque em base64 do formato /s/ do Instagram."""
    try:
        # Normaliza URL-safe base64 para base64 padrão e adiciona padding
        encoded = encoded.replace("-", "+").replace("_", "/")
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += "=" * padding
        decoded = base64.b64decode(encoded).decode("utf-8")
        # Formato esperado: "highlight:18042166463437142"
        m = re.search(r"highlight:(\d+)", decoded)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def collect_files(directory: str) -> list[str]:
    return sorted(
        str(f) for f in Path(directory).rglob("*")
        if f.suffix.lower() in MEDIA_EXTS and f.is_file()
    )

# ─── Downloads Instagram ───────────────────────────────────────────────────────

def _dl_post(shortcode: str, out: str) -> list[str]:
    L = get_loader()
    L.dirname_pattern  = out
    L.filename_pattern = "{shortcode}"
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=out)
    return collect_files(out)


def _dl_story(username: str, mediaid: int, out: str) -> list[str]:
    L = get_loader()
    L.dirname_pattern  = out
    L.filename_pattern = "{mediaid}"
    profile = instaloader.Profile.from_username(L.context, username)
    for story in L.get_stories(userids=[profile.userid]):
        for item in story.get_items():
            if item.mediaid == mediaid:
                L.download_storyitem(item, target=out)
                return collect_files(out)
    raise ValueError("Story não encontrado ou já expirou (stories duram 24h).")


def _dl_highlight(highlight_id: int, out: str) -> list[str]:
    L = get_loader()
    L.dirname_pattern  = out
    L.filename_pattern = "{mediaid}"

    data = L.context.graphql_query(
        "45246d3fe16ccc6577e0bd297a5db1ab",
        {
            "reel_ids": [], "tag_names": [], "location_ids": [],
            "highlight_reel_ids": [str(highlight_id)],
            "precomposed_overlay": False,
            "show_story_viewer_list": True,
            "story_viewer_fetch_count": 50,
            "story_viewer_cursor": "",
            "stories_video_dash_manifest": False,
        },
    )
    reels = data.get("data", {}).get("reels_media", [])
    if not reels:
        raise ValueError("Destaque não encontrado ou privado.")

    reel = reels[0]
    owner_profile = instaloader.Profile(L.context, reel.get("owner", {}))
    for item_node in reel.get("items", []):
        item = instaloader.StoryItem(L.context, item_node, owner_profile)
        L.download_storyitem(item, target=out)

    return collect_files(out)


# ─── Download TikTok ───────────────────────────────────────────────────────────

def _dl_tiktok(url: str, out: str) -> list[str]:
    tmpl = os.path.join(out, "%(id)s.%(ext)s")
    opts = {
        "outtmpl": tmpl,
        "format": (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio"
            "/bestvideo+bestaudio/best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "format_sort": ["res", "vbr", "abr", "ext:mp4:m4a", "fps"],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
    }
    before = set(Path(out).iterdir())
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    after = set(Path(out).iterdir())
    return sorted(str(f) for f in (after - before))

# ─── Async wrapper ─────────────────────────────────────────────────────────────

async def run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

# ─── Envio de arquivos ─────────────────────────────────────────────────────────

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
            await update.message.reply_text(
                f"⚠️ Arquivo com *{size_mb:.1f} MB* — acima do limite de 50 MB do Telegram.",
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        with open(fp, "rb") as f:
            if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                await update.message.reply_video(
                    video=f, supports_streaming=True,
                    caption=f"✅ Vídeo ({size_mb:.1f} MB)",
                )
            elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                if size <= PHOTO_LIMIT:
                    await update.message.reply_photo(photo=f, caption=f"✅ Foto ({size_mb:.1f} MB)")
                else:
                    await update.message.reply_document(document=f, caption=f"✅ Foto original ({size_mb:.1f} MB)")
            else:
                await update.message.reply_document(document=f, caption=f"✅ Arquivo ({size_mb:.1f} MB)")

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cookies_ok = "✅ encontrado" if os.path.isfile(COOKIES_FILE) else "❌ não encontrado"
    await update.message.reply_text(
        "👋 *Olá! Sou o bot de downloads.*\n\n"
        "📲 Me envie um link do *Instagram* ou *TikTok*!\n\n"
        "✅ *Suporto:*\n"
        "• Instagram — Posts, Reels, Fotos, Stories, Destaques\n"
        "• TikTok — Vídeos\n\n"
        f"🍪 *Cookies Instagram:* {cookies_ok}\n"
        "⚠️ *Limite:* 50 MB por arquivo.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    url_match = ANY_LINK.search(update.message.text)
    if not url_match:
        await update.message.reply_text(
            "🔗 Não encontrei um link do Instagram ou TikTok.\nEnvie apenas o link."
        )
        return

    url   = url_match.group(0)
    is_ig = any(d in url.lower() for d in ["instagram.com", "instagr.am"])
    logger.info(f"URL de {update.effective_user.id}: {url}")

    if is_ig and not os.path.isfile(COOKIES_FILE):
        await update.message.reply_text(
            "⚠️ *cookies.txt não encontrado.*\n\n"
            "Faça upload do arquivo no repositório GitHub e aguarde o Railway atualizar.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = await update.message.reply_text("⏳ Baixando... aguarde.")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            files: list[str] = []

            if is_ig:
                if (m := HIGHLIGHT_RE.search(url)):
                    await status.edit_text("⏳ Baixando destaque...")
                    files = await run(_dl_highlight, int(m.group(1)), tmp)

                elif (m := HIGHLIGHT_B64_RE.search(url)):
                    # Links do tipo /s/aGlnaGxpZ2h0... (compartilhados via share sheet)
                    highlight_id = decode_highlight_b64(m.group(1))
                    if not highlight_id:
                        await status.edit_text("❌ Não consegui identificar o destaque nesse link.")
                        return
                    await status.edit_text("⏳ Baixando destaque...")
                    files = await run(_dl_highlight, highlight_id, tmp)

                elif (m := STORY_RE.search(url)):
                    await status.edit_text("⏳ Baixando story...")
                    files = await run(_dl_story, m.group(1), int(m.group(2)), tmp)

                elif (m := POST_RE.search(url)):
                    await status.edit_text("⏳ Baixando post/reel...")
                    files = await run(_dl_post, m.group(1), tmp)

                else:
                    await status.edit_text(
                        "❌ Tipo de link do Instagram não reconhecido.\n"
                        "Suportado: posts, reels, stories e destaques."
                    )
                    return

            else:
                await status.edit_text("⏳ Baixando TikTok...")
                files = await run(_dl_tiktok, url, tmp)

            if not files:
                await status.edit_text(
                    "❌ Nenhum arquivo foi baixado.\n\n"
                    "Possíveis causas:\n"
                    "• Conteúdo privado\n"
                    "• Story já expirou (duram 24h)\n"
                    "• Link inválido ou removido"
                )
                return

            await status.edit_text(f"📤 Enviando {len(files)} arquivo(s)...")
            await send_files(update, files)
            await status.delete()

        except FileNotFoundError as e:
            await status.edit_text(f"⚠️ {e}")

        except instaloader.exceptions.LoginRequiredException as e:
            reset_loader()
            await status.edit_text(
                f"🔐 *Problema com os cookies do Instagram:*\n`{str(e)[:300]}`\n\n"
                "Exporte um novo `cookies.txt` estando logado no Chrome e faça upload no GitHub.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            await status.edit_text("🔒 Perfil privado. A conta precisa seguir esse perfil.")
        except instaloader.exceptions.TooManyRequestsException:
            await status.edit_text("⏱️ Instagram bloqueou temporariamente. Tente em alguns minutos.")
        except instaloader.exceptions.InstaloaderException as e:
            logger.error(f"InstaloaderException: {e}")
            reset_loader()
            await status.edit_text(f"❌ Erro do Instagram:\n`{str(e)[:300]}`", parse_mode=ParseMode.MARKDOWN)
        except ValueError as e:
            await status.edit_text(f"❌ {e}")
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "private" in msg.lower():
                await status.edit_text("🔒 Conteúdo privado.")
            elif "404" in msg or "not found" in msg.lower():
                await status.edit_text("❌ Conteúdo não encontrado ou removido.")
            else:
                await status.edit_text(f"❌ Erro TikTok:\n`{msg[:300]}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.exception(f"Erro inesperado: {e}")
            await status.edit_text(f"❌ Erro inesperado:\n`{str(e)[:300]}`", parse_mode=ParseMode.MARKDOWN)

# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Configure TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot iniciado!")
    app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
