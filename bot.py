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

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Configuração ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "SEU_TOKEN_AQUI")

# Caminho para o arquivo de cookies do Instagram
# O arquivo deve estar na mesma pasta que bot.py, com o nome cookies.txt
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

# Regex para detectar links
URL_PATTERN = re.compile(
    r"https?://(?:www\.)?"
    r"(?:instagram\.com|instagr\.am|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)"
    r"[^\s]*",
    re.IGNORECASE,
)

SUPPORTED_DOMAINS = ["instagram.com", "instagr.am", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]

# ─── Helpers ───────────────────────────────────────────────────────────────────

def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def is_instagram_url(url: str) -> bool:
    return any(d in url.lower() for d in ["instagram.com", "instagr.am"])


def get_ydl_opts(output_path: str, url: str = "") -> dict:
    opts = {
        "outtmpl": output_path,

        # ── Qualidade máxima ────────────────────────────────────────────────────
        "format": (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]"
            "/best"
        ),
        "merge_output_format": "mp4",
        "format_sort": ["res", "vbr", "abr", "ext:mp4:m4a", "fps"],

        # ── Pós-processamento ───────────────────────────────────────────────────
        "postprocessors": [
            {"key": "FFmpegMetadata", "add_metadata": True}
        ],

        # ── Comportamento ───────────────────────────────────────────────────────
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,

        # ── Headers ─────────────────────────────────────────────────────────────
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },

        # ── Timeouts e retries ──────────────────────────────────────────────────
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "continuedl": True,
    }

    # ── Cookies do Instagram (obrigatório para o Instagram funcionar) ───────────
    if is_instagram_url(url) and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logger.info(f"Usando cookies do Instagram: {COOKIES_FILE}")
    elif is_instagram_url(url):
        logger.warning("cookies.txt não encontrado — Instagram pode falhar!")

    return opts


async def download_media(url: str, output_dir: str) -> list[str]:
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    opts = get_ydl_opts(output_template, url)

    def _download():
        files_before = set(Path(output_dir).iterdir())
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files_after = set(Path(output_dir).iterdir())
        return [str(f) for f in (files_after - files_before)]

    loop = asyncio.get_event_loop()
    files = await loop.run_in_executor(None, _download)
    return files


# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cookies_ok = "✅" if os.path.isfile(COOKIES_FILE) else "❌"
    await update.message.reply_text(
        "👋 *Olá! Sou o bot de downloads.*\n\n"
        "📲 Me envie um link do *Instagram* ou *TikTok* e eu faço o download!\n\n"
        "✅ *Suportado:*\n"
        "• Instagram — Reels, Posts, Stories\n"
        "• TikTok — Vídeos\n\n"
        f"🍪 *Cookies do Instagram:* {cookies_ok}\n\n"
        "⚠️ *Limite:* arquivos até 50 MB (limitação do Telegram).",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ *Como usar:*\n\n"
        "Basta enviar ou colar um link do Instagram ou TikTok diretamente no chat.\n\n"
        "Exemplos:\n"
        "`https://www.instagram.com/reel/xxxxx/`\n"
        "`https://www.tiktok.com/@usuario/video/xxxxx`\n"
        "`https://vm.tiktok.com/xxxxx/`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "🔗 Não encontrei um link do Instagram ou TikTok na sua mensagem.\n"
            "Tente enviar apenas o link."
        )
        return

    # Avisa se for Instagram e não tiver cookies
    if is_instagram_url(url) and not os.path.isfile(COOKIES_FILE):
        await update.message.reply_text(
            "⚠️ *Atenção:* o arquivo `cookies.txt` do Instagram não foi encontrado.\n"
            "O Instagram exige login para baixar conteúdos. "
            "Siga as instruções do README para adicionar o cookies.txt ao projeto.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await update.message.reply_text("⏳ Baixando... aguarde um momento.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            files = await download_media(url, tmp_dir)

            if not files:
                await status_msg.edit_text(
                    "❌ Não consegui baixar esse conteúdo.\n\n"
                    "Possíveis motivos:\n"
                    "• Conteúdo privado ou protegido\n"
                    "• Link expirado\n"
                    "• Arquivo maior que 50 MB"
                )
                return

            await status_msg.edit_text(f"📤 Enviando {len(files)} arquivo(s)...")

            TELEGRAM_LIMIT = 50 * 1024 * 1024
            PHOTO_LIMIT    = 10 * 1024 * 1024

            for file_path in files:
                file_size = os.path.getsize(file_path)
                ext = Path(file_path).suffix.lower()
                size_mb = file_size / (1024 * 1024)

                if file_size > TELEGRAM_LIMIT:
                    await update.message.reply_text(
                        f"⚠️ O arquivo tem *{size_mb:.1f} MB* — acima do limite de 50 MB do Telegram.\n"
                        "O conteúdo foi baixado em máxima qualidade, mas não é possível enviá-lo pelo bot.",
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
                                caption=f"✅ Foto em qualidade original! ({size_mb:.1f} MB)\n"
                                        "_Enviada como arquivo para preservar a resolução._",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                else:
                    with open(file_path, "rb") as f:
                        await update.message.reply_document(
                            document=f,
                            caption=f"✅ Arquivo em qualidade original! ({size_mb:.1f} MB)",
                        )

            await status_msg.delete()

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            logger.error(f"DownloadError para {url}: {error_msg}")

            keywords_login = ["login", "authentication", "cookie", "credentials", "rate-limit", "not available"]
            keywords_private = ["private", "Private"]
            keywords_notfound = ["not found", "404", "does not exist"]

            if any(k in error_msg for k in keywords_private):
                msg = "🔒 Esse conteúdo é *privado* e não pode ser baixado."
            elif any(k in error_msg.lower() for k in keywords_notfound):
                msg = "❌ Conteúdo *não encontrado*. O link pode ter sido removido."
            elif any(k in error_msg.lower() for k in keywords_login):
                msg = (
                    "🔐 *O Instagram bloqueou o download.*\n\n"
                    "O cookies.txt pode estar desatualizado.\n"
                    "Exporte um novo arquivo de cookies do Instagram e "
                    "atualize o arquivo `cookies.txt` no seu repositório GitHub."
                )
            else:
                msg = f"❌ Erro ao baixar:\n`{error_msg[:300]}`"

            await status_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.exception(f"Erro inesperado para {url}")
            await status_msg.edit_text(
                f"❌ Ocorreu um erro inesperado:\n`{str(e)[:300]}`",
                parse_mode=ParseMode.MARKDOWN,
            )


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "SEU_TOKEN_AQUI":
        raise ValueError("❌ Configure a variável de ambiente TELEGRAM_BOT_TOKEN")

    logger.info(f"Cookies do Instagram: {'encontrado ✅' if os.path.isfile(COOKIES_FILE) else 'NÃO encontrado ❌'}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot iniciado!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
