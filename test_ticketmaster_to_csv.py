import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests
import ticketmaster_to_csv as tm


class TicketmasterTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(tm.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def response(self, data, status=200):
        return Mock(ok=status == 200, status_code=status, json=Mock(return_value=data))

    def test_optional_null_fields(self):
        row = tm.parse_event({
            'id': '1', '_embedded': None, 'dates': None,
            'images': None, 'classifications': None, 'priceRanges': None,
        })
        self.assertEqual(row['event_id'], '1')
        self.assertEqual(row['artists'], '')
        self.assertIsNone(row['date'])
        self.assertIsNone(row['min_price'])
        self.assertEqual(row['address'], '')
        self.assertIsNone(row['latitude'])

    def test_nested_event(self):
        row = tm.parse_event({
            'id': '1',
            '_embedded': {'attractions': [{'id': 'a', 'name': 'Música'}],
                          'venues': [{'name': 'Sala', 'city': {'name': 'Madrid'}}]},
            'classifications': [{'genre': {'name': 'Otro'}},
                                {'primary': True, 'genre': {'name': 'Rock'}, 'segment': None}],
            'images': [{'url': 'small', 'width': 10, 'height': 10},
                       {'url': 'large', 'width': 100, 'height': 100}],
            'priceRanges': [{'min': 0, 'max': 20, 'currency': 'EUR'}],
        })
        self.assertEqual((row['artists'], row['city'], row['genre']), ('Música', 'Madrid', 'Rock'))
        self.assertEqual(row['min_price'], 0)
        self.assertEqual(row['image_url'], 'large')

    @patch.object(tm.requests, 'Session')
    def test_pagination_and_dates(self, session_class):
        session = session_class.return_value.__enter__.return_value
        calls = []
        def get(url, params, timeout):
            calls.append(dict(params))
            return self.response({'_embedded': {'events': [{'id': str(params['page'])}]},
                                  'page': {'totalPages': 2, 'totalElements': 2}})
        session.get.side_effect = get
        rows = tm.download_events('secret', 'ES', 'music', '2026-10-01', '2026-10-31')
        self.assertEqual(len(rows), 2)
        self.assertEqual([c['page'] for c in calls], [0, 1])
        self.assertTrue(all(c['size'] < 200 for c in calls))
        self.assertEqual(calls[0]['startDateTime'], '2026-10-01T00:00:00Z')
        self.assertEqual(calls[0]['endDateTime'], '2026-10-31T23:59:59Z')

    @patch.object(tm.requests, 'Session')
    def test_split_downloads_over_1000_events_including_boundaries(self, session_class):
        session = session_class.return_value.__enter__.return_value
        start = datetime(2026, 10, 1)
        offsets = [i * 120 for i in range(1100)] + [86399, 172799]
        events = [
            {'id': str(i), 'dates': {'start': {
                'dateTime': (start + timedelta(seconds=offset)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }}}
            for i, offset in enumerate(offsets)
        ]
        calls = []
        def get(url, params, timeout):
            calls.append(dict(params))
            matching = [event for event in events if
                        params['startDateTime'] <= event['dates']['start']['dateTime'] <= params['endDateTime']]
            offset = params['page'] * params['size']
            self.assertLess(offset, 1000)
            return self.response({
                '_embedded': {'events': matching[offset:offset + params['size']]},
                'page': {'totalElements': len(matching), 'totalPages': (len(matching) + 99) // 100},
            })
        session.get.side_effect = get
        rows = tm.download_events('secret', 'ES', 'music', '2026-10-01', '2026-10-02')
        self.assertEqual(len(rows), 1102)
        self.assertEqual({row['event_id'] for row in rows}, {event['id'] for event in events})
        self.assertGreater(len({(c['startDateTime'], c['endDateTime']) for c in calls}), 1)
        self.assertTrue(all(c['includeTBA'] == 'no' and c['includeTBD'] == 'no' for c in calls))

    @patch.object(tm.requests, 'Session')
    def test_exactly_1000_does_not_split(self, session_class):
        session = session_class.return_value.__enter__.return_value
        session.get.side_effect = lambda url, params, timeout: self.response({
            '_embedded': {'events': [{'id': str(params['page'] * 100 + i)} for i in range(100)]},
            'page': {'totalPages': 10, 'totalElements': 1000},
        })
        rows = tm.download_events('secret', 'ES', 'music')
        self.assertEqual(len(rows), 1000)
        self.assertEqual(session.get.call_count, 10)

    @patch.object(tm.requests, 'Session')
    def test_unsplittable_interval_fails_instead_of_truncating(self, session_class):
        session = session_class.return_value.__enter__.return_value
        session.get.return_value = self.response({'page': {'totalPages': 11, 'totalElements': 1100}})
        with self.assertRaisesRegex(RuntimeError, 'mismo segundo'):
            tm.download_events('secret', 'ES', 'music', '2026-10-01', '2026-10-01')
        self.assertLess(session.get.call_count, 20)

    def test_default_date_range_and_leap_day(self):
        start, end = tm.resolve_date_range('2026-09-29')
        self.assertEqual(start, datetime(2026, 9, 29))
        self.assertEqual(end, datetime(2028, 9, 29, 23, 59, 59))
        start, end = tm.resolve_date_range('2028-02-29')
        self.assertEqual(end, datetime(2030, 2, 28, 23, 59, 59))
        start, _ = tm.resolve_date_range()
        self.assertEqual(start.date(), datetime.now(timezone.utc).date())

    @patch.object(tm.requests, 'Session')
    def test_errors_do_not_expose_api_key(self, session_class):
        session = session_class.return_value.__enter__.return_value
        session.get.return_value = self.response({}, 401)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 401') as error:
            tm.download_events('secret', 'ES', 'music')
        self.assertNotIn('secret', str(error.exception))
        session.get.side_effect = requests.ConnectionError('https://example.com/?apikey=secret')
        with self.assertRaisesRegex(RuntimeError, 'conectar') as error:
            tm.download_events('secret', 'ES', 'music')
        self.assertNotIn('secret', str(error.exception))

    @patch.object(tm.requests, 'Session')
    def test_empty_response_and_invalid_json(self, session_class):
        session = session_class.return_value.__enter__.return_value
        session.get.return_value = self.response({'page': {'totalElements': 0}})
        self.assertEqual(tm.download_events('secret', 'ES', 'music'), [])
        session.get.return_value.json.side_effect = ValueError('invalid JSON')
        with self.assertRaisesRegex(RuntimeError, 'JSON válido'):
            tm.download_events('secret', 'ES', 'music')

    def test_csv_roundtrip_and_empty_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'nested' / 'events.csv'
            name = 'Música, "en vivo"\nEspaña'
            late = tm.parse_event({'id': '2', 'name': name, 'dates': {'start': {'localDate': '2026-10-02'}}})
            early = tm.parse_event({'id': '1', 'dates': {'start': {'localDate': '2026-10-01'}}})
            tm.save_csv([late, early, late], output)
            with output.open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r['fecha'] for r in rows], ['2026-10-01', '2026-10-02'])
            self.assertEqual(list(rows[0]), ['nombre_concierto', 'artista', 'genero', 'fecha', 'recinto', 'direccion', 'codigo_postal', 'ciudad', 'provincia', 'pais', 'latitud', 'longitud'])
            self.assertEqual(rows[0]['artista'], '')
            self.assertEqual(rows[1]['nombre_concierto'], ' '.join(name.split()))
            tm.save_csv([], output)
            with output.open(encoding='utf-8-sig', newline='') as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, ['nombre_concierto', 'artista', 'genero', 'fecha', 'recinto', 'direccion', 'codigo_postal', 'ciudad', 'provincia', 'pais', 'latitud', 'longitud'])
                self.assertEqual(list(reader), [])

    def test_duplicate_visible_rows_with_different_ids(self):
        rows = [
            tm.parse_event({
                'id': 'a', 'name': 'Concierto  de Música',
                'dates': {'start': {'localDate': '2026-10-01', 'localTime': '18:00:00'}},
            }),
            tm.parse_event({
                'id': 'b', 'name': ' Concierto de Música ',
                'dates': {'start': {'localDate': '2026-10-01', 'localTime': '21:00:00'}},
            }),
            tm.parse_event({
                'id': 'c', 'name': 'Concierto de Música',
                'dates': {'start': {'localDate': '2026-10-02'}},
            }),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'events.csv'
            df = tm.save_csv(rows, output)
            with output.open(encoding='utf-8-sig', newline='') as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(len(df), 2)
            self.assertEqual(len(exported), 2)
            self.assertEqual([row['fecha'] for row in exported], ['2026-10-01', '2026-10-02'])
            self.assertTrue(all(row['nombre_concierto'] == 'Concierto de Música' for row in exported))

    def test_location_export_and_distinct_venues(self):
        venue = {
            'name': 'Sala Música',
            'address': {'line1': 'Calle Mayor, 1', 'line2': 'Planta baja', 'line3': None},
            'postalCode': '08001', 'city': {'name': 'Barcelona'},
            'state': {'name': 'Barcelona'}, 'country': {'name': 'Spain'},
            'location': {'latitude': '41.3800', 'longitude': '2.1700'},
        }
        event = {'id': 'a', 'name': 'Concierto', '_embedded': {'venues': [venue]}}
        first = tm.parse_event(event)
        second = tm.parse_event({**event, 'id': 'b', '_embedded': {
            'venues': [{**venue, 'name': 'Otra sala', 'address': None, 'location': None}],
        }})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'events.csv'
            tm.save_csv([first, first, second], output)
            with output.open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['direccion'], 'Calle Mayor, 1, Planta baja')
            self.assertEqual(rows[0]['codigo_postal'], '08001')
            self.assertEqual(rows[0]['latitud'], '41.3800')
            self.assertEqual(rows[0]['longitud'], '2.1700')
            self.assertEqual(rows[1]['direccion'], '')
            self.assertEqual(rows[1]['latitud'], '')

    def test_invalid_dates(self):
        for args in [
            ['--start-date', '2026-02-30'],
            ['--start-date', '2026-11-01', '--end-date', '2026-10-01'],
        ]:
            with self.subTest(args=args), patch('sys.argv', ['script'] + args):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    tm.parse_args()
                self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
