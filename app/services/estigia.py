from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

ESTIGIA_HOST = "estigia.ypfb.gob.bo"
ESTIGIA_ROUTE = "/ordenDespachoVerify/index/"
ESTIGIA_REQUEST_TIMEOUT_SECONDS = 10.0
ESTIGIA_MAX_ATTEMPTS = 2
ESTIGIA_SOURCE_TIMEZONE = ZoneInfo("America/La_Paz")

type EstigiaValue = str | int | float
type EstigiaData = dict[str, dict[str, EstigiaValue]]


class EstigiaExtractionError(Exception):
    """An expected error while fetching or parsing an Estigia order page."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


async def extract_information_from_estigia_orden_despacho(
    estigia_orden_despacho_url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> EstigiaData:
    """Fetch and extract billing and dispatch data from an Estigia order URL."""
    _validate_estigia_url(estigia_orden_despacho_url)

    if client is None:
        async with httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": "sigoper-companion-service"},
            timeout=ESTIGIA_REQUEST_TIMEOUT_SECONDS,
        ) as http_client:
            html = await _fetch_estigia_page(http_client, estigia_orden_despacho_url)
    else:
        html = await _fetch_estigia_page(client, estigia_orden_despacho_url)

    return _parse_estigia_page(html)


async def _fetch_estigia_page(client: httpx.AsyncClient, url: str) -> str:
    for attempt in range(ESTIGIA_MAX_ATTEMPTS):
        try:
            response = await client.get(url)
        except httpx.TimeoutException as error:
            if attempt + 1 < ESTIGIA_MAX_ATTEMPTS:
                continue
            raise EstigiaExtractionError(
                "ESTIGIA_REQUEST_TIMEOUT",
                "The Estigia order page request timed out.",
            ) from error
        except httpx.RequestError as error:
            if attempt + 1 < ESTIGIA_MAX_ATTEMPTS:
                continue
            raise EstigiaExtractionError(
                "ESTIGIA_REQUEST_ERROR",
                "The Estigia order page could not be requested.",
            ) from error

        if 500 <= response.status_code < 600 and attempt + 1 < ESTIGIA_MAX_ATTEMPTS:
            continue

        if not 200 <= response.status_code < 300:
            raise EstigiaExtractionError(
                "ESTIGIA_HTTP_ERROR",
                "The Estigia order page returned an HTTP error.",
            )

        return response.text

    raise EstigiaExtractionError(
        "ESTIGIA_REQUEST_ERROR",
        "The Estigia order page could not be requested.",
    )


def _validate_estigia_url(url: str) -> None:
    if not isinstance(url, str) or url.strip() != url:
        raise EstigiaExtractionError(
            "INVALID_ESTIGIA_URL",
            "The URL must point to an official Estigia order page.",
        )

    try:
        parsed_url = urlsplit(url)
        port = parsed_url.port
    except ValueError as error:
        raise EstigiaExtractionError(
            "INVALID_ESTIGIA_URL",
            "The URL must point to an official Estigia order page.",
        ) from error

    token_pattern = re.escape(ESTIGIA_ROUTE) + r"[0-9A-Fa-f]+/?"
    is_valid = (
        parsed_url.scheme == "https"
        and parsed_url.hostname == ESTIGIA_HOST
        and port is None
        and parsed_url.username is None
        and parsed_url.password is None
        and not parsed_url.query
        and not parsed_url.fragment
        and re.fullmatch(token_pattern, parsed_url.path) is not None
    )
    if not is_valid:
        raise EstigiaExtractionError(
            "INVALID_ESTIGIA_URL",
            "The URL must point to an official Estigia order page.",
        )


def _parse_estigia_page(html: str) -> EstigiaData:
    try:
        soup = BeautifulSoup(html, "html.parser")
        billing_rows = _extract_section_rows(soup, "Datos de Facturación")
        dispatch_rows = _extract_section_rows(soup, "Datos de Despacho")

        invoice_number, invoice_status = _required_row(
            billing_rows,
            "Número de Factura",
        )
        if not invoice_status:
            raise ValueError("missing invoice status")

        document_type, document_number = _parse_document(
            _required_row(billing_rows, "Tipo - Número de Documento")[0]
        )
        driver, license_number = _parse_driver(
            _required_row(dispatch_rows, "Conductor")[0]
        )
        quantity, quantity_unit = _parse_quantity(
            _required_row(dispatch_rows, "Cantidad")[0]
        )

        return {
            "facturacion": {
                "numero_factura": invoice_number,
                "estado_factura": invoice_status,
                "cuf": _required_row(billing_rows, "CUF")[0],
                "fecha_emision": _parse_datetime(
                    _required_row(billing_rows, "Fecha de Emisión")[0]
                ),
                "nombre_razon_social": _required_row(
                    billing_rows,
                    "Nombre o Razón Social",
                )[0],
                "tipo_documento": document_type,
                "numero_documento": document_number,
                "codigo_sirehidro": _required_row(
                    billing_rows,
                    "Código Sirehidro",
                )[0],
            },
            "despacho": {
                "numero": _required_row(dispatch_rows, "Número")[0],
                "fecha_despacho_programado": _parse_date(
                    _required_row(
                        dispatch_rows,
                        "Fecha de Despacho Programado",
                    )[0]
                ),
                "fecha_despacho_efectivo": _parse_date(
                    _required_row(
                        dispatch_rows,
                        "Fecha de Despacho Efectivo",
                    )[0]
                ),
                "planta_despacho": _required_row(
                    dispatch_rows,
                    "Planta de Despacho",
                )[0],
                "conductor": driver,
                "licencia": license_number,
                "placa": _required_row(dispatch_rows, "Placa")[0],
                "producto": _required_row(dispatch_rows, "Producto")[0],
                "cantidad": quantity,
                "unidad_cantidad": quantity_unit,
            },
        }
    except (AttributeError, TypeError, ValueError, InvalidOperation) as error:
        raise EstigiaExtractionError(
            "ESTIGIA_PAGE_PARSE_ERROR",
            "The Estigia order page could not be parsed.",
        ) from error


def _extract_section_rows(
    soup: BeautifulSoup,
    title: str,
) -> dict[str, tuple[str, str | None]]:
    heading = soup.find(
        lambda element: (
            isinstance(element, Tag)
            and element.name in {"h1", "h2", "h3", "h4"}
            and _clean_text(element.get_text(" ", strip=True)) == title
        )
    )
    if not isinstance(heading, Tag) or not isinstance(heading.parent, Tag):
        raise TypeError(f"missing section: {title}")

    rows: dict[str, tuple[str, str | None]] = {}
    for row in heading.parent.select("div.input"):
        label = row.find("label", class_="name")
        if not isinstance(label, Tag):
            continue

        label_name = _clean_text(label.get_text(" ", strip=True))
        label.extract()

        badge = row.find(class_="badge-success")
        status = None
        if isinstance(badge, Tag):
            status = _clean_text(badge.get_text(" ", strip=True))
            badge.extract()

        value = _clean_text(row.get_text(" ", strip=True))
        rows[label_name] = (value, status)

    return rows


def _required_row(
    rows: dict[str, tuple[str, str | None]],
    label: str,
) -> tuple[str, str | None]:
    value = rows.get(label)
    if value is None or not value[0]:
        raise ValueError(f"missing field: {label}")
    return value


def _parse_document(value: str) -> tuple[str, str]:
    parts = [part.strip() for part in re.split(r"\s+-\s+", value) if part.strip()]
    if len(parts) < 3 or not parts[0] or not parts[-1]:
        raise ValueError("invalid document value")
    return parts[0], parts[-1]


def _parse_driver(value: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"(?P<driver>.+?)\s+Licencia\s*:\s*(?P<license>.+)",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("invalid driver value")
    return _clean_text(match.group("driver")), _clean_text(match.group("license"))


def _parse_quantity(value: str) -> tuple[int | float, str]:
    match = re.fullmatch(r"(?P<number>[\d.,\s]+)\s+(?P<unit>.+)", value)
    if match is None:
        raise ValueError("invalid quantity value")

    number_text = re.sub(r"\s+", "", match.group("number"))
    unit = _clean_text(match.group("unit")).lower()
    if not unit:
        raise ValueError("missing quantity unit")

    normalized_number = _normalize_number(number_text)
    quantity = Decimal(normalized_number)
    if not quantity.is_finite():
        raise ValueError("invalid quantity number")
    try:
        if quantity == quantity.to_integral_value():
            return int(quantity), unit
        return float(quantity), unit
    except (OverflowError, ValueError) as error:
        raise ValueError("invalid quantity number") from error


def _normalize_number(value: str) -> str:
    if not re.fullmatch(r"[\d.,]+", value):
        raise ValueError("invalid quantity number")

    if "," in value and "." in value:
        decimal_separator = "," if value.rfind(",") > value.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        return value.replace(grouping_separator, "").replace(decimal_separator, ".")

    if "," in value:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", value):
            return value.replace(",", "")
        return value.replace(",", ".")

    if "." in value:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", value):
            return value.replace(".", "")
        return value

    return value


def _parse_datetime(value: str) -> str:
    parsed = datetime.strptime(value, "%d/%m/%Y %H:%M:%S").replace(
        tzinfo=ESTIGIA_SOURCE_TIMEZONE
    )
    utc_value = parsed.astimezone(UTC)
    return utc_value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_date(value: str) -> str:
    try:
        day, month, year = (int(part) for part in value.split("/"))
        return date(year, month, day).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError("invalid date") from error


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
