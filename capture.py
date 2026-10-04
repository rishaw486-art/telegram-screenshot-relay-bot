from __future__ import annotations

import html
import ipaddress
import json
import shutil
import socket
import subprocess
import tempfile
import tarfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pymupdf as fitz
from PIL import Image
from playwright.async_api import async_playwright


class CaptureError(Exception):
    pass


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def _public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CaptureError("Please send a valid http:// or https:// website link.")
    if parsed.username or parsed.password:
        raise CaptureError(
            "Website links containing embedded credentials are not supported."
        )
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise CaptureError("That host is not available for screenshot capture.")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        }
    except (OSError, ValueError) as exc:
        raise CaptureError("The website host could not be resolved.") from exc
    if not addresses:
        raise CaptureError("The website host could not be resolved.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise CaptureError("Private or local network addresses cannot be captured.")
    return url


def _host_is_public(host: str) -> bool:
    host = (host or "").rstrip(".").lower()
    if (
        not host
        or host in {"localhost", "metadata.google.internal"}
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
        return bool(addresses) and all(
            ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addresses
        )
    except (OSError, ValueError):
        return False


def _telegram_webapp_bridge_script(url: str) -> str | None:
    """Expose Telegram's launch data and browser bridge before app scripts run."""
    fragment = parse_qs(urlparse(url).fragment, keep_blank_values=True)
    encoded_data = fragment.get("tgWebAppData", [None])[0]
    if not encoded_data:
        return None
    # parse_qs has already decoded the outer tgWebAppData value. Do not
    # unquote again: percent-encoding inside initData is part of its signed
    # representation and must remain byte-for-byte intact.
    init_data = encoded_data
    values = parse_qs(init_data, keep_blank_values=True)
    init_data_unsafe: dict[str, object] = {}
    for key, items in values.items():
        value: object = items[-1] if items else ""
        if key == "user":
            try:
                value = json.loads(str(value))
            except (TypeError, ValueError):
                pass
        init_data_unsafe[key] = value

    init_data_json = json.dumps(init_data, ensure_ascii=False)
    unsafe_json = json.dumps(init_data_unsafe, ensure_ascii=False)
    version_json = json.dumps(fragment.get("tgWebAppVersion", ["7.10"])[0])
    platform_json = json.dumps(fragment.get("tgWebAppPlatform", ["web"])[0])
    return f"""
(() => {{
  const initData = {init_data_json};
  const initDataUnsafe = {unsafe_json};
  const listeners = new Map();
  const emit = (event, ...args) => (listeners.get(event) || []).forEach(fn => fn(...args));
  const onEvent = (event, callback) => {{
    if (typeof callback !== 'function') return;
    const callbacks = listeners.get(event) || [];
    callbacks.push(callback); listeners.set(event, callbacks);
  }};
  const offEvent = (event, callback) => listeners.set(event, (listeners.get(event) || []).filter(fn => fn !== callback));
  const mainButton = {{
    isVisible: false, isActive: false, isProgressVisible: false, text: '', color: '#2481cc', textColor: '#ffffff',
    setText(text) {{ this.text = String(text); return this; }}, show() {{ this.isVisible = true; return this; }}, hide() {{ this.isVisible = false; return this; }},
    enable() {{ this.isActive = true; return this; }}, disable() {{ this.isActive = false; return this; }}, showProgress() {{ this.isProgressVisible = true; return this; }}, hideProgress() {{ this.isProgressVisible = false; return this; }},
    onClick(callback) {{ onEvent('mainButtonClicked', callback); return this; }}, offClick(callback) {{ offEvent('mainButtonClicked', callback); return this; }}
  }};
  const webApp = {{
    initData, initDataUnsafe, version: {version_json}, platform: {platform_json}, colorScheme: 'light', themeParams: {{}}, isExpanded: true,
    isClosingConfirmationEnabled: false, viewportHeight: 900, viewportStableHeight: 900, headerColor: '#ffffff', backgroundColor: '#ffffff',
    isVersionAtLeast: () => true, ready: () => emit('ready'), expand: () => {{}}, close: () => {{}},
    enableClosingConfirmation: () => {{}}, disableClosingConfirmation: () => {{}}, onEvent, offEvent, sendData: () => {{}}, MainButton: mainButton,
    BackButton: {{ isVisible: false, show() {{ this.isVisible = true; return this; }}, hide() {{ this.isVisible = false; return this; }}, onClick(callback) {{ onEvent('backButtonClicked', callback); return this; }}, offClick(callback) {{ offEvent('backButtonClicked', callback); return this; }} }},
    HapticFeedback: {{ impactOccurred: () => {{}}, notificationOccurred: () => {{}}, selectionChanged: () => {{}} }},
    CloudStorage: {{ getItem: (_key, callback) => callback && callback(null, ''), setItem: (_key, _value, callback) => callback && callback(null, true) }}
  }};
  window.Telegram = window.Telegram || {{}};
  window.Telegram.WebApp = webApp;
  const webView = window.Telegram.WebView || {{}};
  // The official telegram-web-app.js runtime calls these lower-level bridge
  // methods while it initializes WebApp. They are intentionally no-ops for a
  // read-only screenshot, but must exist so app initialization does not throw.
  webView.postEvent = webView.postEvent || (() => {{}});
  webView.receiveEvent = webView.receiveEvent || ((event, data) => emit(event, data));
  webView.callStorageMethod = webView.callStorageMethod || (() => {{}});
  window.Telegram.WebView = webView;
}})();
"""


async def capture_website_apiflash(
    url: str,
    api_key: str | None,
    timeout_seconds: int,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    """Capture a public website through ApiFlash, returning a bounded PNG image."""
    if not api_key:
        raise CaptureError(
            "Website screenshots are not configured yet. The owner must set APIFLASH_API_KEY."
        )
    url = _public_http_url(url)
    endpoint = "https://api.apiflash.com/v1/urltoimage"
    params = {
        "access_key": api_key,
        "url": url,
        "full_page": "true",
        "format": "png",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(timeout_seconds), connect=min(10.0, float(timeout_seconds))
            )
        ) as client:
            async with client.stream("POST", endpoint, data=params) as response:
                if response.status_code != 200:
                    messages = {
                        400: "ApiFlash could not capture this website URL.",
                        401: "ApiFlash rejected its API key; the owner should check APIFLASH_API_KEY.",
                        402: "The ApiFlash screenshot quota has been exhausted.",
                        403: "The configured ApiFlash plan does not allow this capture request.",
                        429: "ApiFlash is rate-limiting requests. Please try again later.",
                    }
                    raise CaptureError(
                        messages.get(
                            response.status_code,
                            f"ApiFlash screenshot failed (HTTP {response.status_code}).",
                        )
                    )

                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type != "image/png":
                    raise CaptureError(
                        "ApiFlash returned an unexpected response instead of a PNG screenshot."
                    )
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > max_bytes:
                    raise CaptureError(
                        "The screenshot is too large to send through Telegram."
                    )

                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise CaptureError(
                            "The screenshot is too large to send through Telegram."
                        )
                    chunks.append(chunk)
                image = b"".join(chunks)
                if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise CaptureError("ApiFlash returned an invalid PNG screenshot.")
    except CaptureError:
        raise
    except httpx.TimeoutException as exc:
        raise CaptureError("ApiFlash timed out while capturing this website.") from exc
    except httpx.HTTPError as exc:
        raise CaptureError(
            f"Could not reach the ApiFlash screenshot service ({type(exc).__name__})."
        ) from exc

    hostname = urlparse(url).hostname or "Website"
    return image, hostname[:250], "Website screenshot captured with ApiFlash."


async def capture_web(
    url: str,
    timeout_seconds: int,
    max_bytes: int,
    *,
    strict_origin: bool = False,
) -> tuple[bytes, str, str]:
    """Capture a page; strict_origin prevents the initial authenticated URL redirecting elsewhere."""
    url = _public_http_url(url)
    expected_origin = _origin(url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1365, "height": 900}, device_scale_factor=1
        )
        page = await context.new_page()
        bridge_script = _telegram_webapp_bridge_script(url)
        if bridge_script:
            await context.add_init_script(bridge_script)

        async def guard(route):
            request_url = route.request.url
            parsed = urlparse(request_url)
            if parsed.scheme not in {"http", "https"} or not _host_is_public(
                parsed.hostname or ""
            ):
                await route.abort()
                return
            if (
                strict_origin
                and route.request.is_navigation_request
                and _origin(request_url) != expected_origin
            ):
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", guard)
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000
            )
            if strict_origin and _origin(page.url) != expected_origin:
                raise CaptureError(
                    "The authenticated Mini App redirected to a different origin; capture was stopped."
                )
            await page.wait_for_timeout(900)
            title = (await page.title())[:250]
            try:
                description = await page.locator(
                    'meta[name="description"]'
                ).get_attribute("content", timeout=1200)
            except Exception:
                description = None
            if not description:
                try:
                    description = await page.locator(
                        'meta[property="og:description"]'
                    ).get_attribute("content", timeout=1200)
                except Exception:
                    description = None
            try:
                body_text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                body_text = ""
            summary = (description or " ".join(body_text.split()))[:700]
            image = await page.screenshot(
                full_page=True, type="png", animations="disabled"
            )
            if len(image) > max_bytes:
                raise CaptureError(
                    "The screenshot is too large to send through Telegram."
                )
            return (
                image,
                title or "Website",
                summary
                or "Screenshot captured; this page did not provide a readable description.",
            )
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(
                f"Could not capture that website: {type(exc).__name__}."
            ) from exc
        finally:
            await context.close()
            await browser.close()


async def render_file(
    path: Path, mime_type: str | None, max_bytes: int, *, display_name: str | None = None
) -> tuple[bytes, str, str]:
    suffix = path.suffix.lower()
    display_name = display_name or path.name
    if suffix == ".pdf" or mime_type == "application/pdf":
        try:
            document = fitz.open(path)
            if not document.page_count:
                raise CaptureError("The PDF has no pages.")
            page = document.load_page(0)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
            image = pixmap.tobytes("png")
            title = document.metadata.get("title") or display_name
            text = (
                " ".join(page.get_text().split())[:700]
                or "First page of PDF rendered as an image."
            )
            if len(image) > max_bytes:
                raise CaptureError("The rendered PDF page is too large to send.")
            return image, title[:250], text
        except CaptureError as exc:
            if "too large" in str(exc).lower():
                raise
            return await _render_file_metadata(path, mime_type, max_bytes, display_name)
        except Exception:
            return await _render_file_metadata(path, mime_type, max_bytes, display_name)
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"} or (
        mime_type or ""
    ).startswith("image/"):
        try:
            with Image.open(path) as image_source:
                image_source.seek(0)
                width, height = image_source.size
                if width * height > 30_000_000:
                    raise CaptureError(
                        "The image dimensions are too large for safe processing."
                    )
                image = image_source.convert("RGB")
                from io import BytesIO

                buffer = BytesIO()
                image.save(buffer, format="PNG", optimize=True)
                result = buffer.getvalue()
                if len(result) > max_bytes:
                    raise CaptureError(
                        "The image is too large to send after conversion."
                    )
                return result, display_name, f"Image preview, {width} × {height} pixels."
        except CaptureError:
            raise
        except Exception as exc:
            return await _render_file_metadata(path, mime_type, max_bytes, display_name)
    if suffix in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"}:
        try:
            text = _extract_office_text(path, suffix)
            if text.strip():
                return await _render_text_preview(
                    display_name, text[:9000], max_bytes, "Document contents extracted safely."
                )
        except (OSError, zipfile.BadZipFile, ET.ParseError, RuntimeError, ValueError):
            # Malformed, encrypted, or over-sized containers fall through to a safe metadata card.
            pass
    if suffix in {".zip", ".epub", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".rar", ".7z"}:
        try:
            names = _archive_listing(path)
            summary = "Archive contents (names only; no files were extracted):\n" + "\n".join(names)
            return await _render_text_preview(
                display_name, summary[:9000], max_bytes, "Archive listing; contents were not extracted."
            )
        except (OSError, zipfile.BadZipFile, tarfile.ReadError, RuntimeError):
            pass

    if _looks_like_text(path, suffix, mime_type):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:9000]
            return await _render_text_preview(
                display_name, text, max_bytes, "Text preview rendered safely."
            )
        except OSError:
            pass

    return await _render_file_metadata(path, mime_type, max_bytes, display_name)


def _looks_like_text(path: Path, suffix: str, mime_type: str | None) -> bool:
    if suffix in {
        ".exe", ".dll", ".so", ".dylib", ".bin", ".apk", ".app", ".msi",
        ".deb", ".rpm", ".class", ".pyc", ".wasm", ".iso", ".dmg",
        ".tgs", ".webm", ".mp4", ".mkv", ".mov", ".mp3", ".m4a", ".ogg",
        ".opus", ".wav", ".flac", ".aac", ".amr", ".avi", ".heic", ".heif",
    }:
        return False
    if suffix in {
        ".txt", ".md", ".csv", ".log", ".json", ".jsonl", ".xml", ".html",
        ".htm", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".py", ".js",
        ".ts", ".css", ".sql", ".sh", ".toml", ".rst", ".tex", ".eml",
    } or (mime_type or "").startswith("text/"):
        return True
    try:
        with path.open("rb") as file:
            return b"\x00" not in file.read(4096)
    except OSError:
        return False


def _extract_office_text(path: Path, suffix: str) -> str:
    """Extract bounded textual XML from OpenXML/OpenDocument/EPUB ZIP containers."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 5000:
            raise ValueError("Container has too many entries")
        output: list[str] = []
        total = 0

        def xml_text(member: str) -> str:
            nonlocal total
            info = archive.getinfo(member)
            if info.file_size > 2_000_000 or total + info.file_size > 6_000_000:
                return ""
            with archive.open(info) as stream:
                raw = stream.read(2_000_001)
            total += len(raw)
            root = ET.fromstring(raw)
            return " ".join(
                value.strip()
                for element in root.iter()
                for value in [element.text or ""]
                if value.strip()
            )

        if suffix == ".docx":
            members = ["word/document.xml"]
        elif suffix == ".pptx":
            members = sorted(
                (name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")),
                key=lambda name: int(Path(name).stem.removeprefix("slide")) if Path(name).stem.removeprefix("slide").isdigit() else 0,
            )[:30]
        elif suffix == ".xlsx":
            members = ["xl/sharedStrings.xml"] + sorted(
                name for name in archive.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )[:20]
        elif suffix in {".odt", ".ods", ".odp"}:
            members = ["content.xml"]
        else:
            members = sorted(
                name for name in archive.namelist()
                if name.lower().endswith((".html", ".xhtml", ".xml", ".txt"))
            )[:20]
        for member in members:
            if member in archive.namelist():
                extracted = xml_text(member)
                if extracted:
                    output.append(extracted)
        return "\n".join(output)[:9000]


def _archive_listing(path: Path) -> list[str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 5000:
                return [f"Archive contains {len(infos)} entries; listing capped for safety."]
            return [
                f"{info.filename[:180]} ({info.file_size:,} bytes)"
                for info in infos[:100]
            ] or ["The archive is empty."]
    if tarfile.is_tarfile(path):
        with tarfile.open(path, mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > 5000:
                return [f"Archive contains {len(members)} entries; listing capped for safety."]
            return [
                f"{member.name[:180]} ({member.size:,} bytes)"
                for member in members[:100]
            ] or ["The archive is empty."]
    archive_tool = shutil.which("7z") or shutil.which("7zz")
    if archive_tool:
        try:
            result = subprocess.run(
                [archive_tool, "l", "-slt", "-bd", "--", str(path)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise tarfile.ReadError("Archive listing timed out or failed") from exc
        if result.returncode == 0:
            entries: list[str] = []
            current_name: str | None = None
            current_size: str | None = None
            for line in result.stdout.splitlines():
                if line.startswith("Path = "):
                    if current_name and current_name not in {str(path), "[Content]"}:
                        entries.append(f"{current_name[:180]} ({current_size or 'unknown'} bytes)")
                    current_name = line[7:]
                    current_size = None
                elif line.startswith("Size = "):
                    current_size = line[7:]
            if current_name and current_name not in {str(path), "[Content]"}:
                entries.append(f"{current_name[:180]} ({current_size or 'unknown'} bytes)")
            if entries:
                return entries[:100]
            return ["The archive is empty."]
    raise tarfile.ReadError("Unsupported or malformed archive")


async def _render_text_preview(
    title: str, text: str, max_bytes: int, description: str
) -> tuple[bytes, str, str]:
    document = (
        "<html><body style='margin:24px;font:15px monospace;white-space:pre-wrap;"
        "overflow-wrap:anywhere'>" + html.escape(text) + "</body></html>"
    )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        temp_png = Path(tmp.name)
    try:
        await _render_html(document, temp_png)
        data = temp_png.read_bytes()
    finally:
        temp_png.unlink(missing_ok=True)
    if len(data) > max_bytes:
        raise CaptureError("The rendered file is too large to send.")
    return data, title[:250], description


async def _render_file_metadata(
    path: Path, mime_type: str | None, max_bytes: int, display_name: str
) -> tuple[bytes, str, str]:
    size = path.stat().st_size
    with path.open("rb") as file:
        signature = file.read(16)
    signatures = (
        (b"%PDF-", "PDF document"),
        (b"\x89PNG\r\n\x1a\n", "PNG image"),
        (b"\xff\xd8\xff", "JPEG image"),
        (b"GIF87a", "GIF image"),
        (b"GIF89a", "GIF image"),
        (b"PK\x03\x04", "ZIP-based container"),
        (b"\x1f\x8b", "GZIP archive"),
        (b"Rar!\x1a\x07", "RAR archive"),
        (b"7z\xbc\xaf\x27\x1c", "7z archive"),
        (b"MZ", "Windows executable format (not executed)"),
        (b"\x7fELF", "Linux executable format (not executed)"),
        (b"ID3", "MP3 audio"),
    )
    detected = next((label for prefix, label in signatures if signature.startswith(prefix)), None)
    if not detected and len(signature) >= 12 and signature[4:8] == b"ftyp":
        detected = "MP4-family media"
    report = (
        f"File: {display_name}\n"
        f"Reported MIME type: {mime_type or 'unknown'}\n"
        f"Detected type: {detected or 'unrecognized binary or specialized format'}\n"
        f"Size: {size:,} bytes\n\n"
        "This format is not text-extracted by the safe previewer. The file was not executed, opened as a program, or extracted."
    )
    return await _render_text_preview(
        display_name, report, max_bytes, "Safe file information preview; contents were not executed."
    )


async def _render_html(source: str, output_path: Path) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1100, "height": 800})
            await page.set_content(source, wait_until="domcontentloaded")
            await page.screenshot(path=str(output_path), full_page=True, type="png")
        finally:
            await browser.close()
