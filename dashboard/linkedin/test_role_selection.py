import ast
import csv
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from role_selection import select_roles

ROOT = Path(__file__).resolve().parents[2]


def catalog():
    tree = ast.parse((ROOT / 'dashboard/linkedin/linkedin_68_roles.py').read_text())
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == 'ROLES')


class RoleSelectionTests(unittest.TestCase):
    def test_default_selection_is_balanced_and_scraped_once(self):
        roles = catalog()
        for total in (4, 8, 12, 16):
            shards = [select_roles(roles, filters=roles[:23], shard=f'{part}/{total}')
                      for part in range(1, total + 1)]
            flattened = [role for shard in shards for role in shard]
            self.assertEqual(flattened, roles[:23])
            self.assertEqual(len(flattened), len(set(flattened)))
            self.assertLessEqual(max(map(len, shards)) - min(map(len, shards)), 1)
        self.assertEqual([len(select_roles(roles, filters=roles[:23], shard=f'{p}/8'))
                          for p in range(1, 9)], [3, 3, 3, 3, 3, 3, 3, 2])

    def test_overlapping_substrings_do_not_repeat_roles(self):
        roles = ['Senior Buyer', 'Buyer', 'Engineer']
        self.assertEqual(select_roles(roles, filters=['buyer', ' Senior BUYER ', 'buyer']), roles[:2])

    def test_empty_filter_uses_configured_index_range(self):
        roles = catalog()
        shards = [select_roles(roles, start_idx=24, end_idx=45, shard=f'{p}/8') for p in range(1, 9)]
        self.assertEqual([r for s in shards for r in s], roles[23:45])

    def test_more_runners_than_roles_and_invalid_input(self):
        self.assertEqual(select_roles(['Buyer'], shard='8/8'), [])
        for shard in ('0/8', '9/8', '1/0', 'bad', '1/2/3'):
            with self.assertRaises(ValueError):
                select_roles(['Buyer'], shard=shard)
        with self.assertRaises(ValueError):
            select_roles(['Buyer'], filters=['unknown role'])

    def test_workflow_exports_one_all_sheet(self):
        from openpyxl import load_workbook
        workflow = (ROOT / '.github/workflows/linkedin_roles_slice.yml').read_text()
        # Execute the real merge/export code using saved-row fixtures, never a scraper.
        code = workflow.split("python3 - <<'PYEOF'\n", 1)[1].split('          PYEOF', 1)[0]
        import textwrap
        code = textwrap.dedent(code)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'dashboard').mkdir()
            for shard, rows in enumerate(([
                {'id': 'one', 'job_title': 'Buyer', 'description': 'Salary: $90-110K per year.',
                 'job_url': 'https://www.linkedin.com/jobs/view/4999999999', 'reposted': True,
                 'reposted_source': 'linkedin_label', 'reposted_confidence': 'explicit'},
            ], [
                {'id': 'one', 'job_title': 'Buyer', 'description': 'Duplicate row', 'job_url': 'https://example.com/1'},
                {'id': 'two', 'job_title': 'Manager', 'description': 'Manage sourcing.\nRequired: SQL.', 'job_url': 'https://example.com/2'},
            ]), 1):
                folder = root / f'scraped_data/shard-{shard}'
                folder.mkdir(parents=True)
                with (folder / f'linkedin_shard_{shard}-8_jobs.csv').open('w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            cwd = Path.cwd()
            try:
                with patch.dict(os.environ, {'GITHUB_WORKSPACE': str(root), 'SCRAPER_DIR': 'dashboard',
                                             'ROLE_START': '1', 'ROLE_END': '23'}):
                    with patch.object(sys, 'path', [str(ROOT / 'dashboard'), str(ROOT / 'dashboard/db'), *sys.path]):
                        exec(compile(code, 'workflow-merge', 'exec'), {})
            finally:
                os.chdir(cwd)
            files = list((root / 'dashboard/deliverables').iterdir())
            self.assertEqual([f.name for f in files], ['jobs_roles_1_to_23.xlsx'])
            book = load_workbook(files[0], read_only=True)
            self.assertEqual(book.sheetnames, ['All'])
            rows = list(book['All'].values)
            self.assertEqual(len(rows), 3)
            id_column = rows[0].index('id')
            self.assertEqual({r[id_column] for r in rows[1:]}, {'one', 'two'})
            by_id = {r[id_column]: r for r in rows[1:]}
            self.assertEqual(by_id['one'][rows[0].index('reposted_source')], 'linkedin_label')
            self.assertEqual(by_id['one'][rows[0].index('repost_status')], 'Reposted')
            self.assertEqual(by_id['two'][rows[0].index('repost_status')], 'Unknown')
            book.close()


if __name__ == '__main__':
    unittest.main()
