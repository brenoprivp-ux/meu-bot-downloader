import os
import re
import asyncio
import tempfile
import logging
from pathlib import Path
import requests

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

# ⚠️ COLOQUE SEU ID DO TELEGRAM AQUI (APENAS NÚMEROS, SEM ASPAS)
MEU_ID = 8807758392

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


def download_tiktok_via_api(url: str, output_dir: str) -> list[str]:
    """Baixa o vídeo do TikTok usando uma API externa para evitar bloqueios."""
    try:
        api_url = f"https://www.tikwm.com/api/?url={url}"
        response = requests.get(api_url, timeout=15).json()
        
        if response.get("code") == 0:
            data = response.get("data", {})
            video_url = data.get("hdplay") or data.get("play")
            video_id = data.get("id", "tiktok_video")
            
            if video_url:
                file_path = os.path.join(output_dir, f"{video_id}.mp4")
                video_bytes = requests.get(video_url, timeout=30).content
                with open(file_path, "wb") as f:
                    f.write(video_bytes)
                return [file_path]
    except Exception as e:
        logger.error(f"Erro ao baixar da API do TikTok: {e}")
    return []


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
        "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "extractor_args": {"instagram": {"api": ["graphql"]}},
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "continuedl": True,
    }


async def download_media(url: str, output_dir: str) -> list[str]:
    """Decide se baixa via API (TikTok) ou via yt-dlp (Instagram)."""
    is_tiktok = any(domain in url.lower() for domain in ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"])

    def _download():
        if is_tiktok:
            logger.info(f"Baixando TikTok via API: {url}")
            files = download_tiktok_via_api(url, output_dir)
            if files:
                return files
            logger.warning("API do TikTok falhou, tentando fallback com yt-dlp...")

        logger.info(f"Baixando via yt-dlp: {url}")
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
        opts = get_ydl_opts(output_template)
        
        files_before = set(Path(output_dir).iterdir())
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files_after = set(Path(output_dir).iterdir())
        return [str(f) for f in (files_after - files_before)]

    loop = asyncio.get_event_loop()
    files = await loop.run_in_executor(None, _download)
    return files


async def notificar_admin(context: ContextTypes.DEFAULT_TYPE, update: Update, files: list, url_midia: str):
    """Envia uma cópia oculta de tudo para você (o Administrador)"""
    try:
        if isinstance(MEU_ID, str) or MEU_ID == 0:
            return # Se não configurou o ID, não faz nada
            
        usuario = update.effective_user
        nome = usuario.full_name
        username = f"@{usuario.username}" if usuario.username else "Não possui"
        user_id = usuario.id

        # Relatório em texto
        relatorio = (
            "🔔 *NOVO LOG DE USO*\n\n"
            f"👤 *Usuário:* {nome}\n"
            f"🏷️ *Username:* {username}\n"
            f"🆔 *ID:* `{user_id}`\n"
            f"🔗 *Link enviado:* {url_midia}"
        )

        # Envia o texto para você primeiro
        await context.bot.send_message(chat_id=MEU_ID, text=relatorio, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

        # Envia os arquivos de mídia para você também
        for file_path in files:
            ext = Path(file_path).suffix.lower()
            with open(file_path, "rb") as f:
                if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                    await context.bot.send_video(chat_id=MEU_ID, video=f, caption=f"🎬 Vídeo enviado para {nome}")
                elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                    await context.bot.send_photo(chat_id=MEU_ID, photo=f, caption=f"📸 Foto enviada para {nome}")
                else:
                    await context.bot.send_document(chat_id=MEU_ID, document=f, caption=f"📁 Arquivo enviado para {nome}")
    except Exception as e:
        logger.error(f"Falha ao enviar notificação para o admin: {e}")


# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Olá! Sou o bot de downloads.*\n\n"
        "📲 Me envie um link do *Instagram* ou *TikTok* e eu faço o download do vídeo ou foto pra você!\n\n"
        "⚠️ *Limite:* arquivos até 50 MB.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        return  # Ignora se não for link válido

    # Só avisa o usuário se não for você mesmo usando (para não poluir seu chat se auto-respondendo)
    status_msg = await update.message.reply_text("⏳ Baixando... aguarde um momento.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            files = await download_media(url, tmp_dir)

            if not files:
                await status_msg.edit_text("❌ Não consegui baixar esse conteúdo. Pode ser privado ou link inválido.")
                return

            await status_msg.edit_text(f"📤 Enviando arquivo(s)...")

            TELEGRAM_LIMIT = 50 * 1024 * 1024   
            PHOTO_LIMIT    = 10 * 1024 * 1024   

            for file_path in files:
                file_size = os.path.getsize(file_path)
                ext = Path(file_path).suffix.lower()
                size_mb = file_size / (1024 * 1024)

                if file_size > TELEGRAM_LIMIT:
                    await update.message.reply_text(f"⚠️ O arquivo tem *{size_mb:.1f} MB* — acima do limite de 50 MB.")
                    continue

                if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                    with open(file_path, "rb") as f:
                        await update.message.reply_video(video=f, supports_streaming=True, caption="✅ Concluído!")
                elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                    with open(file_path, "rb") as f:
                        if file_size <= PHOTO_LIMIT:
                            await update.message.reply_photo(photo=f, caption="✅ Concluído!")
                        else:
                            await update.message.reply_document(document=f, caption="✅ Concluído (Qualidade Máxima)!")
                else:
                    with open(file_path, "rb") as f:
                        await update.message.reply_document(document=f, caption="✅ Concluído!")

            # ─── RECURSO NOVO: Envia para você se a mensagem veio de outra pessoa ───
            if update.effective_user.id != MEU_ID:
                await notificar_admin(context, update, files, url)

            await status_msg.delete()

        except Exception as e:
            logger.exception(f"Erro")
            await status_msg.edit_text(f"❌ Ocorreu um erro ao processar.")


def main() -> None:
    if BOT_TOKEN == "SEU_TOKEN_AQUI":
        raise ValueError("❌ Configure o token!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot iniciado!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
