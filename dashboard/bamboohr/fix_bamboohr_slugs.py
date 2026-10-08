"""
fix_bamboohr_slugs.py
Resolves display-name entries (with spaces) in bamboohr_companies.txt
to their actual BambooHR subdomain slugs by probing candidate URLs.
"""
import asyncio
import httpx
import re
from pathlib import Path

COMPANIES_FILE = Path(__file__).parent / "bamboohr_companies.txt"
TIMEOUT = 8.0
CONCURRENCY = 20


def name_to_candidates(name: str) -> list[str]:
    lower = name.lower().strip()
    no_space = re.sub(r"\s+", "", lower)
    hyphen = re.sub(r"\s+", "-", lower)
    alphanum = re.sub(r"[^a-z0-9]", "", lower)
    alphanum_hyphen = re.sub(r"[^a-z0-9-]", "", hyphen)
    seen, out = set(), []
    for c in [no_space, hyphen, alphanum, alphanum_hyphen]:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def probe(client: httpx.AsyncClient, slug: str) -> bool:
    url = f"https://{slug}.bamboohr.com/careers/list"
    try:
        r = await client.get(url, follow_redirects=True, timeout=TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


async def resolve_name(sem: asyncio.Semaphore, client: httpx.AsyncClient, name: str) -> str | None:
    async with sem:
        for candidate in name_to_candidates(name):
            if await probe(client, candidate):
                print(f"  OK  '{name}' -> '{candidate}'")
                return candidate
        print(f"  XX  '{name}' -> no working slug found")
        return None


async def main():
    lines = COMPANIES_FILE.read_text().splitlines()
    spaced = [l.strip() for l in lines if " " in l.strip() and l.strip()]
    normal = [l.strip() for l in lines if " " not in l.strip() and l.strip()]

    print(f"Found {len(spaced)} entries with spaces to resolve...\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(verify=False) as client:
        tasks = [resolve_name(sem, client, name) for name in spaced]
        results = await asyncio.gather(*tasks)

    resolved = [r for r in results if r]
    unresolved_count = len(spaced) - len(resolved)

    final_slugs = sorted(set(normal + resolved))
    COMPANIES_FILE.write_text("\n".join(final_slugs) + "\n")

    print(f"\nDone! Resolved {len(resolved)}/{len(spaced)} entries.")
    print(f"Skipped {unresolved_count} unresolvable entries.")
    print(f"Total companies: {len(final_slugs)}")


if __name__ == "__main__":
    asyncio.run(main())
