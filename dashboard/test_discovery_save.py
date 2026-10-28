import csv
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from discover_commoncrawl import Found, PLATFORMS, _known, _save_companies


class DiscoverySaveTests(unittest.TestCase):
    def test_missing_oracle_directory_and_idempotent_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            platform = replace(PLATFORMS['oracle'], csv_path=Path(folder) / 'oracle' / 'oracle.csv')
            first = Found('example.oraclecloud.com/CX_1', 'Example', 10)
            duplicate = Found('EXAMPLE.ORACLECLOUD.COM/cx_1', 'Example duplicate', 10)
            self.assertEqual(_save_companies(platform, [], [first, duplicate]), 1)
            rows, known = _known(platform)
            self.assertEqual(len(rows), 1)
            second = Found('second.oraclecloud.com/CX_1', 'Second', 20)
            self.assertEqual(_save_companies(platform, rows, [first, second]), 1)
            rows, known = _known(platform)
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(known), 2)
            self.assertEqual(_save_companies(platform, rows, [first, second]), 0)
            self.assertEqual(len(_known(platform)[0]), 2)

    def test_existing_csv_duplicates_removed_and_custom_fields_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            platform = replace(PLATFORMS['oracle'], csv_path=Path(folder) / 'oracle.csv')
            rows = [dict(name='Existing', slug='example.oraclecloud.com/CX_1', url='https://example', note='keep'),
                    dict(name='Duplicate', slug='EXAMPLE.ORACLECLOUD.COM/cx_1', url='https://duplicate')]
            _save_companies(platform, rows, [Found('second.oraclecloud.com/CX_1', 'Second', 1)])
            with platform.csv_path.open(newline='') as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]['note'], 'keep')

    def test_text_lists_merge_case_insensitively(self):
        with tempfile.TemporaryDirectory() as folder:
            platform = replace(PLATFORMS['lever'], csv_path=Path(folder) / 'lever' / 'companies.txt')
            self.assertEqual(_save_companies(platform, [{'slug': 'Existing'}],
                                            [Found('existing', 'Existing', 1), Found('new', 'New', 2)]), 1)
            self.assertEqual(platform.csv_path.read_text().splitlines(), ['Existing', 'new'])


if __name__ == '__main__':
    unittest.main()
