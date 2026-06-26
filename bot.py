import os
import re
import asyncio
import tempfile
import logging
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

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
IG_USER     = os.getenv("INSTAGRAM_USER", "")
IG_PASS     = os.getenv("INSTAGRAM_PASS", "")

# ─── Padrões de URL ────────────────────────────────────────────────────────────

# Detecta qualquer link do Instagram ou TikTok no texto
ANY_LINK = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)[^\s]*",
    re.IGNORECASE,
)

# /p/CODE  /reel/CODE  /tv/CODE  /r/CODE  — posts normais
POST_RE = re.compile(
    r"instagram\.com/(?:p|reel|tv|r)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# /stories/USERNAME/MEDIAID/
STORY_RE = re.compile(
    r"instagram\.com/stories/([^/]+)/(\d+)",
    re.IGNORECASE,
)

# /stories/highlights/HIGHLIGHT_ID/
HIGHLIGHT_RE = re.compile(
    r"instagram\.com/stories/highlights/(\d+)",
    re.IGNORECASE,
)

MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mov"}

# ─── Login compartilhado ────────────────────────────────────────────────────────

_loader: instaloader.Instaloader | None = None

def get_loader(output_dir: str = "/tmp") -> instaloader.Instaloader:
    """Retorna instância do Instaloader com login, reutilizando sessão."""
    global _loader
    if _loader is None:
        _loader = instaloader.Instaloader(
            download_pictures=True,
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        if IG_USER and IG_PASS:
            try:
                _loader.login(IG_USER, IG_PASS)
                logger.info(f"Login Instagram OK: {IG_USER}")
            except Exception as e:
                logger.error(f"Falha login Instagram: {e}")
                _loader = None
                raise
    return _loader


def collect_files(directory: str) -> list[str]:
    """Coleta arquivos de mídia de um diretório."""
    return sorted(
        str(f) for f in Path(directory).rglob("*")
        if f.suffix.lower() in MEDIA_EXTS and f.is_file()
    )

# ─── Download: Post normal ──────────────────────────────────────────────────────

def _dl_post(shortcode: str, out: str) -> list[str]:
    L = get_loader()
    L.dirname_pattern  = out
    L.filename_pattern = "{shortcode}"
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=out)
    return collect_files(out)

# ─── Download: Story individual ─────────────────────────────────────────────────

def _dl_story(username: str, mediaid: int, out: str) -> list[str]:
    L = get_loader()
    profile = instaloader.Profile.from_username(L.context, username)
    # Percorre stories do perfil até achar o mediaid certo
    for story in L.get_stories(userids=[profile.userid]):
        for item in story.get_items():
            if item.mediaid == mediaid:
                L.dirname_pattern  = out
                L.filename_pattern = "{mediaid}"
                L.download_storyitem(item, target=out)
                return collect_files(out)
    raise ValueError(f"Story {mediaid} não encontrado ou já expirou.")

# ─── Download: Destaque (Highlight) ────────────────────────────────────────────

def _dl_highlight(highlight_id: int, out: str) -> list[str]:
    L = get_loader()
    L.dirname_pattern  = out
    L.filename_pattern = "{mediaid}"

    # get_highlights aceita um Profile, mas precisamos achar pelo ID numérico.
    # Usamos a API interna para buscar o highlight diretamente pelo ID.
    data = L.context.graphql_query(
        "45246d3fe16ccc6577e0bd297a5db1ab",
        {"reel_ids": [], "tag_names": [], "location_ids": [],
         "highlight_reel_ids": [str(highlight_id)],
         "precomposed_overlay": False, "show_story_viewer_list": True,
         "story_viewer_fetch_count": 50, "story_viewer_cursor": "",
         "stories_video_dash_manifest": False},
    )
    edges = (data.get("data", {})
                 .get("reels_media", []))

    if not edges:
        raise ValueError("Destaque não encontrado ou privado.")

    reel = edges[0]
    owner_node = reel.get("owner", {})
    owner_profile = instaloader.Profile(L.context, owner_node)

    for item_node in reel.get("items", []):
        item = instaloader.StoryItem(L.context, item_node, owner_profile)
        L.download_storyitem(item, target=out)

    return collect_files(out)

# ─── Download: TikTok ──────────────────────────────────────────────────────────

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

# ─── Wrappers async ────────────────────────────────────────────────────────────

async def run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)

# ─── Envio de arquivos ─────────────────────────────────────────────────────────

async def send_files(update: Update, files: list[str]) -> None:
    LIMIT       = 50 * 1024 * 1024
    PHOTO_LIMIT = 10 * 1024 * 1024

    for fp in files:
        if not os.path.isfile(fp):
            continue
        size     = os.path.getsize(fp)
        size_mb  = size / (1024 * 1024)
        ext      = Path(fp).suffix.lower()

        if size > LIMIT:
            await update.message.reply_text(
                f"⚠️ Um arquivo tem *{size_mb:.1f} MB* — acima do limite de 50 MB do Telegram.",
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        with open(fp, "rb") as f:
            if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                    caption=f"✅ Vídeo ({size_mb:.1f} MB)",
                )
            elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                if size <= PHOTO_LIMIT:
                    await update.message.reply_photo(
                        photo=f,
                        caption=f"✅ Foto ({size_mb:.1f} MB)",
                    )
                else:
                    await update.message.reply_document(
                        document=f,
                        caption=f"✅ Foto em qualidade original ({size_mb:.1f} MB)",
                    )
            else:
                await update.message.reply_document(
                    document=f,
                    caption=f"✅ Arquivo ({size_mb:.1f} MB)",
                )

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ig = "✅ configurado" if (IG_USER and IG_PASS) else "❌ não configurado"
    await update.message.reply_text(
        "👋 *Olá! Sou o bot de downloads.*\n\n"
        "📲 Me envie um link do *Instagram* ou *TikTok*!\n\n"
        "✅ *Suporto:*\n"
        "• Instagram — Posts, Reels, Fotos, Stories, Destaques\n"
        "• TikTok — Vídeos\n\n"
        f"📷 *Instagram login:* {ig}\n"
        "⚠️ *Limite de envio:* 50 MB por arquivo.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Garante que a mensagem existe e tem texto
    if not update.message or not update.message.text:
        return

    text = update.message.text
    match = ANY_LINK.search(text)

    if not match:
        await update.message.reply_text(
            "🔗 Não encontrei um link do Instagram ou TikTok na sua mensagem.\n"
            "Envie apenas o link."
        )
        return

    url = match.group(0)
    logger.info(f"URL recebida de {update.effective_user.id}: {url}")

    # Credenciais obrigatórias para Instagram
    is_ig = any(d in url.lower() for d in ["instagram.com", "instagr.am"])
    if is_ig and not (IG_USER and IG_PASS):
        await update.message.reply_text(
            "⚠️ *Credenciais do Instagram não configuradas.*\n"
            "Configure `INSTAGRAM_USER` e `INSTAGRAM_PASS` no Railway.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = await update.message.reply_text("⏳ Baixando... aguarde.")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            files: list[str] = []

            if is_ig:
                # ── Destaque ──────────────────────────────────────────────────
                hl = HIGHLIGHT_RE.search(url)
                if hl:
                    await status.edit_text("⏳ Baixando destaque...")
                    highlight_id = int(hl.group(1))
                    files = await run(_dl_highlight, highlight_id, tmp)

                # ── Story individual ──────────────────────────────────────────
                elif (st := STORY_RE.search(url)):
                    await status.edit_text("⏳ Baixando story...")
                    username = st.group(1)
                    mediaid  = int(st.group(2))
                    files = await run(_dl_story, username, mediaid, tmp)

                # ── Post / Reel / Foto ────────────────────────────────────────
                elif (po := POST_RE.search(url)):
                    await status.edit_text("⏳ Baixando post...")
                    files = await run(_dl_post, po.group(1), tmp)

                else:
                    await status.edit_text(
                        "❌ Não reconheci esse tipo de link do Instagram.\n"
                        "Suportado: posts, reels, stories e destaques."
                    )
                    return

            else:
                # ── TikTok ────────────────────────────────────────────────────
                await status.edit_text("⏳ Baixando TikTok...")
                files = await run(_dl_tiktok, url, tmp)

            if not files:
                await status.edit_text(
                    "❌ Nenhum arquivo foi baixado.\n\n"
                    "Possíveis causas:\n"
                    "• Conteúdo privado\n"
                    "• Story já expirou\n"
                    "• Link inválido ou removido"
                )
                return

            await status.edit_text(f"📤 Enviando {len(files)} arquivo(s)...")
            await send_files(update, files)
            await status.delete()

        # ── Erros específicos do Instagram ────────────────────────────────────
        except instaloader.exceptions.LoginRequiredException:
            await status.edit_text("🔐 Login necessário. Verifique `INSTAGRAM_USER` e `INSTAGRAM_PASS`.", parse_mode=ParseMode.MARKDOWN)
        except instaloader.exceptions.BadCredentialsException:
            await status.edit_text("❌ Usuário ou senha do Instagram incorretos.")
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            await status.edit_text("🔒 Perfil privado. A conta do bot precisa seguir esse perfil.")
        except instaloader.exceptions.TooManyRequestsException:
            await status.edit_text("⏱️ Instagram bloqueou temporariamente as requisições. Tente novamente em alguns minutos.")
        except instaloader.exceptions.InstaloaderException as e:
            logger.error(f"InstaloaderException: {e}")
            await status.edit_text(f"❌ Erro do Instagram:\n`{str(e)[:300]}`", parse_mode=ParseMode.MARKDOWN)
        except ValueError as e:
            await status.edit_text(f"❌ {e}")

        # ── Erros do TikTok ───────────────────────────────────────────────────
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "private" in msg.lower():
                await status.edit_text("🔒 Conteúdo privado.")
            elif "404" in msg or "not found" in msg.lower():
                await status.edit_text("❌ Conteúdo não encontrado ou removido.")
            else:
                await status.edit_text(f"❌ Erro ao baixar:\n`{msg[:300]}`", parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.exception(f"Erro inesperado: {e}")
            await status.edit_text(f"❌ Erro inesperado:\n`{str(e)[:300]}`", parse_mode=ParseMode.MARKDOWN)

# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Configure a variável TELEGRAM_BOT_TOKEN")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  start))
    # Captura TODAS as mensagens de texto, incluindo as que vêm de grupos
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot iniciado!")
    app.run_polling(
        allowed_updates=["message"],   # garante receber mensagens privadas e de grupo
        drop_pending_updates=True,     # ignora mensagens acumuladas enquanto estava offline
    )

if __name__ == "__main__":
    main()
