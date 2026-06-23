import os
import re
import asyncio
import tempfile
import logging
import glob
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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "SEU_TOKEN_AQUI")

# Credenciais do Instagram (variáveis de ambiente no Railway)
INSTAGRAM_USER = os.getenv("INSTAGRAM_USER", "")
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASS", "")

# Regex para detectar links
URL_PATTERN = re.compile(
    r"https?://(?:www\.)?"
    r"(?:instagram\.com|instagr\.am|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)"
    r"[^\s]*",
    re.IGNORECASE,
)

# Regex para extrair o shortcode de um link do Instagram
# Ex: instagram.com/p/ABC123/ ou instagram.com/reel/ABC123/
INSTAGRAM_SHORTCODE_PATTERN = re.compile(
    r"instagram\.com/(?:p|reel|tv|r)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def is_instagram_url(url: str) -> bool:
    return any(d in url.lower() for d in ["instagram.com", "instagr.am"])


def get_instagram_shortcode(url: str) -> str | None:
    match = INSTAGRAM_SHORTCODE_PATTERN.search(url)
    return match.group(1) if match else None


# ─── Download Instagram via Instaloader ────────────────────────────────────────

def _download_instagram(shortcode: str, output_dir: str) -> list[str]:
    """Baixa um post do Instagram pelo shortcode e retorna lista de arquivos."""
    L = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        dirname_pattern=output_dir,
        filename_pattern="{shortcode}",
        quiet=True,
    )

    # Faz login se as credenciais estiverem configuradas
    if INSTAGRAM_USER and INSTAGRAM_PASS:
        try:
            L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
            logger.info(f"Login no Instagram como {INSTAGRAM_USER}")
        except Exception as e:
            logger.warning(f"Falha no login do Instagram: {e}")

    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=output_dir)

    # Coleta apenas arquivos de mídia (ignora .txt, .json, etc)
    media_extensions = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mov"}
    files = [
        str(f) for f in Path(output_dir).iterdir()
        if f.suffix.lower() in media_extensions
    ]
    return sorted(files)


async def download_instagram(shortcode: str, output_dir: str) -> list[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_instagram, shortcode, output_dir)


# ─── Download TikTok via yt-dlp ────────────────────────────────────────────────

def get_ydl_opts(output_path: str) -> dict:
    return {
        "outtmpl": output_path,
        "format": (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]"
            "/best"
        ),
        "merge_output_format": "mp4",
        "format_sort": ["res", "vbr", "abr", "ext:mp4:m4a", "fps"],
        "postprocessors": [
            {"key": "FFmpegMetadata", "add_metadata": True}
        ],
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
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
    }


def _download_tiktok(url: str, output_dir: str) -> list[str]:
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    opts = get_ydl_opts(output_template)

    files_before = set(Path(output_dir).iterdir())
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files_after = set(Path(output_dir).iterdir())
    return [str(f) for f in (files_after - files_before)]


async def download_tiktok(url: str, output_dir: str) -> list[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_tiktok, url, output_dir)


# ─── Envio de arquivos ─────────────────────────────────────────────────────────

async def send_files(update: Update, files: list[str]) -> None:
    TELEGRAM_LIMIT = 50 * 1024 * 1024
    PHOTO_LIMIT    = 10 * 1024 * 1024

    for file_path in files:
        if not os.path.isfile(file_path):
            continue

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()
        size_mb = file_size / (1024 * 1024)

        if file_size > TELEGRAM_LIMIT:
            await update.message.reply_text(
                f"⚠️ Um arquivo tem *{size_mb:.1f} MB* — acima do limite de 50 MB do Telegram e não pode ser enviado.",
                parse_mode=ParseMode.MARKDOWN,
            )
            continue

        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            with open(file_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                    caption=f"✅ Vídeo em máxima qualidade! ({size_mb:.1f} MB)",
                )
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            with open(file_path, "rb") as f:
                if file_size <= PHOTO_LIMIT:
                    await update.message.reply_photo(
                        photo=f,
                        caption=f"✅ Foto! ({size_mb:.1f} MB)",
                    )
                else:
                    await update.message.reply_document(
                        document=f,
                        caption=f"✅ Foto em qualidade original! ({size_mb:.1f} MB)",
                    )
        else:
            with open(file_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    caption=f"✅ Arquivo! ({size_mb:.1f} MB)",
                )


# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ig_status = "✅ configurado" if (INSTAGRAM_USER and INSTAGRAM_PASS) else "❌ não configurado"
    await update.message.reply_text(
        "👋 *Olá! Sou o bot de downloads.*\n\n"
        "📲 Me envie um link do *Instagram* ou *TikTok*!\n\n"
        "✅ *Suportado:*\n"
        "• Instagram — Reels, Posts, Fotos\n"
        "• TikTok — Vídeos\n\n"
        f"📷 *Instagram login:* {ig_status}\n\n"
        "⚠️ *Limite:* arquivos até 50 MB (limitação do Telegram).",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ *Como usar:*\n\n"
        "Envie um link do Instagram ou TikTok diretamente no chat.\n\n"
        "Exemplos:\n"
        "`https://www.instagram.com/reel/xxxxx/`\n"
        "`https://www.instagram.com/p/xxxxx/`\n"
        "`https://www.tiktok.com/@usuario/video/xxxxx`\n"
        "`https://vm.tiktok.com/xxxxx/`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "🔗 Não encontrei um link do Instagram ou TikTok na sua mensagem."
        )
        return

    status_msg = await update.message.reply_text("⏳ Baixando... aguarde um momento.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            if is_instagram_url(url):
                # ── Instagram: usa Instaloader ─────────────────────────────────
                shortcode = get_instagram_shortcode(url)
                if not shortcode:
                    await status_msg.edit_text(
                        "❌ Não consegui identificar o post nesse link do Instagram.\n"
                        "Certifique-se de que o link é de um post, reel ou foto."
                    )
                    return

                if not (INSTAGRAM_USER and INSTAGRAM_PASS):
                    await status_msg.edit_text(
                        "⚠️ *As credenciais do Instagram não estão configuradas.*\n\n"
                        "Configure as variáveis `INSTAGRAM_USER` e `INSTAGRAM_PASS` no Railway.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                files = await download_instagram(shortcode, tmp_dir)

            else:
                # ── TikTok: usa yt-dlp ─────────────────────────────────────────
                files = await download_tiktok(url, tmp_dir)

            if not files:
                await status_msg.edit_text(
                    "❌ Não consegui baixar esse conteúdo.\n\n"
                    "Possíveis motivos:\n"
                    "• Conteúdo privado\n"
                    "• Link expirado ou inválido\n"
                    "• Arquivo maior que 50 MB"
                )
                return

            await status_msg.edit_text(f"📤 Enviando {len(files)} arquivo(s)...")
            await send_files(update, files)
            await status_msg.delete()

        except instaloader.exceptions.LoginRequiredException:
            await status_msg.edit_text(
                "🔐 *Login necessário.*\n\n"
                "Configure as variáveis `INSTAGRAM_USER` e `INSTAGRAM_PASS` no Railway.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except instaloader.exceptions.BadCredentialsException:
            await status_msg.edit_text(
                "❌ *Credenciais do Instagram incorretas.*\n\n"
                "Verifique o usuário e senha nas variáveis do Railway.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            await status_msg.edit_text(
                "🔒 *Perfil privado.*\n\n"
                "Você só pode baixar posts de perfis privados que a conta do bot segue.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except instaloader.exceptions.InstaloaderException as e:
            logger.error(f"InstaloaderException para {url}: {e}")
            await status_msg.edit_text(
                f"❌ Erro ao baixar do Instagram:\n`{str(e)[:300]}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            logger.error(f"DownloadError para {url}: {error_msg}")
            if "private" in error_msg.lower():
                msg = "🔒 Esse conteúdo é *privado* e não pode ser baixado."
            elif "not found" in error_msg.lower() or "404" in error_msg:
                msg = "❌ Conteúdo *não encontrado*. O link pode ter sido removido."
            else:
                msg = f"❌ Erro ao baixar:\n`{error_msg[:300]}`"
            await status_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.exception(f"Erro inesperado para {url}")
            await status_msg.edit_text(
                f"❌ Erro inesperado:\n`{str(e)[:300]}`",
                parse_mode=ParseMode.MARKDOWN,
            )


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "SEU_TOKEN_AQUI":
        raise ValueError("❌ Configure a variável de ambiente TELEGRAM_BOT_TOKEN")

    ig_ok = bool(INSTAGRAM_USER and INSTAGRAM_PASS)
    logger.info(f"Instagram login: {'configurado ✅' if ig_ok else 'NÃO configurado ❌'}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot iniciado!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
