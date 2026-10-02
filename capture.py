from __future__ import annotations

import html
import ipaddress
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

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
    path: Path, mime_type: str | None, max_bytes: int
) -> tuple[bytes, str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf" or mime_type == "application/pdf":
        try:
            document = fitz.open(path)
            if not document.page_count:
                raise CaptureError("The PDF has no pages.")
            page = document.load_page(0)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
            image = pixmap.tobytes("png")
            title = document.metadata.get("title") or path.name
            text = (
                " ".join(page.get_text().split())[:700]
                or "First page of PDF rendered as an image."
            )
            if len(image) > max_bytes:
                raise CaptureError("The rendered PDF page is too large to send.")
            return image, title[:250], text
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(
                "Could not render this PDF; it may be encrypted or malformed."
            ) from exc
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
                return result, path.name, f"Image preview, {width} × {height} pixels."
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError("Could not open this image file.") from exc
    if suffix in {".txt", ".md", ".csv", ".log"} or mime_type in {
        "text/plain",
        "text/markdown",
        "text/csv",
    }:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:9000]
            document = f"<html><body><pre style='white-space:pre-wrap;font:15px monospace'>{html.escape(text)}</pre></body></html>"
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                temp_png = Path(tmp.name)
            try:
                await _render_html(document, temp_png)
                data = temp_png.read_bytes()
            finally:
                temp_png.unlink(missing_ok=True)
            if len(data) > max_bytes:
                raise CaptureError("The rendered file is too large to send.")
            return data, path.name, "Text-file preview rendered as an image."
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError("Could not render this text file.") from exc
    raise CaptureError(
        "Unsupported file type. MVP supports images, PDFs, and text files; it will not execute files."
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
