# Ticketmaster → CSV

Proyecto para descargar eventos musicales desde la Ticketmaster Discovery API v2
y guardarlos en un CSV.

## 1. Crear el entorno virtual

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Si `venv` no está instalado en Ubuntu/Debian:

```bash
sudo apt update
sudo apt install python3.13-venv
```

## 2. Configurar la API key

Copia el archivo de ejemplo:

```bash
cp -n .env.example .env
```

Edita `.env`:

```bash
nano .env
```

Y añade tu **Consumer Key** de Ticketmaster:

```env
TICKETMASTER_API_KEY=TU_CONSUMER_KEY
```

No uses el Consumer Secret.

## 3. Ejecutar

Por defecto descarga los eventos de música publicados en España para los próximos dos años:

```bash
python ticketmaster_to_csv.py
```

También puedes ejecutarlo sin activar el entorno: `./.venv/bin/python ticketmaster_to_csv.py`.

Generará:

```text
ticketmaster_music_es.csv
```

## Opciones

Cambiar país:

```bash
python ticketmaster_to_csv.py --country PT
```

Cambiar archivo de salida:

```bash
python ticketmaster_to_csv.py --output conciertos.csv
```

Filtrar por fechas:

```bash
python ticketmaster_to_csv.py   --start-date 2026-09-01   --end-date 2026-12-31   --output conciertos_2026.csv
```

## Nota sobre paginación

El script solicita 100 eventos por página: las peticiones de 500 provocaban un error HTTP 400.
Ticketmaster limita el deep paging a los primeros 1000 resultados de una consulta.
El script divide automáticamente el periodo en intervalos más pequeños cuando una consulta
supera ese límite. Descarga las páginas de cada intervalo y reúne los resultados en un único
CSV, eliminando duplicados. No hay un tope global de 1000 eventos.
Si más de 1000 eventos coinciden en el mismo segundo, se detiene con un error en lugar de
guardar un resultado truncado.

Sin opciones de fecha se consulta desde hoy hasta el mismo día de dentro de dos años.
Puedes cambiar el periodo con `--start-date` y `--end-date`.
Solo se incluyen conciertos con fecha definida; los eventos TBA/TBD quedan excluidos.
La cantidad final depende de los eventos que Ticketmaster tenga publicados en ese momento;
una fecha final más lejana no garantiza más resultados.
Las fechas de los filtros se interpretan en UTC, desde las 00:00:00 hasta las 23:59:59.

Referencia: [Discovery API de Ticketmaster](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/).

## Formato del dataset

Filas ordenadas por fecha y hora, sin filas duplicadas. Incluye las siguientes columnas:

- `nombre_concierto`: nombre del evento.
- `artista`: artistas asociados, separados por `; ` cuando hay varios.
- `genero`: género musical indicado por Ticketmaster.
- `fecha`: fecha local del concierto en formato `YYYY-MM-DD`.
- `recinto`: nombre del lugar del concierto.
- `direccion`: líneas de la dirección facilitadas por Ticketmaster.
- `codigo_postal`, `ciudad`, `provincia`, `pais`: ubicación del recinto.
- `latitud`, `longitud`: coordenadas del recinto facilitadas por Ticketmaster.

Se utiliza el primer recinto asociado al evento. No se inventan direcciones ni coordenadas
cuando Ticketmaster no las proporciona; esos campos quedan vacíos.

Los campos ausentes se dejan vacíos. Se eliminan saltos de línea y espacios repetidos.
Los identificadores y la hora se usan internamente para eliminar duplicados y ordenar,
pero no se incluyen en el CSV. Tras limpiar los espacios, se eliminan también las filas
idénticas en todas las columnas exportadas, aunque tengan IDs u horarios distintos.
Un concierto en fechas o ubicaciones diferentes se conserva como filas diferentes.
El CSV usa comas y UTF-8 con BOM para conservar los acentos al abrirlo en Excel.
Si Excel no separa las columnas automáticamente, impórtalo con delimitador coma.
Si no hay eventos se genera un CSV con las cabeceras.
Las carpetas de salida se crean automáticamente; un archivo existente en esa ruta se reemplaza.

El script reintenta los fallos temporales de conexión, los errores HTTP 429 y determinados
errores del servidor. Los mensajes de error no muestran la API key.

## Comprobar el código

Las pruebas no necesitan conexión ni una clave real:

```bash
.venv/bin/python -m unittest -v
```
