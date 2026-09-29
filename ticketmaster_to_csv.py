import argparse
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 100
DEEP_PAGING_LIMIT = 1000


def parse_args():
    parser = argparse.ArgumentParser(
        description="Descarga eventos de Ticketmaster Discovery API y los guarda en CSV."
    )

    parser.add_argument(
        "--country",
        default="ES",
        help="Código ISO del país. Por defecto: ES",
    )

    parser.add_argument(
        "--classification",
        default="music",
        help="Clasificación de Ticketmaster. Por defecto: music",
    )

    parser.add_argument(
        "--output",
        default="ticketmaster_music_es.csv",
        help="Ruta del CSV de salida.",
    )

    parser.add_argument(
        "--start-date",
        help="Fecha inicial YYYY-MM-DD. Por defecto: hoy (UTC).",
    )

    parser.add_argument(
        "--end-date",
        help="Fecha final YYYY-MM-DD. Por defecto: dos años desde la fecha inicial.",
    )

    args = parser.parse_args()
    try:
        start, end = resolve_date_range(args.start_date, args.end_date)
    except ValueError as exc:
        parser.error(str(exc))
    args.start_date = start.strftime("%Y-%m-%d")
    args.end_date = end.strftime("%Y-%m-%d")
    return args


def resolve_date_range(start_date=None, end_date=None):
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None,
        )
        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            # El 29 de febrero termina el 28 si el año de destino no es bisiesto.
            day = 28 if start.month == 2 and start.day == 29 else start.day
            end = start.replace(year=start.year + 2, day=day)
        end = end.replace(hour=23, minute=59, second=59)
    except ValueError:
        raise ValueError("Las fechas deben ser válidas y tener el formato YYYY-MM-DD.") from None
    if start > end:
        raise ValueError("--start-date no puede ser posterior a --end-date.")
    return start, end


def iso_start(date_str):
    if not date_str:
        return None

    date = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{date:%Y-%m-%d}T00:00:00Z"


def iso_end(date_str):
    if not date_str:
        return None

    date = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{date:%Y-%m-%d}T23:59:59Z"


def first(items):
    return next((item for item in (items or []) if isinstance(item, dict)), {})


def get_primary_classification(event):
    classifications = [c for c in (event.get("classifications") or []) if isinstance(c, dict)]

    if not classifications:
        return {}

    for classification in classifications:
        if classification.get("primary"):
            return classification

    return classifications[0]


def get_best_image(event):
    images = [
        image
        for image in (event.get("images") or [])
        if isinstance(image, dict) and image.get("url")
    ]

    if not images:
        return None

    best = max(
        images,
        key=lambda image:
            (image.get("width") or 0) * (image.get("height") or 0),
    )

    return best.get("url")


def parse_event(event):
    embedded = event.get("_embedded") or {}

    attractions = [a for a in (embedded.get("attractions") or []) if isinstance(a, dict)]

    artist_names = [
        artist.get("name")
        for artist in attractions
        if artist.get("name")
    ]

    artist_ids = [
        artist.get("id")
        for artist in attractions
        if artist.get("id")
    ]

    classification = get_primary_classification(event)

    segment = classification.get("segment") or {}
    genre = classification.get("genre") or {}
    subgenre = classification.get("subGenre") or {}

    venue = first(embedded.get("venues", []))

    city = venue.get("city") or {}
    country = venue.get("country") or {}
    state = venue.get("state") or {}
    location = venue.get("location") or {}
    address = venue.get("address") or {}

    dates = event.get("dates") or {}
    start = dates.get("start") or {}
    status = dates.get("status") or {}

    price = first(event.get("priceRanges", []))

    return {
        "event_id": event.get("id"),
        "event_name": event.get("name"),

        "artist_ids": "; ".join(artist_ids),
        "artists": "; ".join(artist_names),

        "segment": segment.get("name"),
        "genre": genre.get("name"),
        "subgenre": subgenre.get("name"),

        "date": start.get("localDate"),
        "time": start.get("localTime"),
        "datetime_utc": start.get("dateTime"),
        "timezone": dates.get("timezone"),

        "status": status.get("code"),

        "venue_id": venue.get("id"),
        "venue": venue.get("name"),
        "address": ", ".join(
            address[line] for line in ("line1", "line2", "line3") if address.get(line)
        ),
        "postal_code": venue.get("postalCode"),
        "city": city.get("name"),
        "state": state.get("name"),
        "country": country.get("name"),

        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),

        "min_price": price.get("min"),
        "max_price": price.get("max"),
        "currency": price.get("currency"),

        "info": event.get("info"),
        "please_note": event.get("pleaseNote"),

        "image_url": get_best_image(event),
        "ticketmaster_url": event.get("url"),
    }


def request_events_page(session, params):
    # Como máximo cinco peticiones por segundo, además de los reintentos.
    time.sleep(0.21)
    try:
        response = session.get(BASE_URL, params=params, timeout=30)
    except requests.RequestException:
        # Las excepciones de requests pueden contener la URL con la clave.
        raise RuntimeError(
            "No se pudo conectar con Ticketmaster. Revisa la conexión e inténtalo de nuevo."
        ) from None
    if not response.ok:
        hints = {
            400: "Revisa los filtros de búsqueda.",
            401: "Revisa TICKETMASTER_API_KEY (Consumer Key).",
            403: "Comprueba los permisos de tu clave de Ticketmaster.",
            429: "Se ha alcanzado el límite de consultas. Inténtalo más tarde.",
        }
        raise RuntimeError(
            f"Ticketmaster devolvió HTTP {response.status_code}. "
            + hints.get(response.status_code, "Inténtalo de nuevo más tarde.")
        )
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Ticketmaster no devolvió un JSON válido.") from None
    if not isinstance(data, dict):
        raise RuntimeError("La respuesta de Ticketmaster no tiene el formato esperado.")
    return data


def download_events(
    api_key,
    country,
    classification,
    start_date=None,
    end_date=None,
):
    start, end = resolve_date_range(start_date, end_date)
    base_params = {
        "apikey": api_key,
        "countryCode": country,
        "classificationName": classification,
        "size": PAGE_SIZE,
        "includeTBA": "no",
        "includeTBD": "no",
    }
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retry))

        def split_window(window_start, window_end):
            seconds = int((window_end - window_start).total_seconds())
            if seconds == 0:
                raise RuntimeError(
                    "Hay más de 1000 eventos en el mismo segundo. "
                    "Se necesitan filtros más específicos; no se guardará un CSV incompleto."
                )
            midpoint = window_start + timedelta(seconds=seconds // 2)
            print("Más de 1000 resultados: dividiendo el intervalo...")
            return (
                download_window(window_start, midpoint)
                + download_window(midpoint + timedelta(seconds=1), window_end)
            )

        def download_window(window_start, window_end):
            params = {
                **base_params,
                "startDateTime": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            print(f"Intervalo: {params['startDateTime']} — {params['endDateTime']}")
            rows = []
            max_pages = DEEP_PAGING_LIMIT // PAGE_SIZE
            for page in range(max_pages):
                data = request_events_page(session, {**params, "page": page})
                page_info = data.get("page") or {}
                total_elements = page_info.get("totalElements")
                total_pages = page_info.get("totalPages")
                if ((total_elements is not None and total_elements > DEEP_PAGING_LIMIT)
                        or (total_pages is not None and total_pages > max_pages)):
                    # Se descartan las páginas parciales antes de descargar las mitades.
                    return split_window(window_start, window_end)
                events = (data.get("_embedded") or {}).get("events") or []
                if not events:
                    if total_elements is not None and len(rows) < total_elements:
                        raise RuntimeError("Ticketmaster devolvió una página vacía antes de completar la consulta.")
                    return rows
                rows.extend(parse_event(event) for event in events)
                print(f"  Página {page + 1}: {len(rows)} / {total_elements if total_elements is not None else '?'}")
                if total_pages is not None and page + 1 >= total_pages:
                    return rows
                if total_pages is None:
                    if total_elements is not None and len(rows) >= total_elements:
                        return rows
                    if len(events) < PAGE_SIZE:
                        return rows
            # Si faltan metadatos, también se divide al alcanzar el límite.
            return split_window(window_start, window_end)

        return download_window(start, end)


def save_csv(rows, output):
    # Mantiene las cabeceras incluso cuando la consulta no devuelve eventos.
    df = pd.DataFrame(rows, columns=list(parse_event({})))
    df = (
        df.drop_duplicates(subset=["event_id"])
        .sort_values(by=["date", "time"], na_position="last")
        .reset_index(drop=True)
    )
    columns = {
        "event_name": "nombre_concierto",
        "artists": "artista",
        "genre": "genero",
        "date": "fecha",
        "venue": "recinto",
        "address": "direccion",
        "postal_code": "codigo_postal",
        "city": "ciudad",
        "state": "provincia",
        "country": "pais",
        "latitude": "latitud",
        "longitude": "longitud",
    }
    df = df[list(columns)].rename(columns=columns)
    # Una línea por concierto, sin saltos de línea ni espacios repetidos.
    for column in df.columns:
        df[column] = df[column].fillna("").map(lambda value: " ".join(str(value).split()))
    # Distintos IDs u horarios pueden producir la misma fila en el CSV final.
    df = df.drop_duplicates().reset_index(drop=True)
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output, index=False, encoding="utf-8-sig")
    return df


def main():
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path)

    args = parse_args()

    api_key = (os.getenv("TICKETMASTER_API_KEY") or "").strip()

    if not api_key:
        raise RuntimeError(
            "No se encontró TICKETMASTER_API_KEY. "
            "Copia .env.example a .env y añade tu Consumer Key."
        )

    rows = download_events(
        api_key=api_key,
        country=args.country.upper(),
        classification=args.classification,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    if not rows:
        print("No se encontraron eventos; se creará un CSV con las cabeceras.")
    df = save_csv(rows, args.output)

    print()
    print(f"Dataset creado: {args.output}")
    print(f"Número de eventos únicos: {len(df)}")

    print()
    print("Primeras filas:")
    print(
        df.head().to_string(index=False)
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
