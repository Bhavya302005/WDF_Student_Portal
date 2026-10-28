import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import discover_commoncrawl as discovery


class HarvestTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_pages_extract_distinct_lever_boards(self):
        calls = []

        async def fetch(client, url, params):
            calls.append(params)
            if params.get('showNumPages'):
                return json.dumps({'pages': 2})
            return json.dumps({'url': f'https://jobs.lever.co/company-{params["page"]}/job-id'})

        class Client:
            async def get(self, *args, **kwargs):
                return httpx.Response(200, json=[{'id': 'test', 'cdx-api': 'https://index.test'}])

        diagnostics = {}
        with patch.object(discovery, '_get_text', side_effect=fetch), patch.object(discovery.asyncio, 'sleep', new=AsyncMock()):
            found = await discovery.harvest(Client(), discovery.PLATFORMS['lever'], diagnostics=diagnostics)
        self.assertEqual(found, {'company-0', 'company-1'})
        self.assertTrue(diagnostics['complete'])
        self.assertTrue(all(call['pageSize'] == 1 for call in calls))

    async def test_failed_count_tries_first_page_but_marks_partial(self):
        class Client:
            async def get(self, *args, **kwargs):
                return httpx.Response(200, json=[{'id': 'test', 'cdx-api': 'https://index.test'}])

        diagnostics = {}
        with patch.object(discovery, '_get_text', side_effect=[discovery.IndexFetchError('timeout'),
                json.dumps({'url': 'https://jobs.lever.co/new-company/job'})]), \
                patch.object(discovery.asyncio, 'sleep', new=AsyncMock()):
            found = await discovery.harvest(Client(), discovery.PLATFORMS['lever'], diagnostics=diagnostics)
        self.assertEqual(found, {'new-company'})
        self.assertFalse(diagnostics['complete'])
        self.assertEqual(diagnostics['failed_snapshots'], 1)

    async def test_no_readable_pages_raises_instead_of_empty_success(self):
        class Client:
            async def get(self, *args, **kwargs):
                return httpx.Response(200, json=[{'id': 'test', 'cdx-api': 'https://index.test'}])
        with patch.object(discovery, '_get_text', side_effect=discovery.IndexFetchError('timeout')):
            with self.assertRaisesRegex(discovery.IndexFetchError, 'no readable'):
                await discovery.harvest(Client(), discovery.PLATFORMS['lever'])

    async def test_exhausted_gateway_retries_are_not_empty_success(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(504, text='gateway timeout'))
        async with httpx.AsyncClient(transport=transport) as client:
            with patch.object(discovery.asyncio, 'sleep', new=AsyncMock()):
                with self.assertRaises(discovery.IndexFetchError):
                    await discovery._get_text(client, 'https://index.test', {'page': 0})

    async def test_real_no_captures_is_a_valid_empty_snapshot(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(404, text='No Captures found for: jobs.lever.co/*'))
        async with httpx.AsyncClient(transport=transport) as client:
            value = await discovery._get_text(client, 'https://index.test', {'showNumPages': 'true'})
        self.assertEqual(json.loads(value), {'pages': 0})


if __name__ == '__main__':
    unittest.main()
