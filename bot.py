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
    """Extrai o primeiro link do Instagram ou TikTok do texto."""
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def is_supported_url(url: str) -> bool:
    return any(domain in url.lower() for domain in SUPPORTED_DOMAINS)


def get_ydl_opts(output_path: str) -> dict:
    return {
        "outtmpl": output_path,

        # ── Qualidade máxima ────────────────────────────────────────────────────
        # Prioridade: melhor vídeo + melhor áudio → merge em mp4
        # Fallbacks progressivos caso o merge não esteja disponível
        "format": (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]"
            "/best"
        ),
        "merge_output_format": "mp4",

        # Prefere a maior resolução disponível (sem limite de height/width)
        "format_sort": [
            "res",       # maior resolução primeiro
            "vbr",       # maior bitrate de vídeo
            "abr",       # maior bitrate de áudio
            "ext:mp4:m4a",  # prefere mp4/m4a nativamente
            "fps",       # maior FPS
        ],

        # ── Pós-processamento ───────────────────────────────────────────────────
        # Garante que imagens sejam salvas na maior qualidade (sem recompressão)
        "postprocessors": [
            {
                # Embute metadados no arquivo final
                "key": "FFmpegMetadata",
                "add_metadata": True,
            }
        ],

        # ── Comportamento ───────────────────────────────────────────────────────
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,  # não baixa thumbnail separada

        # ── Headers para evitar bloqueios ───────────────────────────────────────
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },

        # ── Instagram ───────────────────────────────────────────────────────────
        "extractor_args": {
            "instagram": {"api": ["graphql"]},
        },

        # ── Limites e timeouts ──────────────────────────────────────────────────
        # Sem limite de filesize aqui — validamos depois antes de enviar
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "continuedl": True,   # retoma downloads interrompidos
    }


async def download_media(url: str, output_dir: str) -> list[str]:
    """
    Faz o download do vídeo/foto e retorna a lista de arquivos baixados.
    Roda em thread separada para não bloquear o event loop.
    """
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    opts = get_ydl_opts(output_template)

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
    await update.message.reply_text(
        "👋 *Olá! Sou o bot de downloads.*\n\n"
        "📲 Me envie um link do *Instagram* ou *TikTok* e eu faço o download do vídeo ou foto pra você!\n\n"
        "✅ *Suportado:*\n"
        "• Instagram — Reels, Posts, Stories\n"
        "• TikTok — Vídeos\n\n"
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

            TELEGRAM_LIMIT = 50 * 1024 * 1024   # 50 MB — limite padrão de bots
            PHOTO_LIMIT    = 10 * 1024 * 1024   # 10 MB — limite de reply_photo

            for file_path in files:
                file_size = os.path.getsize(file_path)
                ext = Path(file_path).suffix.lower()
                size_mb = file_size / (1024 * 1024)

                if file_size > TELEGRAM_LIMIT:
                    await update.message.reply_text(
                        f"⚠️ O arquivo baixado tem *{size_mb:.1f} MB* — acima do limite de 50 MB do Telegram.\n\n"
                        "O vídeo foi baixado na *máxima qualidade disponível*, mas não é possível enviar pelo bot.\n"
                        "Tente um vídeo mais curto ou de menor resolução.",
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
                            # Acima de 10 MB: envia como documento para preservar qualidade total
                            await update.message.reply_document(
                                document=f,
                                caption=f"✅ Foto em qualidade original! ({size_mb:.1f} MB)\n"
                                        "_Enviada como arquivo para preservar a resolução completa._",
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

            if "Private" in error_msg or "private" in error_msg:
                msg = "🔒 Esse conteúdo é *privado* e não pode ser baixado."
            elif "not found" in error_msg.lower() or "404" in error_msg:
                msg = "❌ Conteúdo *não encontrado*. O link pode ter sido removido."
            elif "login" in error_msg.lower() or "authentication" in error_msg.lower():
                msg = (
                    "🔐 Esse conteúdo exige *login*.\n\n"
                    "Configure as cookies do Instagram nas variáveis de ambiente "
                    "(veja o README para instruções)."
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
        raise ValueError(
            "❌ Configure o token do bot!\n"
            "Defina a variável de ambiente TELEGRAM_BOT_TOKEN ou edite o arquivo bot.py"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot iniciado! Pressione Ctrl+C para parar.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
