"""Partition the catalog, crawl one independent slice, and merge CSV artifacts."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROVIDERS = ('adp', 'icims')
FIELDS = ['source_board', 'external_id', 'board_id', 'job_title', 'company',
          'location', 'countries', 'posted_at', 'job_url', 'apply_url', 'description',
          'salary_min', 'salary_max', 'salary_currency', 'salary_period', 'salary',
          'experience_min', 'experience_max', 'experience', 'skills',
          'skills_required', 'skills_preferred', 'work_mode', 'job_type',
          'education_requirements', 'basic_qualifications',
          'preferred_qualifications', 'key_responsibilities', 'is_remote',
          'country_evidence', 'also_on', 'structured_fields', 'extraction', 'description_raw']


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def partitions(boards, size, limit=0):
    if not 1 <= size <= 500 or limit < 0:
        raise ValueError('boards-per-slice must be 1..500; max-boards cannot be negative')
    unique = {}
    for b in boards:
        if b['provider'] not in PROVIDERS or b.get('status') in ('disabled', 'retired', 'rejected'):
            continue
        key = (b['provider'], b['board'].lower(), b.get('region', '').lower())
        unique.setdefault(key, b)
    slices = []
    for provider in PROVIDERS:
        group = [b for k, b in sorted(unique.items()) if k[0] == provider]
        if limit:
            group = group[:limit]
        for offset in range(0, len(group), size):
            slices.append({'id': f'{provider}-{offset // size:03d}', 'provider': provider,
                           'boards': group[offset:offset + size]})
    if not slices or len(slices) > 256:
        raise ValueError(f'{len(slices)} slices: GitHub matrix needs 1..256; increase boards-per-slice')
    return slices


def prepare(args):
    slices = partitions(json.loads(args.catalog.read_text()), args.boards_per_slice, args.max_boards)
    digest = hashlib.sha256(json.dumps(slices, sort_keys=True).encode()).hexdigest()[:16]
    matrix = {'include': [{'id': s['id'], 'provider': s['provider']} for s in slices]}
    manifest = {'catalog_hash': digest, 'matrix': matrix,
                'boards': dict(Counter(b['provider'] for s in slices for b in s['boards'])),
                'slices': [{'id': s['id'], 'provider': s['provider'], 'count': len(s['boards'])} for s in slices]}
    write_json(args.output / 'manifest.json', manifest)
    for s in slices:
        write_json(args.output / 'slices' / (s['id'] + '.json'), s['boards'])
    if args.github_output:
        with args.github_output.open('a') as f:
            f.write('matrix=' + json.dumps(matrix, separators=(',', ':')) + '\n')
            f.write('catalog_hash=' + digest + '\n')
    print(json.dumps({'slices': len(slices), 'boards': manifest['boards']}))


def run_slice(args):
    from job_extraction.engine import extract_job
    args.output.mkdir(parents=True, exist_ok=True)
    raw = args.output / 'raw-jobs.json'
    report_path = args.output / 'crawl-report.json'
    for path in (raw, report_path, args.output / 'jobs.json'):
        path.unlink(missing_ok=True)
    proc = subprocess.run([str(args.binary.resolve()), '--catalog', str(args.catalog.resolve()),
                           '--providers', args.provider, '--output', str(raw.resolve()),
                           '--report', str(report_path.resolve()), '--state', str(args.state.resolve()),
                           '--workers', str(args.workers), '--board-timeout', args.board_timeout])
    if not raw.exists() or not report_path.exists():
        raise RuntimeError('Crawler did not produce fresh jobs and report')
    rows = [extract_job(row) for row in json.loads(raw.read_text())]
    for row in rows:
        row['extraction']['model'] = {'status': 'disabled'}
    write_json(args.output / 'jobs.json', rows)
    report = json.loads(report_path.read_text())
    report.update(slice_id=args.slice_id, process_exit_code=proc.returncode, exported_jobs=len(rows))
    write_json(report_path, report)
    raw.unlink()  # jobs.json retains every original source field plus extracted data.
    print(json.dumps({'slice': args.slice_id, 'jobs': len(rows), 'failed_boards': report['failed_boards']}))
    return proc.returncode


def merge(args):
    manifest = json.loads(args.manifest.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    reports, missing, seen = [], [], set()
    counts = Counter()
    duplicates = 0
    paths = {p: args.output / f'{p}_usa_jobs.csv' for p in PROVIDERS}
    paths['all'] = args.output / 'adp_icims_usa_jobs.csv'
    handles = {p: path.with_suffix('.csv.tmp').open('w', encoding='utf-8-sig', newline='') for p, path in paths.items()}
    try:
        writers = {p: csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore') for p, f in handles.items()}
        for writer in writers.values():
            writer.writeheader()
        for s in manifest['slices']:
            folder = args.input / ('ats-slice-' + s['id'])
            jobs_path, report_path = folder / 'jobs.json', folder / 'crawl-report.json'
            if not jobs_path.exists() or not report_path.exists():
                missing.append(s['id'])
                continue
            report = json.loads(report_path.read_text())
            if report.get('slice_id') != s['id'] or len(report['boards']) != s['count']:
                missing.append(s['id'])
            reports.append(report)
            for row in json.loads(jobs_path.read_text()):
                provider = row['source_board'].lower()
                if provider != s['provider'] or not row.get('job_url') or not row.get('description'):
                    raise ValueError(f'Invalid job in slice {s["id"]}')
                key = (provider, row['job_url'].split('#')[0])
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                encoded = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()}
                writers[provider].writerow(encoded)
                writers['all'].writerow(encoded)
                counts[provider] += 1
    finally:
        for handle in handles.values():
            handle.close()
    for path in paths.values():
        path.with_suffix('.csv.tmp').replace(path)
    failed = sum(r.get('failed_boards', 0) for r in reports)
    summary = {'finished_at': datetime.now(timezone.utc).isoformat(),
               'all_slices_received': not missing,
               'all_boards_succeeded': not missing and failed == 0 and all(r.get('process_exit_code') == 0 for r in reports),
               'boards_expected': manifest['boards'], 'boards_processed': sum(len(r['boards']) for r in reports),
               'slices_expected': len(manifest['slices']), 'slices_received': len(reports),
               'missing_or_incomplete_slices': missing, 'failed_or_partial_boards': failed,
               'jobs': {p: counts[p] for p in PROVIDERS}, 'total_jobs': sum(counts.values()),
               'duplicates_removed': duplicates, 'catalog_hash': manifest['catalog_hash'],
               'scope': 'Readable jobs with explicit US location evidence; unspecified geography excluded. Unstated fields remain blank.'}
    write_json(args.output / 'summary.json', summary)
    write_json(args.output / 'board-results.json', [b for r in reports for b in r['boards']])
    text = ('## ADP and iCIMS US CSV export\n\n'
            + '\n'.join(f'- {p}: {counts[p]:,} jobs' for p in PROVIDERS)
            + f'\n- Boards processed: {summary["boards_processed"]:,}\n'
            + f'- Failed or partial boards: {failed:,}\n- Missing or incomplete slices: {len(missing)}\n\n'
            + 'Download **adp-icims-usa-csv** from this run’s Artifacts.\n')
    (args.output / 'README.md').write_text(text)
    if args.github_summary:
        with args.github_summary.open('a') as f:
            f.write(text)
    print(json.dumps(summary, indent=2))
    # Attach successful results even when this final completeness check fails.
    return 0 if summary['all_boards_succeeded'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--catalog', type=Path, default=ROOT / 'boards.json')
    p.add_argument('--boards-per-slice', type=int, default=50)
    p.add_argument('--max-boards', type=int, default=0)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--github-output', type=Path)
    p = sub.add_parser('crawl')
    p.add_argument('--binary', type=Path, default=ROOT / 'bin/getworkin-ingest')
    p.add_argument('--catalog', type=Path, required=True)
    p.add_argument('--provider', choices=PROVIDERS, required=True)
    p.add_argument('--slice-id', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--state', type=Path, default=ROOT / 'state')
    p.add_argument('--workers', type=int, default=8)
    p.add_argument('--board-timeout', default='5m')
    p = sub.add_parser('merge')
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--github-summary', type=Path)
    args = parser.parse_args()
    return {'prepare': prepare, 'crawl': run_slice, 'merge': merge}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())
